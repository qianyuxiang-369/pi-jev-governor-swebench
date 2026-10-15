#!/usr/bin/env python3
"""Pi SWE-bench Runner (P4/P5).

Runs SWE-bench Verified instances with Pi inside the official SWE-bench
container image, optionally loading the pi-jev-governor extension, then
collects the working-tree diff as the prediction patch.

Key behaviors (see README.md for the full design):
  * Model scope: governor runs pass all three tier models as the pi --models
    scope and the initial model via --model; the extension can only switch
    within scope, so a single-model scope would silently disable routing.
  * Status machine: every instance ends in success / timeout_partial /
    agent_error / setup_error / collection_error / extension_bypass
    (plus dry_run_ok / dry_run_probe_failed in --dry-run). Recorded outcomes
    (success, timeout_partial, agent_error) always land in preds.jsonl -- an
    empty patch is itself an outcome (the evaluator records it as an empty
    patch), so the denominator matches the planned instance set. Only the
    infrastructure statuses (setup_error, collection_error, extension_bypass)
    are retried symmetrically and never written to preds.
  * Governor validity: a governor run is only valid when decision logs exist.
    A silently skipped extension (pi keeps going without it) is marked
    extension_bypass instead of success.
  * Result layout is isolated per variant: <output>/<run-id>/<variant>/...
  * Timeouts stop the whole container (kill the full process tree) before
    collecting the patch, so no pytest/build child can mutate the diff.

Reuses the transfer channel validated in P2: on this docker-rt host, bind
mounts and `docker cp` silently fail, so every file goes into the container
via `docker exec -i ... bash -c 'cat > <path>' < local-file`.

Usage:
  python run.py --variant governor --instance-id astropy__astropy-14182
  python run.py --variant vanilla --variant governor --limit 2
  python run.py --variant governor --instance-id X --dry-run
  python run.py --variant vanilla --variant governor --sample 30 --seed 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parents[1]
DEFAULT_ASSETS = EVAL_DIR / "container-assets"
FALLBACK_ASSETS = Path("/workspace/work/mjy/xqy_swe/.smoke-repo/docker-inject")

DATASET = "princeton-nlp/SWE-bench_Verified"
SPLIT = "test"
IMAGE_PREFIX = "swebench/sweb.eval.x86_64."
AGENT_DIR = "/root/agent"
PROMPTS_DIR = f"{AGENT_DIR}/prompts"
PI_PKG_REL = "pi/node_modules/@earendil-works/pi-coding-agent"
CLI_REL = f"{PI_PKG_REL}/dist/bundle/cli.js"
MODEL_RE = re.compile(r"^[A-Za-z0-9._:@/-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TIERS = ("small", "normal", "strong")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# Recorded outcomes: always written to preds.jsonl (empty patches included).
RESULT_STATUSES = frozenset({"success", "timeout_partial", "agent_error"})
# Infrastructure failures: no valid measurement, retried symmetrically, never
# written to preds (a run without the governor is not a vanilla datapoint).
INFRA_STATUSES = frozenset({"setup_error", "collection_error", "extension_bypass"})
RETRYABLE_STATUSES = INFRA_STATUSES
MAX_INFRA_RETRIES = 2
# Statuses that a --resume run treats as final (no rerun): recorded outcomes
# plus a successful dry-run check.
RESUME_TERMINAL_STATUSES = RESULT_STATUSES | {"dry_run_ok"}
# Allowlist for .env.local keys that reach the container; unrelated secrets in
# the file must not be exposed to task containers.
ENV_ALLOWED_PREFIXES = ("PI_JEV_",)
ENV_ALLOWED_KEYS = frozenset({"OPENROUTER_API_KEY"})
# Runtime artifacts that must never end up in the prediction patch. Appended to
# .git/info/exclude (inside .git, so it cannot leak into the diff) and only
# affects untracked files.
EXCLUDE_ENTRIES = ("__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/")
PATCH_SIZE_WARN = 5 * 1024 * 1024
PROBE_KEY = "sk-probe-invalid"
# Structured auth-failure evidence: matched only against errorMessage-shaped
# fields of pi events, never the raw mixed output ("api key"/"credential"
# substrings appear in benign text and used to let a degraded run pass).
PROBE_AUTH_RE = re.compile(r"\b401\b|unauthorized", re.IGNORECASE)
# pi JSON event types that prove the agent loop actually started. Known names
# plus suffixed families: a pi version bump should not kill the probe, but
# arbitrary JSON from an unrelated program must not count as evidence.
PI_PROBE_EVENT_TYPES = frozenset({
    "message_end", "agent_settled", "agent_start", "agent_end", "error", "custom_entry",
})


class InfraError(RuntimeError):
    """Docker/container lifecycle failure unrelated to the model's behavior.

    Without a dedicated type, a docker failure during the agent phase would be
    classified agent_error (a model failure) and land in preds. InfraError maps
    to the retryable infrastructure statuses instead.
    """


def log(msg: str) -> None:
    print(f"[run] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file + os.replace: a crash can leave the old content or
    the new content, never a torn file."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode())


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2) + "\n")


# ---------------------------------------------------------------- assets/env


def parse_env_local(path: Path) -> dict[str, str]:
    """Parse the repo's .env.local (shell `export KEY="value"` lines)."""
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        env[key.strip()] = val
    return env


def strip_provider(ref: str) -> str:
    """`openrouter:qwen/...` -> `qwen/...`: pi --model/--models take the bare id."""
    return ref.strip().removeprefix("openrouter:")


def load_tiers(env_kv: dict[str, str]) -> dict[str, str]:
    """{tier: bare-model-id} for PI_JEV_MODEL_{SMALL,NORMAL,STRONG} that are set."""
    tiers: dict[str, str] = {}
    for tier in TIERS:
        raw = env_kv.get(f"PI_JEV_MODEL_{tier.upper()}", "")
        if raw.strip():
            tiers[tier] = strip_provider(raw)
    return tiers


