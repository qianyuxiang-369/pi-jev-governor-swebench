#!/usr/bin/env python3
"""Unit tests for check_images.py (run-time integrity of graded images).

Covers the review findings:
  * a tag that resolved to two different digests inside one run must FAIL even
    when the local digest matches one of them (the old set-membership check
    silently passed it);
  * structural completeness is fail-closed: an absent or empty manifest, a
    planned instance without metadata or without a recorded outcome, a
    recorded outcome without image_id or patch.diff, and metadata for an
    unplanned instance all FAIL;
  * the graded input itself is verified: preds.jsonl must exist, carry no blank
    or unparsable lines, every row's instance_id / model_name_or_path /
    model_patch present, exactly one line per recorded outcome with patch text
    matching the patch.diff on disk, without duplicates or extras;
  * ok / missing / mismatch paths and the exit-relevant ok flag.

Run with:  /workspace/.conda/bin/python -m pytest test_check_images.py -q
"""
import importlib.util
import json
from pathlib import Path

CI_PY = Path(__file__).resolve().parent / "check_images.py"
_spec = importlib.util.spec_from_file_location("check_images", CI_PY)
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


def write_instance(run_dir: Path, variant: str, iid: str, patch=True, **meta) -> None:
    inst = run_dir / variant / iid
    inst.mkdir(parents=True, exist_ok=True)
    base = {"instance_id": iid, "status": "success",
            "image": f"img-{iid}", "image_id": "sha256:1"}
    base.update(meta)
    (inst / "metadata.json").write_text(json.dumps(base))
    if patch:
        # A recorded outcome always ships a patch file (possibly empty): the
        # default keeps every outcome-shaped fixture structurally complete.
        (inst / "patch.diff").write_text("diff --git a/x b/x\n")


def write_manifest(run_dir: Path, variant: str, instances: list[str],
                   with_preds: bool = True) -> None:
    vdir = run_dir / variant
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "manifest.json").write_text(json.dumps({"instances": instances}))
    if with_preds:
        # The runner rebuilds preds.jsonl from the records (one line per
        # recorded outcome): mirror that so the graded input matches the plan.
        rows = []
        for iid in instances:
            mf = vdir / iid / "metadata.json"
            try:
                status = json.loads(mf.read_text()).get("status")
            except Exception:
                status = None
            if status not in ci.RESULT_STATUSES:
                continue  # infra / no metadata: the runner would not predict it
            pf = vdir / iid / "patch.diff"
            patch = pf.read_text() if pf.is_file() else ""
            rows.append(json.dumps({"instance_id": iid,
                                    "model_name_or_path": f"label-{variant}",
                                    "model_patch": patch}) + "\n")
        (vdir / "preds.jsonl").write_text("".join(rows))


def write_preds(run_dir: Path, variant: str, rows: list[str]) -> None:
    """Write preds.jsonl verbatim (for fixtures that need a specific shape)."""
    vdir = run_dir / variant
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "preds.jsonl").write_text("".join(r + "\n" for r in rows))


def fake_inspect(mapping):
    def _inspect(tag):
        return mapping.get(tag)
    return _inspect


