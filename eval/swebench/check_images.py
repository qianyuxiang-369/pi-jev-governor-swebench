#!/usr/bin/env python3
"""Verify that local SWE-bench images still match the digests recorded when the
patches were produced.

The evaluator applies each prediction to a fresh container spawned from the
dataset image tag; if that tag now resolves to a different digest, grading runs
against a different tree than the one the agent worked in, and the comparison
silently measures the wrong thing. Run this before `swebench eval`:

    python check_images.py results/pilot-01

Exit code is non-zero when any recorded image is missing or has moved, when one
tag resolved to more than one digest inside the same run, when one tag mapped to
different /testbed trees inside the same run (patches produced against different
trees), when the run itself is structurally incomplete: an
unreadable or absent manifest, an empty planned set, a planned instance
without metadata or without a recorded outcome, a recorded outcome without an
image digest or patch.diff, or metadata for an instance no manifest lists, or
when the prediction file the evaluator will grade from does not match the
recorded outcomes (absent, blank or unparsable lines, rows missing
instance_id / model_name_or_path / model_patch, duplicated, missing lines,
extra lines, or patch text different from the patch.diff on disk).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

RESULT_STATUSES = frozenset({"success", "timeout_partial", "agent_error"})


def inspect_image_id(tag: str) -> str | None:
    try:
        res = subprocess.run(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def probe_image_head(tag: str) -> str | None:
    """Measure the /testbed git HEAD of the tree `tag` currently resolves to.

    This is the content fingerprint of the tree a grading container spawned
    from this tag would run in. It deliberately avoids image_id, which
    docker-rt reports differently for every copy of the same tar. Read-only:
    a --rm container prints the HEAD and is removed immediately.
    """
    try:
        res = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "git", tag,
             "-C", "/testbed", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def analyze_run(run_dir: Path, inspect=inspect_image_id,
                probe=probe_image_head) -> tuple[list[str], bool]:
    """Full structural + content-consistency check. Returns (report lines, ok).

    `inspect` and `probe` are injectable so the analysis is testable without
    docker.
    """
    lines: list[str] = []
    problems = 0
    records: dict[tuple[str, str], dict] = {}
    for mf in sorted(run_dir.glob("*/*/metadata.json")):
        variant, iid = mf.parent.parent.name, mf.parent.name
        try:
            meta = json.loads(mf.read_text())
        except Exception:
            lines.append(f"UNREADABLE metadata {variant}/{iid}/metadata.json")
            problems += 1
            continue
        records[(variant, iid)] = meta
    if not records:
        return lines + [f"no */<instance>/metadata.json found under {run_dir}"], False

    # (1) Run completeness: every planned instance must be accounted for, a
    # recorded outcome must carry both the image digest the evaluator will
    # grade against and the patch file it will submit, and the plan itself
    # must be readable. Every gap here is fail-closed: a run that cannot
    # prove what it planned and produced cannot be graded as one.
    planned_variants: set[str] = set()
    all_planned: set[tuple[str, str]] = set()
    for variant_dir in sorted(d for d in run_dir.iterdir() if d.is_dir()):
        vname = variant_dir.name
        mf = variant_dir / "manifest.json"
        planned: list[str] = []
        if not mf.is_file():
            lines.append(f"MISSING  {vname}/manifest.json: planned set unknown "
                         "(run completeness cannot be verified)")
            problems += 1
        else:
            try:
                planned = json.loads(mf.read_text()).get("instances") or []
            except Exception:
                lines.append(f"UNREADABLE manifest {vname}/manifest.json")
                problems += 1
            else:
                if not planned:
                    lines.append(f"EMPTY    {vname}/manifest.json: "
                                 "no planned instances recorded")
                    problems += 1
                else:
                    planned_variants.add(vname)
        for iid in planned:
            all_planned.add((vname, iid))
            meta = records.get((vname, iid))
            if meta is None:
                lines.append(f"MISSING  {vname}/{iid}: no metadata (run incomplete)")
                problems += 1
                continue
            if meta.get("status") in RESULT_STATUSES:
                if not meta.get("image_id"):
                    lines.append(f"NO-IMAGE {vname}/{iid}: recorded outcome "
                                 "without image_id")
                    problems += 1
                if not (variant_dir / iid / "patch.diff").is_file():
                    # An outcome without a patch file would be submitted as an
                    # empty patch: fail closed instead of grading silently.
                    lines.append(f"NO-PATCH {vname}/{iid}: recorded outcome without "
                                 "patch.diff (would grade as an empty patch)")
                    problems += 1
            else:
                # A planned instance that ended in an infra status never
                # entered preds: the evaluator would drop it from the graded
                # set instead of counting a failure. Fail closed until it is
                # re-run.
                lines.append(f"NO-OUTCOME {vname}/{iid}: status={meta.get('status')!r} "
                             "(planned instance without a recorded outcome)")
                problems += 1
    for vname, iid in sorted(set(records) - all_planned):
        if vname in planned_variants:
            lines.append(f"UNPLANNED {vname}/{iid}: metadata not listed in "
                         f"{vname}/manifest.json")
            problems += 1

    # (2) Grading input: the evaluator grades preds.jsonl, not the instance
    # directories. Verify the file that is actually fed to grading -- no blank
    # or unparsable lines (one json.loads per line: a blank line makes the
    # whole file unloadable), every row carrying the fields the evaluator
    # reads with direct subscripts, one line per recorded outcome with patch
    # text identical to the patch.diff on disk, no duplicates, no extras -- or
    # a run whose records look complete can still fail to grade at all, or be
    # graded from a torn, stale, or shifted prediction file.
    for vname in sorted(planned_variants):
        expected = {iid for v, iid in all_planned if v == vname
                    and records.get((v, iid), {}).get("status") in RESULT_STATUSES}
        preds_path = run_dir / vname / "preds.jsonl"
        if not preds_path.is_file():
            lines.append(f"MISSING  {vname}/preds.jsonl: grading input absent")
            problems += 1
            continue
        try:
            raw_preds = preds_path.read_text()
        except OSError as exc:
            lines.append(f"UNREADABLE {vname}/preds.jsonl: {exc}")
            problems += 1
            continue
        seen: dict[str, int] = {}
        for lineno, raw in enumerate(raw_preds.splitlines(), 1):
            if not raw.strip():
                # The evaluator parses the file with one json.loads per line
                # (harness/utils.py, no strip, no skip): a single blank line
                # aborts loading the entire predictions file.
                lines.append(f"BAD-PREDS {vname}/preds.jsonl:{lineno}: blank line")
                problems += 1
                continue
            try:
                row = json.loads(raw)
            except Exception:
                lines.append(f"BAD-PREDS {vname}/preds.jsonl:{lineno}: unparseable line")
                problems += 1
                continue
            iid = row.get("instance_id")
            name = row.get("model_name_or_path")
            # instance_id/model_patch are read with direct subscripts and the
            # report filename embeds model_name_or_path (.replace): a row
            # without them crashes grading only after every instance ran.
            gaps = []
            if not isinstance(iid, str) or not iid:
                gaps.append("instance_id")
            if not isinstance(name, str) or not name:
                gaps.append("model_name_or_path")
            if "model_patch" not in row:
                gaps.append("model_patch")
            if gaps:
                lines.append(f"BAD-PREDS {vname}/preds.jsonl:{lineno}: "
                             f"missing {'/'.join(gaps)}")
                problems += 1
                continue
            seen[iid] = seen.get(iid, 0) + 1
            patch_file = run_dir / vname / iid / "patch.diff"
            if iid in expected and patch_file.is_file():
                try:
                    disk_patch = patch_file.read_text()
                except OSError:
                    disk_patch = None
                if disk_patch is not None and row.get("model_patch") != disk_patch:
                    # The evaluator submits model_patch, so a prediction that
                    # no longer matches the record grades a different tree.
                    lines.append(f"STALE-PREDS {vname}/{iid}: model_patch differs from "
                                 "patch.diff on disk (preds not rebuilt from the records)")
                    problems += 1
        for iid in sorted(i for i, n in seen.items() if n > 1):
            lines.append(f"DUP-PREDS {vname}/preds.jsonl: {seen[iid]} lines for "
                         f"{iid} (prediction duplicated)")
            problems += 1
        for iid in sorted(expected - set(seen)):
            lines.append(f"NO-PRED  {vname}/{iid}: recorded outcome missing from "
                         "preds.jsonl (would grade as incomplete)")
            problems += 1
        for iid in sorted(set(seen) - expected):
            lines.append(f"EXTRA-PRED {vname}/preds.jsonl: prediction for {iid} "
                         "(no recorded outcome in this run)")
            problems += 1

    # (3) Content-tree consistency: every grading container is spawned from
    # the dataset image tag, so the tag must resolve to the same /testbed tree
    # the agent worked in. image_id is NOT usable evidence here: docker-rt
    # reports a distinct id for every copy of the same tar, so one tag
    # legitimately maps to several ids inside a run (observed in smoke-03:
    # six tags, twelve records, all valid). The recorded image_head -- git
    # HEAD of /testbed measured inside the run container -- is the run-side
    # fingerprint, and the tag's current tree is probed the same way; metadata
    # predating image_head falls back to the image_id comparison.
    heads_by_tag: dict[str, set[str]] = {}
    ids_by_tag: dict[str, set[str]] = {}
    for meta in records.values():
        image, image_id = meta.get("image"), meta.get("image_id")
        if image and image_id:
            ids_by_tag.setdefault(image, set()).add(image_id)
        head = meta.get("image_head")
        if image and head:
            heads_by_tag.setdefault(image, set()).add(str(head))

    tags = sorted(set(ids_by_tag) | set(heads_by_tag))
    if not tags:
        lines.append(f"no image/image_id pairs recorded in {len(records)} metadata file(s)")
        return lines, False

    ok = mismatched = missing = mixed = 0
    for image in tags:
        heads, ids = heads_by_tag.get(image) or set(), ids_by_tag.get(image) or set()
        if heads:
            # Content fingerprint path (docker-rt safe).
            if len(heads) > 1:
                # Patches inside this run were produced against two different
                # /testbed trees: the run cannot be graded as one.
                mixed += 1
                lines.append(f"MIXED    {image}  recorded heads: {sorted(heads)}\n"
                             "  one tag mapped to different /testbed trees inside this run")
            if inspect(image) is None:
                missing += 1
                lines.append(f"MISSING  {image}  (recorded heads: {sorted(heads)})")
                continue
            probed = probe(image)
            if probed is None:
                # Fail closed: without a live measurement the current tree is
                # unproven, even though the tag still resolves.
                mismatched += 1
                lines.append(f"PROBE-FAIL {image}: could not measure the /testbed HEAD "
                             "of the tree this tag resolves to")
            elif heads == {probed}:
                ok += 1
                lines.append(f"OK       {image}  head {probed}")
            else:
                mismatched += 1
                lines.append(f"MISMATCH {image}\n  recorded head(s): {sorted(heads)}\n"
                             f"  current tree HEAD: {probed}")
        else:
            # Legacy path: metadata predating image_head.
            if len(ids) > 1:
                # A set-membership check against the local digest would silently
                # pass this: the patches inside this run were produced against two
                # different trees, so the run cannot be graded as one.
                mixed += 1
                lines.append(f"MIXED    {image}  recorded digests: {sorted(ids)}\n"
                             "  one tag resolved to different digests inside this run")
            current = inspect(image)
            if current is None:
                missing += 1
                lines.append(f"MISSING  {image}  (recorded {sorted(ids)})")
            elif len(ids) == 1 and current in ids:
                ok += 1
                lines.append(f"OK       {image}  {current}")
            elif current not in ids:
                mismatched += 1
                lines.append(f"MISMATCH {image}\n  recorded: {sorted(ids)}\n  current:  {current}")
            # current in ids with len(ids) > 1 was already reported as MIXED.

    lines.append(f"\n{ok} ok, {mismatched} mismatched, {missing} missing, {mixed} mixed, "
                 f"{problems} structural ({len(tags)} image tag(s), "
                 f"{len(records)} metadata file(s))")
    bad = bool(mismatched or missing or mixed or problems)
    if bad:
        lines.append("do NOT grade this run until every tag matches: the evaluator would "
                     "apply patches to a different tree than the agent ran in")
    return lines, not bad


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check that docker images still match the digests recorded in a run")
    parser.add_argument("run_dir", help="a results/<run-id> directory produced by run.py")
    args = parser.parse_args()

    lines, ok = analyze_run(Path(args.run_dir))
    for line in lines:
        print(line)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