def filter_env(env_kv: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Keep only allow-listed keys so unrelated secrets never enter a container."""
    kept = {
        k: v for k, v in env_kv.items()
        if k in ENV_ALLOWED_KEYS or k.startswith(ENV_ALLOWED_PREFIXES)
    }
    dropped = sorted(k for k in env_kv if k not in kept)
    return kept, dropped


def build_scope(variant: str, model: str, tiers: dict[str, str]) -> str:
    """pi --models scope for one variant.

    Governor: every tier plus the initial model -- pi only switches within the
    scope, and an out-of-scope target is silently refused (`not_scoped` in the
    decision log), so a narrower scope would disable routing without an error.
    """
    if variant != "governor":
        return model
    members = [model, *(tiers[t] for t in TIERS if tiers.get(t))]
    return ",".join(dict.fromkeys(members))


def validate_run_id(run_id: str) -> None:
    # "." / ".." pass the character check but would redirect the run root (and
    # its manifests) outside the output directory.
    if run_id in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        sys.exit(f"refusing run-id {run_id!r}: allowed characters are [A-Za-z0-9._-]")


def resolve_assets(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else [DEFAULT_ASSETS, FALLBACK_ASSETS]
    for cand in candidates:
        if (cand / "inject.tar.gz").is_file() and (cand / "pi-deps.tar.gz").is_file():
            if cand == FALLBACK_ASSETS:
                log(f"note: using fallback assets dir {cand}")
            return cand
    sys.exit(
        f"assets not found (looked at: {', '.join(str(c) for c in candidates)}); "
        f"run: bash {EVAL_DIR}/build-assets.sh"
    )


# ---------------------------------------------------------------- docker io


def docker(*args: str, timeout: float | None = None, check: bool = True):
    return subprocess.run(
        ["docker", *args], capture_output=True, timeout=timeout, check=check
    )


def docker_server_version() -> str | None:
    try:
        return docker(
            "version", "--format", "{{.Server.Version}}", timeout=30
        ).stdout.decode().strip()
    except Exception:
        return None


def docker_exec(name: str, cmd: str, timeout: float | None = None, check: bool = False):
    return subprocess.run(
        ["docker", "exec", name, "bash", "-c", cmd],
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def docker_upload_bytes(name: str, data: bytes, dest: str, timeout: float | None = None) -> None:
    subprocess.run(
        ["docker", "exec", "-i", name, "bash", "-c", f"cat > {dest}"],
        input=data,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


def docker_upload(name: str, local: Path, dest: str, timeout: float | None = None) -> None:
    docker_upload_bytes(name, local.read_bytes(), dest, timeout=timeout)


# ---------------------------------------------------------------- dataset


def load_instances(ids: list[str], limit: int | None, sample: int | None, seed: int,
                   revision: str | None = None):
    from datasets import load_dataset

    kwargs = {"split": SPLIT}
    if revision:
        kwargs["revision"] = revision
    ds = load_dataset(DATASET, **kwargs)
    rows = {row["instance_id"]: row for row in ds}
    if ids:
        missing = [i for i in ids if i not in rows]
        if missing:
            sys.exit(f"instance_id not found in {DATASET}: {missing}")
        return [rows[i] for i in ids]
    if sample is not None:
        all_rows = list(ds)
        return random.Random(seed).sample(all_rows, min(sample, len(all_rows)))
    if limit is not None:
        # First N of the dataset: a smoke-test convenience, NOT a representative
        # sample. Use --sample with --seed for pilots.
        return [row for row in ds][:limit]
    return [row for row in ds][:1]


def resolve_dataset_revision() -> str | None:
    """Commit sha of the dataset's current main revision (best effort).

    Pinning the revision keeps two runs comparable when the upstream dataset
    is updated; the resolved sha is recorded in the manifest.
    """
    try:
        from huggingface_hub import HfApi
        return HfApi().dataset_info(DATASET).sha
    except Exception as exc:
        log(f"warn: dataset revision lookup failed ({type(exc).__name__}); using default revision")
        return None


def evaluator_version() -> str:
    try:
        from importlib.metadata import version as pkg_version
        return f"swebench=={pkg_version('swebench')}"
    except Exception:
        return "unknown"


def dataset_fingerprint(instances: list[dict]) -> str:
    """sha256 over the ordered (instance_id, base_commit) pairs.

    Pins which dataset rows the run actually executed: unlike the revision sha
    it also catches partial re-loads and upstream edits at an identical sha.
    """
    return hashlib.sha256(json.dumps(
        [[i["instance_id"], i["base_commit"]] for i in instances]
    ).encode()).hexdigest()


def runner_sha256() -> str:
    """sha256 of this runner file, pinned inside the run spec.

    The eval tree is not committed (git tracks no eval/ files), so no commit
    sha can pin the execution logic: resuming a run whose runner was modified
    would silently mix two versions of the measurement code under one
    config_hash. Hashing the file makes --resume refuse that.
    """
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def build_run_spec(*, variant: str, model: str, tiers: dict[str, str],
                   instances: list[dict], dataset_rev: str | None, args,
                   asset_sha: str, pi_deps_sha: str, append_prompt_sha: str,
                   template_sha: str, env_kv: dict[str, str], runner_sha: str) -> dict:
    """The immutable input contract of a run (one dict per variant).

    Everything that can change a measured number is pinned here: dataset
    revision + row fingerprint, model scope, tiers, thinking/timeout, prompt
    and asset hashes, dry_run, the governor knobs that reach the container
    (filtered env minus credentials), and the runner script's own hash.
    config_hash is derived from this dict, and --resume refuses to continue
    when it differs.
    """
    kept, _ = filter_env(env_kv)
    jev_env = {k: v for k, v in sorted(kept.items()) if "API_KEY" not in k.upper()}
    return {
        "dataset": f"{DATASET}:{SPLIT}",
        "dataset_revision": dataset_rev or "unresolved(default)",
        "dataset_fingerprint": dataset_fingerprint(instances),
        "instances": [i["instance_id"] for i in instances],
        "variant": variant,
        "model": model,
        "model_scope": build_scope(variant, model, tiers),
        "tier_models": tiers if variant == "governor" else {},
        "thinking_level": args.thinking,
        "timeout_s": args.timeout,
        "dry_run": bool(args.dry_run),
        "allow_unpinned_dataset": bool(args.allow_unpinned_dataset),
        "assets": {"inject_tar_sha256": asset_sha, "pi_deps_tar_sha256": pi_deps_sha},
        "prompts": {"append_prompt_sha256": append_prompt_sha,
                    "instance_template_sha256": template_sha},
        "jev_env": jev_env,
        "runner_sha256": runner_sha,
    }


def compute_config_hash(spec: dict) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def describe_spec_diff(old: dict, new: dict) -> str:
    """Human-readable summary of changed spec keys (for a refusal message)."""
    changed = sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))
    if not changed:
        return "no spec field differs (hash mismatch is unexplained)"
    shown = ", ".join(changed[:6]) + (" ..." if len(changed) > 6 else "")
    return f"changed field(s): {shown}"


# ---------------------------------------------------------------- container


def container_name(instance_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", instance_id)[-40:]
    return f"pi-jev-{safe}-{os.getpid() % 0xFFFF:04x}"


def setup_container(inst: dict, env_kv: dict[str, str], assets: Path, tmp_root: Path,
                    keep: bool = False) -> dict:
    """Create the container, inject assets/prompts, verify versions and base commit.

    Every failure after the container is created cleans up first: run_one never
    receives the name from a failed setup, so its finally block cannot delete
    the container -- without this, a crashed setup leaks containers silently.
    """
    iid = inst["instance_id"]
    base_commit = inst["base_commit"]
    if not COMMIT_RE.match(base_commit):
        raise RuntimeError(f"dataset row has an unexpected base_commit: {base_commit!r}")
    image = IMAGE_PREFIX + iid.replace("__", "_1776_")
    name = container_name(iid)

    image_id = docker("image", "inspect", image, "--format", "{{.Id}}", timeout=60).stdout.decode().strip()

    env_dir = Path(tempfile.mkdtemp(prefix="pi-jev-env-", dir=tmp_root))
    try:
        env_file = env_dir / "container.env"
        merged, dropped = filter_env(env_kv)
        if dropped:
            log(f"env: dropping non-allowlisted keys: {dropped}")
        merged["PI_JEV_LOG_DIR"] = "/root/jev-logs"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in merged.items()))
        env_file.chmod(0o600)

        docker("rm", "-f", name, check=False, timeout=120)
        docker("create", "--name", name, "--env-file", str(env_file), "-w", "/testbed", image, timeout=120)
        docker("start", name, timeout=120)
        time.sleep(1)
        log(f"container {name} started (image {image})")

        docker_upload(name, assets / "inject.tar.gz", "/root/inject.tar.gz")
        docker_upload(name, assets / "pi-deps.tar.gz", "/root/pi-deps.tar.gz")
        docker_exec(
            name,
            f"mkdir -p {AGENT_DIR} && cd {AGENT_DIR} && tar xzf /root/inject.tar.gz",
            check=True,
        )
        docker_exec(
            name,
            f"cd {AGENT_DIR}/{PI_PKG_REL} && tar xzf /root/pi-deps.tar.gz",
            check=True,
        )
        docker_exec(name, f"mkdir -p {PROMPTS_DIR}", check=True)

        docker_upload(name, EVAL_DIR / "prompts" / "append-system-prompt.md",
                      f"{PROMPTS_DIR}/append-system-prompt.md")
        task_text = (EVAL_DIR / "prompts" / "instance-prompt.md").read_text().replace(
            "{{problem_statement}}", inst["problem_statement"]
        )
        docker_upload_bytes(name, task_text.encode(), f"{PROMPTS_DIR}/task.md")

        wrapper = (
            "#!/bin/bash\n"
            "export BASH_ENV=/root/.bashrc\n"
            f"exec {AGENT_DIR}/node {AGENT_DIR}/{CLI_REL} \"$@\"\n"
        )
        docker_upload_bytes(name, wrapper.encode(), "/root/pi.sh")
        docker_exec(name, "chmod +x /root/pi.sh", check=True)

        node_v = docker_exec(name, f"{AGENT_DIR}/node --version", check=True).stdout.decode().strip()
        pi_v = docker_exec(name, "/root/pi.sh --version", check=True).stdout.decode().strip()

        # The image HEAD is a "SWE-bench" commit on top of the dataset
        # base_commit (it carries image setup changes, e.g. mode bits). The
        # evaluator applies the prediction patch directly to a fresh container
        # spawned from this same image -- it never checks out base_commit
        # first -- so HEAD, not base_commit, is the correct patch baseline.
        # Verify base_commit is an ancestor to prove the image was built from
        # the right commit, and keep HEAD as the baseline.
        head = docker_exec(name, "cd /testbed && git rev-parse HEAD", check=True).stdout.decode().strip()
        ancestor = docker_exec(
            name, f"cd /testbed && git merge-base --is-ancestor {base_commit} HEAD && echo ok"
        ).stdout.decode().strip()
        if ancestor != "ok":
            raise RuntimeError(
                f"dataset base_commit {base_commit} is not an ancestor of testbed HEAD {head}"
            )
        dirty = docker_exec(name, "cd /testbed && git status --porcelain").stdout.decode().strip()
        if dirty:
            raise RuntimeError(f"testbed worktree is not clean: {dirty[:200]}")

        return {
            "name": name,
            "image": image,
            "image_id": image_id,
            "node_version": node_v,
            "pi_version": pi_v,
            "base_commit": base_commit,
            "image_head": head,
            "task_sha256": hashlib.sha256(task_text.encode()).hexdigest(),
            "env_dir": env_dir,
        }
    except BaseException:
        if keep:
            log(f"setup failed; keeping container {name} for debugging (--keep-container)")
        else:
            # Never raises: the original setup error must not be replaced.
            cleanup_container(name, keep=False)
        shutil.rmtree(env_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------- probe


def _iter_probe_events(out: str) -> list[dict]:
    """Parsed pi events from the merged probe output.

    Only type-bearing JSON objects count (a bare "{}" or arbitrary JSON from
    an unrelated program must not pass as evidence). Type matching is
    deliberately broad: known names plus suffixed families, so a pi version
    bump does not kill the probe.
    """
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if not isinstance(etype, str) or not etype:
            continue
        if (etype in PI_PROBE_EVENT_TYPES or etype.endswith(("_end", "_start", "_error"))
                or "error" in etype):
            events.append(event)
    return events


def _event_error_strings(event: dict) -> list[str]:
    """errorMessage-shaped string fields of a pi event.

    message_end carries message.errorMessage; error events may carry a nested
    error object instead. Matching happens only on these structured fields,
    never on the raw mixed output.
    """
    strings: list[str] = []
    containers = [event]
    message = event.get("message")
    if isinstance(message, dict):
        containers.append(message)
        if isinstance(message.get("error"), dict):
            containers.append(message["error"])
    if isinstance(event.get("error"), dict):
        containers.append(event["error"])
    for container in containers:
        for key, val in container.items():
            if isinstance(val, str) and "error" in key.lower():
                strings.append(val)
    return strings


def analyze_probe_output(variant: str, returncode: int | None, out: str,
                         jev_text: str = "", jev_problems: list[str] | None = None) -> list[str]:
    """Judge a zero-cost probe from structural evidence. Returns a problem list.

    The probe must show, in order of strength:
      * a working pi run: parsable type-bearing pi JSON events;
      * governor only: >= 1 parsable route record in the container's decision
        logs -- a degraded configuration (bad tier/URL/thresholds) still
        writes a jev-state entry but never decision records (log() bails out
        early), so the route record is the real "governor took effect" test;
      * a structured auth failure (401/unauthorized in an errorMessage-shaped
        field), proving the run reached the provider auth step with the fake key.
    A bare "no known error string" check is not enough: an unrelated fatal
    error (rc=127, broken wrapper) or a degraded run passed the old matching.
    """
    problems = list(jev_problems or [])
    if "Failed to load extension" in out or "Extension error" in out:
        problems.append("extension failed to load")
    if "No models match pattern" in out:
        problems.append("model scope pattern did not match")
    if re.search(r'Model "[^"]+" not found', out):
        problems.append("model not found")
    if returncode is not None and returncode != 0:
        # The real probe exits 0. Any non-zero rc fails outright: an intact
        # evidence chain no longer excuses it (pi may still die from an
        # unrelated cause after printing the structured 401).
        if returncode in (126, 127, 137):
            problems.append(f"pi wrapper died at shell level (rc={returncode})")
        else:
            problems.append(f"non-zero probe exit (rc={returncode})")
    events = _iter_probe_events(out)
    if not events:
        problems.append("no parsable JSON events: pi never reached the agent loop")
    route_records = 0
    jev_parse_errors = 0
    for line in jev_text.splitlines():
        try:
            record = json.loads(line)
        except Exception:
            jev_parse_errors += 1
            continue
        if isinstance(record, dict) and record.get("decisionKind") == "route":
            route_records += 1
    if variant == "governor":
        if route_records == 0:
            problems.append("no route decision record: governor did not take effect "
                            "(degraded config or extension not loaded)")
        elif jev_parse_errors:
            problems.append(f"{jev_parse_errors} unparsable decision log line(s)")
    auth_evidence = [s for event in events for s in _event_error_strings(event)
                     if PROBE_AUTH_RE.search(s)]
    if not auth_evidence:
        problems.append("no auth-failure evidence: probe did not reach the provider auth step")
    return problems


def run_probe(name: str, variant: str, scope: str, model: str, timeout: int = 180,
              thinking: str = "medium") -> dict:
    """Zero-cost pre-flight inside the container: extension load + model resolution.

    Runs pi with the real model scope but an invalid API key, so the run dies at
    the provider auth step without spending anything. The verdict (see
    analyze_probe_output) is a structural evidence chain: pi JSON events, the
    container's decision records for governor runs (a degraded config writes
    none), and the structured 401 of the failing provider call.
    """
    cmd = "/root/pi.sh"
    if variant == "governor":
        cmd += f" -e {AGENT_DIR}/governor"
    cmd += f" --mode json --no-session --no-approve --thinking {thinking} --models {scope} --model {model} -p probe"

    def verdict(rc: int | None, output: str, jev_text: str,
                jev_problems: list[str]) -> dict:
        problems = analyze_probe_output(variant, rc, output, jev_text, jev_problems)
        return {
            "ok": not problems,
            "problems": problems,
            "returncode": rc,
            "output": output[:200_000],
        }

    try:
        result = subprocess.run(
            [
                "docker", "exec",
                "-e", f"OPENROUTER_API_KEY={PROBE_KEY}",
                "-e", f"PI_JEV_API_KEY={PROBE_KEY}",
                name, "bash", "-c", cmd,
            ],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "problems": [f"probe timed out after {timeout}s"],
            "returncode": None,
            "output": "",
        }
    out = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
    jev_text, jev_problems = "", []
    if variant == "governor":
        try:
            jev_text, _files, jev_problems = read_decision_logs(name)
        except Exception as exc:
            jev_problems = [f"decision log read raised: {type(exc).__name__}: {exc}"]
    return verdict(result.returncode, out, jev_text, jev_problems)


# ---------------------------------------------------------------- launch


def restart_container(name: str) -> bool:
    """Stop the container (kills the whole process tree), then start it again.

    Both steps are verified: a failed stop would leave child processes writing
    to the worktree while the patch is collected, so the caller must know when
    the container never came to rest.

    Never raises: this runs inside launch_pi's TimeoutExpired handler, where an
    escaping docker failure (stop/inspect timeout, CLI error) would bypass the
    InfraError channel and land in run_one_attempt's generic handler as a
    phantom agent_error written into preds. Any problem returns False, which
    the caller maps to an infrastructure failure.
    """
    try:
        stopped = False
        for attempt in (1, 2):
            res = docker("stop", "-t", "5", name, check=False, timeout=120)
            running = docker(
                "inspect", "-f", "{{.State.Running}}", name, check=False, timeout=30
            ).stdout.decode().strip()
            if running == "false":
                stopped = True
                break
            log(f"stop attempt {attempt} failed (rc={res.returncode}, running={running!r})")
        if not stopped:
            log(f"warn: could not stop container {name}")
            return False
        try:
            docker("start", name, timeout=90)
        except Exception as exc:
            log(f"warn: container restart failed: {exc}")
            return False
        running = docker(
            "inspect", "-f", "{{.State.Running}}", name, check=False, timeout=30
        ).stdout.decode().strip()
        if running != "true":
            log(f"warn: container did not come back up (running={running!r})")
            return False
        time.sleep(1)
        return True
    except Exception as exc:
        # Deliberately broad: the only contract this helper has with its
        # caller is True/False, and False is the fail-closed answer.
        log(f"warn: container {name} stop/restart failed at the docker level: "
            f"{type(exc).__name__}: {exc}")
        return False


def launch_pi(name: str, variant: str, model: str, scope: str, timeout: int,
              thinking: str = "medium"):
    cmd = "/root/pi.sh"
    if variant == "governor":
        cmd += f" -e {AGENT_DIR}/governor"
    cmd += f" --mode json --no-session --no-approve --thinking {thinking} --models {scope} --model {model}"
    cmd += f" --append-system-prompt {PROMPTS_DIR}/append-system-prompt.md"
    cmd += f' -p "$(cat {PROMPTS_DIR}/task.md)"'
    inner = f"{cmd} > /root/run.log 2>&1"

    timed_out = False
    exit_code = None
    container_stable = True
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["docker", "exec", name, "bash", "-c", inner], capture_output=True, timeout=timeout
        )
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        log(f"timeout after {timeout}s; stopping container to kill the full process tree")
        # docker stop terminates every process in the container (pytest/build
        # children included); start restores the writable layer for a stable
        # patch collection. A container that never stopped must not be diffed.
        container_stable = restart_container(name)
    except (OSError, subprocess.SubprocessError) as exc:
        # docker itself failed (daemon gone, container removed): this measures
        # nothing about the model, so it must not become an agent_error.
        raise InfraError(f"docker exec failed: {type(exc).__name__}: {exc}") from exc
    return exit_code, timed_out, time.monotonic() - started, container_stable


# ---------------------------------------------------------------- collect


def collect_patch(name: str, baseline: str) -> str:
    # Baseline = the image's original HEAD, NOT the dataset base_commit:
    # diffing against base_commit would drag the image's own setup changes
    # (this image carries 1862 mode changes + an env tweak) into the prediction.
    # The fixed baseline also survives a stray agent commit (its content still
    # shows up in index-vs-baseline), where `diff --cached HEAD` would go empty.
    entries = " ".join(f"'{p}'" for p in EXCLUDE_ENTRIES)
    cmd = (
        f"cd /testbed && printf '%s\\n' {entries} >> .git/info/exclude && "
        f"git add -A && git diff --cached --binary --full-index {baseline}"
    )
    result = docker_exec(name, cmd, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"patch collection failed (rc={result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')[:300]}"
        )
    return result.stdout.decode("utf-8", "replace")


def collect_run_log(name: str, dest: Path) -> None:
    result = docker_exec(name, "cat /root/run.log", timeout=180)
    if result.returncode != 0:
        # A read failure here would silently turn into "no agent output" and a
        # bogus agent_error verdict: fail as infrastructure instead.
        raise InfraError(
            f"failed to read /root/run.log (rc={result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')[:200]}")
    atomic_write_bytes(dest, result.stdout)


def read_decision_logs(name: str) -> tuple[str, int, list[str]]:
    """Read /root/jev-logs/*.jsonl from the container.

    Returns (combined_text, file_count, errors). A missing directory is normal
    (vanilla / degraded governor write no decisions), so only per-file read
    failures are reported as errors.
    """
    listing = docker_exec(name, "ls -1 /root/jev-logs 2>/dev/null || true", timeout=60)
    if listing.returncode != 0:
        return "", 0, [f"ls /root/jev-logs failed (rc={listing.returncode})"]
    files = [f for f in listing.stdout.decode("utf-8", "replace").split()
             if f.endswith(".jsonl") and re.fullmatch(r"[A-Za-z0-9._:-]+", f)]
    chunks: list[str] = []
    errors: list[str] = []
    for file_name in sorted(files):
        res = docker_exec(name, f"cat /root/jev-logs/{file_name}", timeout=60)
        if res.returncode != 0:
            errors.append(f"cat {file_name} failed (rc={res.returncode})")
            continue
        chunks.append(res.stdout.decode("utf-8", "replace"))
    return "".join(chunks), len(files), errors


def collect_decisions(name: str, jev_dir: Path) -> tuple[int, list[str]]:
    """Persist the decision log; returns (file_count, read_errors).

    The file is always (re)written, even when this attempt produced no
    decisions at all: the instance directory is reused across retries, so
    skipping the write would leave the previous attempt's decisions.jsonl in
    place and parse_decisions would read its stale route records as this
    attempt's governor evidence, defeating the route>=1 validity gate.
    """
    text, files, errors = read_decision_logs(name)
    jev_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(jev_dir / "decisions.jsonl", text)
    return files, errors


def cleanup_container(name: str, keep: bool) -> None:
    """Never raises: this runs in a finally block, where an escaping docker
    failure would abort the whole batch and drop the attempt's result. A
    container that cannot be removed is logged and left for manual cleanup."""
    if keep:
        log(f"keeping container {name}")
        return
    try:
        docker("rm", "-f", name, check=False, timeout=120)
    except Exception as exc:
        log(f"warn: removing container {name} failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- metrics


def parse_pi_log(path: Path) -> dict:
    stats: dict = {
        "model_calls": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "models": {},
        "stop_reasons": {},
        "stop_sequence": [],
        "final_stop_reason": None,
        "agent_settled": False,
        "aborted": False,
        "extension_errors": 0,
        "parse_errors": 0,
        "events": 0,
    }
    if not path.is_file():
        return stats
    text = path.read_text(errors="replace")
    stats["extension_errors"] = text.count("Failed to load extension") + text.count("Extension error")
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            stats["parse_errors"] += 1
            continue
        stats["events"] += 1
        etype = event.get("type")
        if etype == "agent_settled":
            stats["agent_settled"] = True
            stats["aborted"] = bool(event.get("aborted"))
            continue
        if etype != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        stats["model_calls"] += 1
        ref = f"{message.get('provider', '?')}:{message.get('model', '?')}"
        stats["models"][ref] = stats["models"].get(ref, 0) + 1
        sr = str(message.get("stopReason") or "?")
        stats["stop_reasons"][sr] = stats["stop_reasons"].get(sr, 0) + 1
        # Ordered record: the final element drives the success/error verdict.
        stats["stop_sequence"].append(sr)
        stats["final_stop_reason"] = sr
        usage = message.get("usage") or {}
        cost = usage.get("cost")
        if isinstance(cost, dict):
            cost = cost.get("total")
        if isinstance(cost, (int, float)):
            stats["cost_usd"] += cost
        for field, target in (("input", "input_tokens"), ("output", "output_tokens"),
                              ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                              ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                              ("cacheRead", "cache_read_tokens"), ("cacheWrite", "cache_write_tokens")):
            val = usage.get(field)
            if isinstance(val, (int, float)):
                stats[target] += int(val)
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return stats


def parse_decisions(path: Path) -> dict:
    stats: dict = {
        "route": 0, "permission": 0, "outcome": 0,
        "permission_allow": 0, "permission_block": 0,
        "route_tiers": {}, "route_effective_tiers": {}, "route_kept": 0,
        "route_skips": {}, "route_degradations": {},
        "jev_cost_usd": 0.0, "jev_input_tokens": 0, "jev_output_tokens": 0, "jev_latency_ms": 0,
        "parse_errors": 0,
    }
    if not path.is_file():
        return stats
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            stats["parse_errors"] += 1
            continue
        kind = record.get("decisionKind")
        if kind in ("route", "permission", "outcome"):
            stats[kind] += 1
        if kind == "permission":
            effective = str(record.get("effective") or "")
            if effective.startswith("allow"):
                stats["permission_allow"] += 1
            elif effective.startswith("block"):
                stats["permission_block"] += 1
        if kind == "route":
            answers = record.get("answers") or {}
            tier_answer = (answers.get("tier") or {}).get("value")
            if tier_answer in TIERS:
                stats["route_tiers"][tier_answer] = stats["route_tiers"].get(tier_answer, 0) + 1
            # effective looks like: "phase=executing; tier=small (skipped: not_scoped); <degradation>; ..."
            effective = str(record.get("effective") or "")
            for part in effective.split("; "):
                if part.startswith("phase=") or not part:
                    continue
                skip = re.match(r"^tier=(\w+) \(skipped: ([^)]+)\)$", part)
                if skip:
                    reason = skip.group(2)
                    stats["route_skips"][reason] = stats["route_skips"].get(reason, 0) + 1
                elif part == "tier=kept":
                    stats["route_kept"] += 1
                else:
                    applied = re.match(r"^tier=(\w+)$", part)
                    if applied:
                        stats["route_effective_tiers"][applied.group(1)] = \
                            stats["route_effective_tiers"].get(applied.group(1), 0) + 1
                    else:
                        stats["route_degradations"][part] = \
                            stats["route_degradations"].get(part, 0) + 1
        usage = record.get("usage") or {}
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            stats["jev_cost_usd"] += cost
        for field, target in (("input_tokens", "jev_input_tokens"), ("output_tokens", "jev_output_tokens")):
            val = usage.get(field)
            if isinstance(val, (int, float)):
                stats[target] += int(val)
        lat = record.get("latencyMs")
        if isinstance(lat, (int, float)):
            stats["jev_latency_ms"] += int(lat)
    stats["jev_cost_usd"] = round(stats["jev_cost_usd"], 6)
    return stats


def classify_status(variant: str, timed_out: bool, patch: str,
                    log_stats: dict, jev_stats: dict,
                    collection_errors: int = 0,
                    exit_code: int | None = None) -> tuple[str, list[str]]:
    """Verdict for one attempt. Validity gates run before outcome gates.

    A governor measurement is only valid with a parseable decision record
    (route >= 1), no extension errors, no unparsable decision lines and no
    artifact collection errors; a vanilla measurement needs pi output at all.
    Invalid measurements map to infrastructure statuses (retryable, never in
    preds) and are checked before the timeout/exit-code branches, so a
    silently bypassed governor cannot slip into preds as timeout_partial.
    """
    notes = []
    if log_stats.get("extension_errors"):
        notes.append(f"{log_stats['extension_errors']} extension-error line(s) in run.log")
    if variant == "governor":
        # Extension load failures are silently skipped by pi, and a degraded
        # config (unparsable tiers/URL/thresholds) still writes jev-state but
        # no decision records at all (log() bails out early): the route record
        # is the only reliable proof that the governor actually took effect.
        if log_stats.get("extension_errors"):
            return "extension_bypass", notes + \
                ["extension error(s) in run.log: governor run invalid"]
        if jev_stats.get("route", 0) == 0:
            return "extension_bypass", notes + \
                ["no route decision records: governor did not take effect"]
        if jev_stats.get("parse_errors", 0):
            return "collection_error", notes + \
                [f"{jev_stats['parse_errors']} unparsable decision log line(s)"]
    if collection_errors:
        return "collection_error", notes + \
            [f"{collection_errors} artifact collection error(s)"]
    if log_stats.get("events", 0) == 0:
        # No pi JSON event at all: the agent never produced output (wrapper or
        # container problem), so neither timeout nor model failure applies.
        return "collection_error", notes + \
            ["no pi JSON events in run.log: the agent never started"]
    if timed_out:
        if not patch.strip():
            notes.append("timeout with empty patch (recorded as an empty patch)")
        return "timeout_partial", notes
    if exit_code not in (None, 0):
        return "agent_error", notes + [f"pi exited with code {exit_code}"]
    if not log_stats.get("agent_settled"):
        return "agent_error", notes + ["no agent_settled event in run.log"]
    if log_stats.get("aborted"):
        return "agent_error", notes + ["agent_settled reports aborted=true"]
    calls = log_stats.get("model_calls", 0)
    if calls == 0:
        return "agent_error", notes + ["no assistant responses recorded"]
    ok_calls = sum(n for sr, n in log_stats.get("stop_reasons", {}).items() if sr in ("stop", "toolUse"))
    if ok_calls == 0:
        return "agent_error", notes + [f"all {calls} assistant response(s) failed"]
    final = log_stats.get("final_stop_reason")
    if final in ("error", "aborted"):
        # Early tool calls do not rescue a run whose last response failed: the
        # agent never produced a final answer, so this is not a success.
        return "agent_error", notes + [f"final assistant response stopReason={final}"]
    if log_stats.get("parse_errors"):
        # Success must not be recorded from a log that cannot be trusted end
        # to end: an unparsable line means the record is incomplete (a torn
        # write may have swallowed the last message_end and its cost). The
        # earlier conservative verdicts (timeout, exit code) are unaffected.
        return "collection_error", notes + [
            f"{log_stats['parse_errors']} unparsable run.log line(s): log incomplete"]
    failed = log_stats.get("stop_reasons", {}).get("error", 0)
    if failed:
        notes.append(f"{failed} failed response(s) among {calls}")
    return "success", notes


def git_info(root: Path) -> tuple[str | None, bool]:
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() or None
        dirty = bool(subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout.strip())
        return head, dirty
    except Exception:
        return None, False


# ---------------------------------------------------------------- per task

PHASE_STATUS = {
    "setup": "setup_error",
    "agent": "agent_error",
    "collect": "collection_error",
    "classify": "collection_error",
}


def phase_status(phase: str) -> str:
    """Infrastructure status for a failure in `phase`.

    Never agent_error: a model verdict requires the evidence chain built in
    classify_status, so an exception escaping any other phase -- including
    the agent phase -- is a runner/infrastructure result: retryable and kept
    out of preds.
    """
    status = PHASE_STATUS.get(phase, "setup_error")
    return "collection_error" if status == "agent_error" else status


def describe_exception(exc: Exception) -> str:
    """Exception string for metadata, keeping the docker stderr that
    subprocess.CalledProcessError's default text drops ("No such image" is
    the usual setup failure and the only actionable part)."""
    msg = f"{type(exc).__name__}: {exc}"
    stderr = getattr(exc, "stderr", None)
    if stderr:
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        msg += f"; stderr: {stderr.strip()[:200]}"
    return msg


def rebuild_preds(run_dir: Path, preds_label: str) -> tuple[int, list[str]]:
    """Atomically rebuild preds.jsonl from the per-instance records.

    The instance directory (metadata.json + patch.diff) is the single source of
    truth; preds.jsonl is derived from it. A crash between the two writes can
    therefore no longer duplicate or lose a prediction on resume: every rebuild
    rewrites the file from scratch (tmp + os.replace) with exactly one line per
    instance whose recorded status is a RESULT_STATUS. Empty patches are
    recorded too (an empty patch is an outcome, not a missing task).

    Returns (line count, instance ids whose recorded outcome has no readable
    patch.diff). Those lines still ship -- the planned set must stay complete
    or the graded denominator shrinks -- but a missing file is a torn record,
    not an empty patch, so callers get the ids to fail the run on.
    """
    lines = []
    missing_patches: list[str] = []
    for inst_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        mf = inst_dir / "metadata.json"
        if not mf.is_file():
            continue
        try:
            meta = json.loads(mf.read_text())
        except Exception:
            continue
        if meta.get("status") not in RESULT_STATUSES:
            continue
        iid = meta.get("instance_id") or inst_dir.name
        try:
            patch = (inst_dir / "patch.diff").read_text()
        except OSError:
            patch = ""
            missing_patches.append(iid)
        lines.append(json.dumps({
            "instance_id": iid,
            "model_name_or_path": preds_label,
            "model_patch": patch,
        }))
    atomic_write_text(run_dir / "preds.jsonl", "".join(line + "\n" for line in lines))
    return len(lines), missing_patches


def cost_was_collected(args, meta: dict) -> bool:
    """Whether this attempt's recorded cost is a complete account of its spend.

    False when the attempt had a container (so it may have made API calls)
    but any cost-carrying record is missing or unparsable: run.log was never
    captured or contains unparsable lines, or decision-log files failed to
    read or to parse. Calls can hide in the missing part and the computed
    cost would silently be a lower bound. A dry run or a setup that failed
    before the container existed made no calls at all, so zero is exact
    there.
    """
    if args.dry_run or "container" not in meta:
        return True
    return bool("cost_usd" in meta
                and not meta.get("parse_errors")
                and not meta.get("decision_read_errors")
                and not (meta.get("jev") or {}).get("parse_errors"))


def run_one(inst: dict, variant: str, model: str, scope: str, args, run_dir: Path,
            preds_label: str, env_kv: dict[str, str], assets: Path, tmp_root: Path) -> dict:
    """Run one instance, retrying infrastructure failures symmetrically.

    setup_error / collection_error / extension_bypass measure nothing and are
    retried (MAX_INFRA_RETRIES) for both variants alike; recorded outcomes
    (success / timeout_partial / agent_error) stop the loop and are the final
    result of this attempt -- model failures are data, not rerun candidates.
    The instance directory is the durable record: metadata.json is written
    atomically first and preds.jsonl is then rebuilt from all instance
    directories, so a crash between the writes cannot duplicate a prediction.
    """
    iid = inst["instance_id"]
    inst_dir = run_dir / iid
    inst_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = 1 + MAX_INFRA_RETRIES
    history = []
    meta: dict = {}
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        meta = run_one_attempt(inst, variant, model, scope, args, run_dir, env_kv,
                               assets, tmp_root, inst_dir)
        per_attempt_cost = round(
            (meta.get("cost_usd") or 0.0)
            + ((meta.get("jev") or {}).get("jev_cost_usd") or 0.0), 6)
        history.append({"attempt": attempts, "status": meta["status"],
                        "error": meta.get("error"), "cost_usd": per_attempt_cost,
                        "cost_collected": cost_was_collected(args, meta)})
        if meta["status"] not in RETRYABLE_STATUSES or attempts >= max_attempts:
            break
        log(f"{iid} [{variant}]: {meta['status']} -> retry {attempts}/{max_attempts - 1}")

    meta["attempts"] = attempts
    meta["attempt_history"] = history
    meta["total_cost_usd"] = round(
        meta.get("cost_usd", 0.0) + (meta.get("jev") or {}).get("jev_cost_usd", 0.0), 6)
    # Retries burn real money: keep both the final attempt's cost (the number a
    # per-instance comparison uses) and the sum over every attempt. When
    # cost_complete is false that sum is a lower bound: an attempt whose log
    # was never captured contributes 0, not its real spend.
    meta["total_cost_usd_all_attempts"] = round(
        sum(h.get("cost_usd") or 0.0 for h in history), 6)
    meta["cost_complete"] = all(h["cost_collected"] for h in history)
    meta["ended_at"] = now_iso()
    # Crash consistency: the instance directory is the source of truth, so the
    # metadata lands first (atomic tmp + rename) and preds.jsonl is then
    # rebuilt from all instance directories. A crash between the two writes
    # can no longer duplicate a prediction when the run is resumed.
    atomic_write_json(inst_dir / "metadata.json", meta)
    rebuild_preds(run_dir, preds_label)
    return meta


def run_one_attempt(inst: dict, variant: str, model: str, scope: str, args, run_dir: Path,
                    env_kv: dict[str, str], assets: Path, tmp_root: Path,
                    inst_dir: Path) -> dict:
    iid = inst["instance_id"]
    meta: dict = {
        "instance_id": iid,
        "variant": variant,
        "model": model,
        "model_scope": scope,
        "timeout_s": args.timeout,
        "started_at": now_iso(),
        "status": "error",
        "status_notes": [],
        "error": None,
    }
    name = None
    env_dir = None
    phase = "setup"
    started = time.monotonic()
    try:
        info = setup_container(inst, env_kv, assets, tmp_root, keep=args.keep_container)
        name = info["name"]
        env_dir = info["env_dir"]
        meta.update({
            "image": info["image"],
            "image_id": info["image_id"],
            "container": name,
            "node_version": info["node_version"],
            "pi_version": info["pi_version"],
            "base_commit": info["base_commit"],
            "image_head": info["image_head"],
            "prompt_sha256": info["task_sha256"],
        })
        log(f"{iid}: container ready (node {info['node_version']}, pi {info['pi_version']})")

        if args.dry_run:
            probe = run_probe(name, variant, scope, model, thinking=args.thinking)
            atomic_write_text(inst_dir / "probe-output.log", probe.pop("output", ""))
            meta["probe"] = probe
            if probe["ok"]:
                meta["status"] = "dry_run_ok"
            else:
                meta["status"] = "dry_run_probe_failed"
                meta["error"] = "probe failed: " + "; ".join(probe["problems"])
            return meta

        phase = "agent"
        exit_code, timed_out, pi_wall, container_stable = launch_pi(
            name, variant, model, scope, args.timeout, thinking=args.thinking)
        meta.update({"exit_code": exit_code, "timed_out": timed_out,
                     "pi_wall_time_s": round(pi_wall, 1),
                     "container_stable": container_stable})
        log(f"{iid}: pi finished exit={exit_code} timed_out={timed_out} wall={pi_wall:.0f}s")

        phase = "collect"
        if not container_stable:
            # The stop/restart verification failed: children may still write to
            # the worktree, so the diff cannot be trusted. Collect diagnostics
            # (best effort) and fail the attempt as an infrastructure error.
            try:
                collect_run_log(name, inst_dir / "run.log")
                meta["decision_files"], meta["decision_read_errors"] = \
                    collect_decisions(name, inst_dir / "jev")
            except Exception as exc:
                log(f"{iid}: diagnostics collection failed: {exc}")
            raise InfraError(
                "container did not stop/restart cleanly after timeout; patch not collected")
        patch = collect_patch(name, info["image_head"])
        atomic_write_text(inst_dir / "patch.diff", patch)
        meta["patch_bytes"] = len(patch)
        if len(patch) > PATCH_SIZE_WARN:
            meta["patch_size_warn"] = True
        collect_run_log(name, inst_dir / "run.log")
        decision_files, decision_read_errors = collect_decisions(name, inst_dir / "jev")
        meta["decision_files"] = decision_files
        meta["decision_read_errors"] = decision_read_errors
        meta["collection_errors"] = len(decision_read_errors)
        log_stats = parse_pi_log(inst_dir / "run.log")
        meta.update(log_stats)
        meta["jev"] = parse_decisions(inst_dir / "jev" / "decisions.jsonl")

        phase = "classify"
        status, notes = classify_status(variant, timed_out, patch, log_stats, meta["jev"],
                                        collection_errors=len(decision_read_errors),
                                        exit_code=exit_code)
        meta["status"] = status
        meta["status_notes"] = notes
        return meta
    except InfraError as exc:
        # Docker/lifecycle failures measure nothing about the model: never
        # agent_error, always a retryable infrastructure status.
        meta["status"] = phase_status(phase)
        meta["error"] = describe_exception(exc)
        return meta
    except Exception as exc:  # keep the batch going on per-task failures
        # An unexpected exception is not a model verdict either: nothing ran
        # classify_status, so it must not land in agent_error (a RESULT
        # status would enter preds as the model's fault with no evidence).
        meta["status"] = phase_status(phase)
        meta["error"] = describe_exception(exc)
        return meta
    finally:
        meta["wall_time_s"] = round(time.monotonic() - started, 1)
        if env_dir is not None:
            shutil.rmtree(env_dir, ignore_errors=True)
        if name is not None:
            cleanup_container(name, keep=args.keep_container)


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi SWE-bench runner (P4/P5)")
    parser.add_argument("--variant", action="append", choices=["vanilla", "governor"],
                        help="repeatable; default: governor")
    parser.add_argument("--instance-id", action="append", default=[],
                        help="repeatable SWE-bench instance id")
    parser.add_argument("--instance-ids", default="", help="comma-separated instance ids")
    parser.add_argument("--limit", type=int, default=None,
                        help="first N instances of the dataset (smoke convenience, not representative)")
    parser.add_argument("--sample", type=int, default=None,
                        help="random sample of N instances (use with --seed for pilots)")
    parser.add_argument("--seed", type=int, default=0, help="sampling seed (with --sample)")
    parser.add_argument("--timeout", type=int, default=1800, help="wall-clock timeout per task")
    parser.add_argument("--model", default=None, help="initial model id (default: PI_JEV_MODEL_NORMAL)")
    parser.add_argument("--thinking", choices=THINKING_LEVELS, default="medium",
                        help="pi thinking level for all runs (explicit: keeps the level fixed "
                             "across batches; pi's own default is also medium)")
    parser.add_argument("--output-dir", default=str(EVAL_DIR / "results"))
    parser.add_argument("--run-id", default=None,
                        help="run label shared by all variants; result goes to <output>/<run-id>/<variant>/")
    parser.add_argument("--assets-dir", default=None)
    parser.add_argument("--env-local", default=str(REPO_ROOT / ".env.local"))
    parser.add_argument("--dry-run", action="store_true",
                        help="stop after container setup + zero-cost load probe (no pi agent run)")
    parser.add_argument("--resume", action="store_true",
                        help="continue an interrupted run: skip instances with a final status, "
                             "rerun infrastructure failures; the run spec (dataset revision, "
                             "instances, models, thinking/timeout, prompts, assets, JEV env) "
                             "must match the previous manifest's config_hash")
    parser.add_argument("--allow-unpinned-dataset", action="store_true",
                        help="continue when the dataset revision cannot be resolved "
                             "(the run then relies on the per-instance fingerprint alone)")
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        sys.exit("--limit must be >= 1")
    if args.sample is not None and args.sample < 1:
        sys.exit("--sample must be >= 1")

    variants = list(dict.fromkeys(args.variant or ["governor"]))
    ids = list(dict.fromkeys(
        list(args.instance_id) + [i.strip() for i in args.instance_ids.split(",") if i.strip()]
    ))

    env_path = Path(args.env_local)
    if not env_path.is_file():
        sys.exit(f".env.local not found: {env_path}")
    env_kv = parse_env_local(env_path)
    # Governor runs make two kinds of calls and need both credentials: model
    # calls use OPENROUTER_API_KEY, JEV decision calls use PI_JEV_API_KEY.
    required_keys = ["OPENROUTER_API_KEY"]
    if "governor" in variants:
        required_keys.append("PI_JEV_API_KEY")
    missing_keys = [k for k in required_keys if not env_kv.get(k)]
    if missing_keys:
        sys.exit(f"missing required key(s) in {env_path}: {', '.join(missing_keys)}")

    tiers = load_tiers(env_kv)
    model = strip_provider(args.model) if args.model else tiers.get("normal", "")
    if not model:
        sys.exit("no model: pass --model or set PI_JEV_MODEL_NORMAL")
    if not MODEL_RE.match(model):
        sys.exit(f"refusing suspicious model id: {model!r}")

    # Pre-flight: a run with a degraded configuration would produce silent
    # garbage (fail-closed JEV gates, unroutable tiers, off-scope switches), so
    # refuse early rather than measure the wrong thing.
    if "governor" in variants:
        missing = [t for t in TIERS if not tiers.get(t)]
        if missing:
            sys.exit("governor runs require all tiers: missing "
                     + ", ".join(f"PI_JEV_MODEL_{t.upper()}" for t in missing))
        if env_kv.get("PI_JEV_UNATTENDED_POLICY") not in ("benchmark", "allow-all"):
            sys.exit("governor runs require PI_JEV_UNATTENDED_POLICY=benchmark or allow-all "
                     "(without either the headless JEV ask path fails closed)")
        if not args.dry_run:
            distinct = sorted({tiers[t] for t in TIERS})
            if len(distinct) < 3:
                # Identical tiers can only measure governor overhead, never
                # routing gains: a same-model switch short-circuits as success.
                sys.exit(f"refusing non-dry-run governor with {len(distinct)} distinct tier "
                         f"model(s): {distinct}; configure three different models in "
                         ".env.local, or pass --dry-run to test the wiring")
    for tier in TIERS:
        m = tiers.get(tier)
        if m and not MODEL_RE.match(m):
            sys.exit(f"refusing suspicious tier model id for {tier}: {m!r}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"pi-jev-{stamp}"
    validate_run_id(run_id)
    run_root = Path(args.output_dir) / run_id
    existing_run = run_root.exists() and any(run_root.iterdir())
    if existing_run and not args.resume:
        sys.exit(f"run dir already exists and is not empty: {run_root} "
                 f"(pass --resume to continue it, or a different --run-id)")
    if args.resume and not existing_run:
        sys.exit(f"--resume: no existing run to continue at {run_root}")
    resume_manifests: dict[str, dict] = {}
    pinned_rev: str | None = None
    if args.resume:
        for variant in variants:
            mf = run_root / variant / "manifest.json"
            if not mf.is_file():
                sys.exit(f"--resume: missing manifest for variant {variant}: {mf}")
            resume_manifests[variant] = json.loads(mf.read_text())
        first = resume_manifests[variants[0]]
        if not ids and not args.limit and not args.sample:
            # No explicit task selection: restore the planned set from the
            # previous manifest so a resumed batch cannot silently grade a
            # different set.
            ids = first.get("instances") or []
            if not ids:
                sys.exit(f"--resume: {variants[0]} manifest does not list any instances")
            log(f"resume: restored {len(ids)} instance id(s) from the previous manifest")
        # Pin the dataset revision recorded by the original run: a resumed run
        # must load the same upstream data revision it started with.
        rev_val = str(first.get("dataset_revision") or "")
        if rev_val and rev_val != "unresolved(default)":
            pinned_rev = rev_val
        elif not (args.dry_run or args.allow_unpinned_dataset):
            sys.exit("--resume: the previous run does not pin a dataset revision; "
                     "refusing to resume against possibly-moved upstream data "
                     "(pass --allow-unpinned-dataset to override)")

    assets = resolve_assets(args.assets_dir)
    log(f"assets: {assets}")
    for variant in variants:
        log(f"scope[{variant}]: {build_scope(variant, model, tiers)}")
    if args.resume:
        dataset_rev = pinned_rev
        if dataset_rev:
            log(f"dataset revision: {dataset_rev} (pinned from the previous manifest)")
        else:
            log("dataset revision: unresolved (--allow-unpinned-dataset)")
    else:
        dataset_rev = resolve_dataset_revision()
        if not dataset_rev and not (args.dry_run or args.allow_unpinned_dataset):
            # Fail closed: without a pinned revision the run cannot prove it
            # loaded the same upstream data twice.
            sys.exit("dataset revision lookup failed; refusing an unpinned run "
                     "(pass --allow-unpinned-dataset to override, or --dry-run "
                     "for zero-cost wiring tests)")
        if dataset_rev:
            log(f"dataset revision: {dataset_rev}")
    instances = load_instances(ids, args.limit, args.sample, args.seed, revision=dataset_rev)
    log(f"instances ({len(instances)}): {[i['instance_id'] for i in instances]}")

    expected = [i["instance_id"] for i in instances]
    dataset_fp = dataset_fingerprint(instances)
    append_prompt = EVAL_DIR / "prompts" / "append-system-prompt.md"
    template = EVAL_DIR / "prompts" / "instance-prompt.md"
    asset_sha = hashlib.sha256((assets / "inject.tar.gz").read_bytes()).hexdigest()
    pi_deps_sha = hashlib.sha256((assets / "pi-deps.tar.gz").read_bytes()).hexdigest()
    append_prompt_sha = hashlib.sha256(append_prompt.read_bytes()).hexdigest()
    template_sha = hashlib.sha256(template.read_bytes()).hexdigest()
    runner_sha = runner_sha256()

    def run_spec_for(variant: str) -> dict:
        return build_run_spec(
            variant=variant, model=model, tiers=tiers, instances=instances,
            dataset_rev=dataset_rev, args=args, asset_sha=asset_sha,
            pi_deps_sha=pi_deps_sha, append_prompt_sha=append_prompt_sha,
            template_sha=template_sha, env_kv=env_kv, runner_sha=runner_sha)

    if args.resume:
        # Immutable run spec: an instance-set match is not enough -- model
        # scope, tiers, thinking, timeout, prompts, assets, JEV env, dry_run
        # and allow_unpinned_dataset are all part of config_hash. Resuming a
        # run whose spec changed would silently mix incomparable results.
        for variant in variants:
            old = resume_manifests[variant]
            if old.get("instances") != expected:
                sys.exit(f"--resume: instance set differs from the previous {variant} "
                         "manifest; refusing to mix tasks under one run-id")
            if old.get("dataset_fingerprint") != dataset_fp:
                sys.exit(f"--resume: dataset rows changed for {variant} "
                         "(instance_id/base_commit fingerprint mismatch); refusing to mix")
            old_hash = old.get("config_hash")
            if not old_hash:
                sys.exit(f"--resume: previous {variant} manifest has no config_hash "
                         "(created by an older runner); refusing to mix")
            spec = run_spec_for(variant)
            if old_hash != compute_config_hash(spec):
                diff = describe_spec_diff(old.get("run_spec") or {}, spec)
                sys.exit(f"--resume: run configuration changed for {variant} "
                         f"({diff}); refusing to mix")

    head, dirty = git_info(REPO_ROOT)
    tmp_root = Path(tempfile.mkdtemp(prefix="pi-jev-run-"))
    evaluator_ver = evaluator_version()
    docker_ver = docker_server_version()
    run_root.mkdir(parents=True, exist_ok=True)

    for variant in variants:
        vdir = run_root / variant
        vdir.mkdir(exist_ok=True)
        spec = run_spec_for(variant)
        manifest = {
            "run_id": run_id,
            "variant": variant,
            "created_at": now_iso(),
            "dataset": f"{DATASET}:{SPLIT}",
            "dataset_revision": dataset_rev or "unresolved(default)",
            "dataset_fingerprint": dataset_fp,
            "instances": [i["instance_id"] for i in instances],
            "model": model,
            "model_scope": build_scope(variant, model, tiers),
            "tier_models": tiers if variant == "governor" else {},
            "thinking_level": args.thinking,
            "timeout_s": args.timeout,
            "dry_run": args.dry_run,
            "allow_unpinned_dataset": args.allow_unpinned_dataset,
            "sample": args.sample,
            "seed": args.seed if args.sample else None,
            "ab_ba_pairing": len(variants) > 1,
            "governor_git_head": head,
            "governor_git_dirty": dirty,
            "assets_dir": str(assets),
            "inject_tar_sha256": asset_sha,
            "pi_deps_tar_sha256": pi_deps_sha,
            "append_prompt_sha256": append_prompt_sha,
            "instance_template_sha256": template_sha,
            "unattended_policy": env_kv.get("PI_JEV_UNATTENDED_POLICY"),
            "jev_env": spec["jev_env"],
            "jev_thresholds": {
                "route_confidence": env_kv.get("PI_JEV_ROUTE_CONFIDENCE") or "default(0.8)",
                "permission_confidence": env_kv.get("PI_JEV_PERMISSION_CONFIDENCE") or "default(0.9)",
                "outcome_confidence": env_kv.get("PI_JEV_OUTCOME_CONFIDENCE") or "default(0.8)",
            },
            "env_allowlist": {
                "prefixes": list(ENV_ALLOWED_PREFIXES),
                "keys": sorted(ENV_ALLOWED_KEYS),
            },
            "evaluator": evaluator_ver,
            "docker_server_version": docker_ver,
            "runner_sha256": runner_sha,
            "run_spec": spec,
            "config_hash": compute_config_hash(spec),
        }
        mf_path = vdir / "manifest.json"
        if args.resume:
            old = resume_manifests.get(variant) or json.loads(mf_path.read_text())
            manifest["created_at"] = old.get("created_at", manifest["created_at"])
            manifest["resumed_at"] = [*old.get("resumed_at", []), now_iso()]
            # This rewrite would otherwise silently replace the git state the
            # original run recorded with the current one: keep the first run's
            # values under a stable key.
            manifest["original_governor_git"] = old.get("original_governor_git") or {
                "head": old.get("governor_git_head"),
                "dirty": old.get("governor_git_dirty"),
            }
        atomic_write_json(mf_path, manifest)
        log(f"=== variant={variant} dir={vdir} ===")

    if args.resume:
        # Rebuild preds.jsonl from the instance records before anything runs:
        # heals any torn write a crash left behind (duplicate or missing lines).
        for variant in variants:
            n, missing = rebuild_preds(run_root / variant, f"{run_id}-{variant}")
            log(f"resume: rebuilt preds.jsonl for {variant} ({n} line(s) from instance metadata)")
            if missing:
                log(f"warn: resume: {variant}: {len(missing)} recorded outcome(s) have no "
                    f"patch.diff: {', '.join(missing)}")

    def prior_meta(vdir: Path, iid: str) -> dict | None:
        mf = vdir / iid / "metadata.json"
        if not mf.is_file():
            return None
        try:
            return json.loads(mf.read_text())
        except Exception:
            return None

    summaries = []
    try:
        for idx, inst in enumerate(instances):
            # Paired AB/BA alternation keeps provider load / rate limits / model
            # version drift from biasing one variant on the same task stream.
            order = variants if idx % 2 == 0 else list(reversed(variants))
            for variant in order:
                vdir = run_root / variant
                iid = inst["instance_id"]
                if args.resume:
                    prior = prior_meta(vdir, iid)
                    if prior and prior.get("status") in RESUME_TERMINAL_STATUSES:
                        # The preds file was already rebuilt from instance metadata
                        # above; just re-register the outcome.
                        summaries.append(prior)
                        log(f"{iid} [{variant}]: skipped (already {prior['status']})")
                        continue
                meta = run_one(inst, variant, model, build_scope(variant, model, tiers), args,
                               vdir, f"{run_id}-{variant}", env_kv, assets, tmp_root)
                summaries.append(meta)
                log(f"{iid} [{variant}]: status={meta['status']} "
                    f"wall={meta.get('wall_time_s')}s cost=${meta.get('total_cost_usd', 0)} "
                    f"{('error=' + meta['error']) if meta.get('error') else ''}")
    finally:
        # Even on Ctrl-C a temp root must not leak: it holds the per-attempt
        # container.env files with the API keys.
        shutil.rmtree(tmp_root, ignore_errors=True)

    log("=== summary ===")
    total_pi = total_jev = total_all = 0.0
    cost_incomplete = 0
    for meta in summaries:
        cost_pi = meta.get("cost_usd", 0) or 0
        cost_jev = (meta.get("jev") or {}).get("jev_cost_usd", 0) or 0
        cost_final = meta.get("total_cost_usd", cost_pi)
        cost_all = meta.get("total_cost_usd_all_attempts", cost_final)
        cost_ok = meta.get("cost_complete", True)
        total_pi += cost_pi
        total_jev += cost_jev
        total_all += cost_all
        if not cost_ok:
            cost_incomplete += 1
        retry_note = f" all-attempts=${cost_all}" if cost_all != cost_final else ""
        cost_note = "" if cost_ok else " cost_complete=false"
        print(f"  {meta['variant']:<9} {meta['instance_id']:<45} {meta['status']:<22} "
              f"wall={meta.get('wall_time_s')}s cost=${cost_final}{retry_note}{cost_note} "
              f"(pi=${cost_pi} jev=${cost_jev})")
    total_note = (f" (cost_complete=false for {cost_incomplete} instance(s): lower bound)"
                  if cost_incomplete else "")
    print(f"  TOTAL: pi=${round(total_pi, 6)} jev=${round(total_jev, 6)} "
          f"sum=${round(total_pi + total_jev, 6)} all-attempts=${round(total_all, 6)}"
          f"{total_note}")
    log(f"results: {run_root}")

    # Finalize manifests: every variant's denominator is the planned instance
    # set, so the manifest must show exactly how each task ended.
    missing_patch_files: list[str] = []
    for variant in variants:
        vdir = run_root / variant
        rows = [m for m in summaries if m["variant"] == variant]
        counts: dict[str, int] = {}
        for m in rows:
            counts[m["status"]] = counts.get(m["status"], 0) + 1
        mf_path = vdir / "manifest.json"
        if not mf_path.is_file():
            continue
        preds_count, missing = rebuild_preds(vdir, f"{run_id}-{variant}")
        manifest = json.loads(mf_path.read_text())
        manifest["finalized_at"] = now_iso()
        manifest["expected"] = len(instances)
        manifest["completed"] = len(rows)
        manifest["status_counts"] = dict(sorted(counts.items()))
        manifest["recorded_outcomes"] = sum(1 for m in rows if m["status"] in RESULT_STATUSES)
        manifest["infra_failed"] = sorted(m["instance_id"] for m in rows
                                          if m["status"] in INFRA_STATUSES)
        manifest["cost_incomplete"] = sorted(m["instance_id"] for m in rows
                                             if m.get("cost_complete") is False)
        manifest["preds_count"] = preds_count
        # A recorded outcome whose patch.diff is missing shipped as an empty
        # patch: keep the line (the denominator stays whole) but surface the
        # torn record so the run exits non-zero below.
        manifest["missing_patch_files"] = sorted(missing)
        missing_patch_files.extend(f"{variant}/{iid}" for iid in missing)
        atomic_write_json(mf_path, manifest)

    if args.dry_run:
        bad = [m for m in summaries if m["status"] != "dry_run_ok"]
        if bad:
            sys.exit(1)
    else:
        infra_failed = [m for m in summaries if m["status"] in INFRA_STATUSES]
        outcomes = [m for m in summaries if m["status"] in RESULT_STATUSES]
        if infra_failed:
            log(f"exit 1: {len(infra_failed)} instance(s) ended without a recorded outcome "
                "(infrastructure failures)")
            sys.exit(1)
        if missing_patch_files:
            log(f"exit 1: {len(missing_patch_files)} recorded outcome(s) without patch.diff "
                f"({', '.join(missing_patch_files)}); repair or re-run them before grading")
            sys.exit(1)
        if not outcomes:
            log("exit 1: no recorded outcomes at all")
            sys.exit(1)


if __name__ == "__main__":
    main()