class TestAnalyzeRun:
    def test_all_ok(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert ok
        assert any(line.startswith("OK") for line in lines)

    def test_mixed_digests_same_tag_fails_even_on_local_hit(self, tmp_path):
        # P0 regression: two digests for one tag inside a run passed the old
        # set-membership check when the local digest matched one of them.
        write_instance(tmp_path, "governor", "task-1", image="img", image_id="sha256:1")
        write_instance(tmp_path, "governor", "task-2", image="img", image_id="sha256:2")
        write_manifest(tmp_path, "governor", ["task-1", "task-2"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img": "sha256:1"}))
        assert not ok
        assert any(line.startswith("MIXED") for line in lines)

    def test_missing_local_image_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({}))
        assert not ok
        assert any(line.startswith("MISSING  img-task-1") for line in lines)

    def test_moved_digest_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:other"}))
        assert not ok
        assert any(line.startswith("MISMATCH") for line in lines)

    def test_planned_instance_without_metadata_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1", "task-2"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any("task-2" in line and "run incomplete" in line for line in lines)

    def test_recorded_outcome_without_image_id_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1", image_id=None)
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({}))
        assert not ok
        assert any(line.startswith("NO-IMAGE") for line in lines)

    def test_planned_infra_status_fails_without_no_image_noise(self, tmp_path):
        # P1 regression: a planned instance that ended in an infra status has
        # no recorded outcome, so grading would silently shrink the
        # denominator. It must FAIL -- but as NO-OUTCOME, not NO-IMAGE (an
        # infra status legitimately has no digest recorded).
        write_instance(tmp_path, "governor", "task-1")
        write_instance(tmp_path, "governor", "task-2", status="setup_error", patch=False,
                       image=None, image_id=None)
        write_manifest(tmp_path, "governor", ["task-1", "task-2"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("NO-OUTCOME") for line in lines)
        assert not any(line.startswith("NO-IMAGE") for line in lines)

    def test_missing_manifest_fails(self, tmp_path):
        # P0 regression: an absent manifest silently skipped the whole
        # completeness check for that variant.
        write_instance(tmp_path, "governor", "task-1")
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("MISSING  governor/manifest.json") for line in lines)

    def test_empty_manifest_instances_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", [])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("EMPTY") for line in lines)

    def test_recorded_outcome_without_patch_file_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1", patch=False)
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("NO-PATCH") for line in lines)

    def test_timeout_partial_with_empty_patch_file_is_fine(self, tmp_path):
        # An empty patch file is a recorded outcome; only a missing file fails.
        write_instance(tmp_path, "governor", "task-1", status="timeout_partial")
        (tmp_path / "governor" / "task-1" / "patch.diff").write_text("")
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert ok
        assert not any(line.startswith("NO-PATCH") for line in lines)

    def test_unplanned_metadata_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_instance(tmp_path, "governor", "task-9")
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(
            tmp_path, fake_inspect({"img-task-1": "sha256:1", "img-task-9": "sha256:1"}))
        assert not ok
        assert any(line.startswith("UNPLANNED") for line in lines)

    def test_unreadable_metadata_fails(self, tmp_path):
        inst = tmp_path / "governor" / "task-1"
        inst.mkdir(parents=True)
        (inst / "metadata.json").write_text("{broken")
        write_instance(tmp_path, "governor", "task-2")
        write_manifest(tmp_path, "governor", ["task-2"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-2": "sha256:1"}))
        assert not ok
        assert any(line.startswith("UNREADABLE") for line in lines)

    def test_no_metadata_at_all_fails(self, tmp_path):
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({}))
        assert not ok
        assert any("no */<instance>/metadata.json" in line for line in lines)

    def test_two_variants_two_tags_all_ok(self, tmp_path):
        write_instance(tmp_path, "vanilla", "task-1", image="img1", image_id="d1")
        write_instance(tmp_path, "governor", "task-1", image="img2", image_id="d2")
        write_manifest(tmp_path, "vanilla", ["task-1"])
        write_manifest(tmp_path, "governor", ["task-1"])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img1": "d1", "img2": "d2"}))
        assert ok

    def test_missing_preds_file_fails(self, tmp_path):
        # P1 regression: the evaluator grades preds.jsonl; a run without it
        # cannot be graded no matter how complete the instance records look.
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"], with_preds=False)
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("MISSING  governor/preds.jsonl") for line in lines)

    def test_stale_preds_fail(self, tmp_path):
        # patch.diff changed after preds.jsonl was generated: the evaluator
        # would submit the stale model_patch, not the recorded one.
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        (tmp_path / "governor" / "task-1" / "patch.diff").write_text("diff --git a/y b/y\n")
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("STALE-PREDS") for line in lines)

    def test_missing_preds_line_for_recorded_outcome_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_instance(tmp_path, "governor", "task-2")
        write_manifest(tmp_path, "governor", ["task-1", "task-2"])
        write_preds(tmp_path, "governor", [
            json.dumps({"instance_id": "task-1", "model_name_or_path": "label-governor",
                        "model_patch": "diff --git a/x b/x\n"})])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect(
            {"img-task-1": "sha256:1", "img-task-2": "sha256:1"}))
        assert not ok
        assert any(line.startswith("NO-PRED") and "task-2" in line for line in lines)

    def test_duplicate_and_extra_preds_fail(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        line = json.dumps({"instance_id": "task-1", "model_name_or_path": "label-governor",
                           "model_patch": "diff --git a/x b/x\n"})
        extra = json.dumps({"instance_id": "task-9", "model_name_or_path": "label-governor",
                            "model_patch": ""})
        write_preds(tmp_path, "governor", [line, line, extra])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("DUP-PREDS") for line in lines)
        assert any(line.startswith("EXTRA-PRED") for line in lines)

    def test_bad_preds_line_fails(self, tmp_path):
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        write_preds(tmp_path, "governor", ['{"torn'])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any(line.startswith("BAD-PREDS") for line in lines)

    def test_preds_blank_line_fails(self, tmp_path):
        # P12 regression: the evaluator parses preds with one json.loads per
        # line (harness/utils.py, no strip, no skip) -- a single blank line
        # aborts loading the whole file, so it must not pass the checker.
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        line = json.dumps({"instance_id": "task-1",
                           "model_name_or_path": "label-governor",
                           "model_patch": "diff --git a/x b/x\n"})
        write_preds(tmp_path, "governor", [line, ""])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any("blank line" in line for line in lines)

    def test_preds_missing_model_name_fails(self, tmp_path):
        # P12 regression: the report stage reads model_name_or_path with a
        # direct subscript after every instance has run -- a row without it
        # crashes grading at the most expensive possible point.
        write_instance(tmp_path, "governor", "task-1")
        write_manifest(tmp_path, "governor", ["task-1"])
        write_preds(tmp_path, "governor", [
            json.dumps({"instance_id": "task-1",
                        "model_patch": "diff --git a/x b/x\n"})])
        lines, ok = ci.analyze_run(tmp_path, fake_inspect({"img-task-1": "sha256:1"}))
        assert not ok
        assert any("model_name_or_path" in line for line in lines)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
