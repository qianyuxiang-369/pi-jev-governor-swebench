#!/usr/bin/env python3
"""Unit tests for the Pi SWE-bench runner (run.py).

Pure-function and fault-injection coverage for the review findings:
  * classify_status: validity gates (route records, parse/collection/extension
    errors, pi events) run before outcome gates; timeout never bypasses the
    governor check; the final stop reason and exit codes drive the verdict.
  * analyze_probe_output: structural evidence chain -- an unrelated fatal
    error (rc=127), a degraded governor (jev-state but no route record), or
    bare credential strings must NOT pass the zero-cost probe.
  * rebuild_preds: preds.jsonl is derived from instance metadata + patch.diff
    (crash consistency), never appended twice; infra statuses stay out; a
    recorded outcome whose patch.diff is unreadable is reported, not silently
    shipped as a legitimate empty patch.
  * run spec / config_hash / dataset_fingerprint: resume refusals.
  * InfraError / collect rc checks / phase_status: docker failures and
    unexpected runner exceptions never classify as agent_error; decision log
    read errors surface as collection errors.
  * cost_was_collected / run_one cost accounting: retries sum honestly; an
    attempt that lost run.log, a parsable record, or a decision-log read is
    flagged cost_complete=false (a lower bound, never an exact total).
  * parse_pi_log / parse_decisions: ordered stop sequence, cache tokens,
    route tier distribution / skip reasons / degradations, parse errors.
  * RESULT_STATUSES: recorded outcomes (empty patches included) vs
    infrastructure statuses -- the prediction/denominator policy.

Run with:  /workspace/.conda/bin/python -m pytest test_run.py -q
"""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUN_PY = Path(__file__).resolve().parent / "run.py"
_spec = importlib.util.spec_from_file_location("swebench_run", RUN_PY)
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)


def log_stats(**kwargs) -> dict:
    """A parse_pi_log-shaped dict with sane defaults."""
    base = {
        "model_calls": 1, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
        "models": {}, "stop_reasons": {"stop": 1}, "stop_sequence": ["stop"],
        "final_stop_reason": "stop", "agent_settled": True, "aborted": False,
        "extension_errors": 0, "parse_errors": 0, "events": 1,
    }
    base.update(kwargs)
    return base


def route_stats(route=1, **kwargs) -> dict:
    """A parse_decisions-shaped dict with sane defaults (governor valid)."""
    base = {"route": route, "permission": 0, "outcome": 0, "parse_errors": 0}
    base.update(kwargs)
    return base


def make_args(**kwargs):
    base = {"thinking": "medium", "timeout": 1800, "dry_run": False,
            "allow_unpinned_dataset": False, "keep_container": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


# ------------------------------------------------------------- classify_status

class TestClassifyStatus:
    def test_success(self):
        status, notes = run.classify_status("governor", False, "patch", log_stats(),
                                            route_stats(3))
        assert status == "success"
        assert notes == []

    def test_timeout_with_patch_is_timeout_partial(self):
        status, notes = run.classify_status("vanilla", True, "patch", log_stats(), route_stats(0))
        assert status == "timeout_partial"

    def test_timeout_empty_patch_still_recorded(self):
        status, notes = run.classify_status("vanilla", True, "", log_stats(), route_stats(0))
        assert status == "timeout_partial"
        assert any("empty patch" in n for n in notes)

    def test_governor_timeout_without_route_records_is_bypass(self):
        # P0 regression: the governor validity check must run before the
        # timeout branch, or a bypassed extension slips into preds.
        status, _ = run.classify_status("governor", True, "patch", log_stats(), route_stats(0))
        assert status == "extension_bypass"

    def test_governor_without_route_records_is_bypass(self):
        status, _ = run.classify_status("governor", False, "patch", log_stats(), route_stats(0))
        assert status == "extension_bypass"

    def test_governor_degraded_jev_state_only_is_bypass(self):
        # Degraded config writes a jev-state entry but no decision records:
        # the route count (not decision-file existence) is the validity gate.
        status, notes = run.classify_status("governor", False, "patch", log_stats(),
                                            route_stats(0))
        assert status == "extension_bypass"
        assert any("route" in n for n in notes)

    def test_governor_decision_parse_errors_is_collection_error(self):
        status, _ = run.classify_status("governor", False, "patch", log_stats(),
                                        route_stats(2, parse_errors=1))
        assert status == "collection_error"

    def test_governor_extension_error_line_is_bypass(self):
        status, _ = run.classify_status("governor", False, "patch",
                                        log_stats(extension_errors=1), route_stats(2))
        assert status == "extension_bypass"

    def test_collection_errors_is_collection_error(self):
        status, _ = run.classify_status("governor", False, "patch", log_stats(),
                                        route_stats(2), collection_errors=1)
        assert status == "collection_error"

    def test_no_pi_events_is_collection_error_not_agent_error(self):
        status, notes = run.classify_status("vanilla", False, "p",
                                            log_stats(events=0, agent_settled=False),
                                            route_stats(0))
        assert status == "collection_error"
        assert any("events" in n for n in notes)

    def test_no_pi_events_beats_timeout(self):
        status, _ = run.classify_status("vanilla", True, "p", log_stats(events=0),
                                        route_stats(0))
        assert status == "collection_error"

    def test_vanilla_timeout_without_decision_logs_is_fine(self):
        status, _ = run.classify_status("vanilla", True, "patch", log_stats(), route_stats(0))
        assert status == "timeout_partial"

    def test_no_agent_settled_is_agent_error(self):
        status, _ = run.classify_status("vanilla", False, "p", log_stats(agent_settled=False),
                                        route_stats(0))
        assert status == "agent_error"

    def test_aborted_is_agent_error(self):
        status, _ = run.classify_status("vanilla", False, "p", log_stats(aborted=True),
                                        route_stats(0))
        assert status == "agent_error"

    def test_no_calls_is_agent_error(self):
        status, _ = run.classify_status(
            "vanilla", False, "p",
            log_stats(model_calls=0, stop_reasons={}, stop_sequence=[], final_stop_reason=None),
            route_stats(0))
        assert status == "agent_error"

    def test_all_calls_failed_is_agent_error(self):
        status, _ = run.classify_status(
            "vanilla", False, "p",
            log_stats(stop_reasons={"error": 2}, stop_sequence=["error", "error"],
                      final_stop_reason="error"), route_stats(0))
        assert status == "agent_error"

    def test_final_error_after_tool_calls_is_agent_error(self):
        # P0 regression: {"toolUse": 3, "error": 1} used to classify as success.
        stats = log_stats(model_calls=4, stop_reasons={"toolUse": 3, "error": 1},
                          stop_sequence=["toolUse", "toolUse", "toolUse", "error"],
                          final_stop_reason="error")
        status, notes = run.classify_status("governor", False, "p", stats, route_stats(3))
        assert status == "agent_error"
        assert any("final" in n for n in notes)

    def test_final_aborted_is_agent_error(self):
        stats = log_stats(stop_sequence=["stop", "aborted"], final_stop_reason="aborted",
                          stop_reasons={"stop": 1, "aborted": 1})
        status, _ = run.classify_status("vanilla", False, "p", stats, route_stats(0))
        assert status == "agent_error"

    def test_nonzero_exit_code_is_agent_error(self):
        status, notes = run.classify_status("vanilla", False, "p", log_stats(), route_stats(0),
                                            exit_code=1)
        assert status == "agent_error"
        assert any("exit" in n.lower() for n in notes)

    def test_zero_exit_code_is_fine(self):
        status, _ = run.classify_status("vanilla", False, "p", log_stats(), route_stats(0),
                                        exit_code=0)
        assert status == "success"

    def test_early_error_among_calls_only_notes(self):
        stats = log_stats(model_calls=2, stop_reasons={"error": 1, "stop": 1},
                          stop_sequence=["error", "stop"], final_stop_reason="stop")
        status, notes = run.classify_status("vanilla", False, "p", stats, route_stats(0))
        assert status == "success"
        assert any("failed response" in n for n in notes)

    def test_parse_errors_block_success(self):
        # P0 regression: an unparsable run.log line means the record is
        # incomplete (a torn write can swallow the last message_end and its
        # cost); success must not be recorded from such a log.
        status, notes = run.classify_status("vanilla", False, "p",
                                            log_stats(parse_errors=1), route_stats(0))
        assert status == "collection_error"
        assert any("unparsable" in n for n in notes)

    def test_parse_errors_do_not_downgrade_timeout(self):
        # Timeout truncation is expected: the timeout verdict must not be
        # converted into a retryable collection error by a torn last line.
        status, _ = run.classify_status("vanilla", True, "p",
                                        log_stats(parse_errors=2), route_stats(0))
        assert status == "timeout_partial"


# ------------------------------------------------------------ analyze_probe

GOOD_PROBE_OUTPUT = "\n".join([
    '{"type":"session_start"}',
    '{"type":"message_end","message":{"role":"assistant","stopReason":"error",'
    '"errorMessage":"401 Missing Authentication header"}}',
    '{"type":"custom_entry","customType":"jev-state"}',
])

GOOD_JEV_TEXT = '{"decisionKind":"route","answers":{},"effective":"phase=planning; tier=kept"}\n'


class TestAnalyzeProbe:
    def test_unknown_fatal_error_fails(self):
        # P0 regression: rc=127 + unrelated fatal output used to pass.
        problems = run.analyze_probe_output("vanilla", 127, "bash: fatal unexpected failure")
        assert problems  # shell-level + no pi events
        assert any("shell level" in p for p in problems)
        assert any("no parsable JSON" in p for p in problems)

    def test_good_governor_probe_passes(self):
        assert run.analyze_probe_output("governor", 0, GOOD_PROBE_OUTPUT, GOOD_JEV_TEXT) == []

    def test_good_vanilla_probe_passes_without_jev_records(self):
        out = GOOD_PROBE_OUTPUT.replace('{"type":"custom_entry","customType":"jev-state"}', "")
        assert run.analyze_probe_output("vanilla", 0, out) == []

    def test_degraded_governor_jev_state_only_fails(self):
        # P0 regression: a jev-state entry + structured 401 passed the old
        # matching even though the config was degraded and no decision record
        # was ever written; the route record is the real validity gate.
        problems = run.analyze_probe_output("governor", 0, GOOD_PROBE_OUTPUT, "")
        assert any("route decision record" in p for p in problems)

    def test_governor_route_record_with_parse_errors_fails(self):
        jev = GOOD_JEV_TEXT + '{"broken\n'
        problems = run.analyze_probe_output("governor", 0, GOOD_PROBE_OUTPUT, jev)
        assert any("unparsable decision" in p for p in problems)

    def test_credential_substring_no_longer_counts_as_auth(self):
        out = ("authentication failed: invalid credential / api key\n"
               '{"type":"message_end","message":{"role":"assistant","stopReason":"error"}}')
        problems = run.analyze_probe_output("vanilla", 0, out)
        assert any("auth-failure" in p for p in problems)

    def test_garbage_json_without_type_does_not_count_as_event(self):
        out = '{"foo":1}\n{"bar":"401 unauthorized"}'
        problems = run.analyze_probe_output("vanilla", 0, out)
        assert any("no parsable JSON events" in p for p in problems)
        assert any("auth-failure" in p for p in problems)

    def test_extension_error_marker_fails(self):
        out = GOOD_PROBE_OUTPUT + "\nFailed to load extension: x"
        assert any("extension failed" in p
                   for p in run.analyze_probe_output("governor", 0, out, GOOD_JEV_TEXT))

    def test_scope_pattern_marker_fails(self):
        out = GOOD_PROBE_OUTPUT + "\nWarning: No models match pattern"
        assert any("scope pattern" in p
                   for p in run.analyze_probe_output("governor", 0, out, GOOD_JEV_TEXT))

    def test_no_auth_evidence_fails(self):
        out = '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}'
        assert any("auth-failure" in p for p in run.analyze_probe_output("vanilla", 0, out))

    def test_rc_137_fails(self):
        problems = run.analyze_probe_output("vanilla", 137, GOOD_PROBE_OUTPUT.replace("401", "oops"))
        assert any("shell level" in p for p in problems)

    def test_nonzero_rc_fails_even_with_full_chain(self):
        # The real probe exits 0. Any non-zero rc fails outright: an intact
        # evidence chain no longer excuses it (pi may still die from an
        # unrelated cause after printing the structured 401).
        problems = run.analyze_probe_output("governor", 1, GOOD_PROBE_OUTPUT, GOOD_JEV_TEXT)
        assert any("non-zero probe exit" in p for p in problems)

    def test_nonzero_rc_without_auth_evidence_fails(self):
        problems = run.analyze_probe_output(
            "vanilla", 1,
            '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}')
        assert any("non-zero probe exit" in p for p in problems)

    def test_decision_log_io_problem_is_surfaced(self):
        problems = run.analyze_probe_output("governor", 0, GOOD_PROBE_OUTPUT,
                                            GOOD_JEV_TEXT, ["cat x.jsonl failed (rc=1)"])
        assert any("cat x.jsonl" in p for p in problems)


# ------------------------------------------------------------------ preds policy

class TestPredsPolicy:
    def test_recorded_outcomes_include_model_failures(self):
        assert run.RESULT_STATUSES == {"success", "timeout_partial", "agent_error"}

    def test_infra_statuses_are_not_recorded(self):
        assert run.INFRA_STATUSES == {"setup_error", "collection_error", "extension_bypass"}
        assert not (run.RESULT_STATUSES & run.INFRA_STATUSES)

    def test_retry_candidates_are_infra_only(self):
        assert run.RETRYABLE_STATUSES == run.INFRA_STATUSES

    def test_resume_terminals_exclude_probe_failed(self):
        assert "dry_run_ok" in run.RESUME_TERMINAL_STATUSES
        assert "dry_run_probe_failed" not in run.RESUME_TERMINAL_STATUSES

    def test_rebuild_preds_records_empty_patch(self, tmp_path):
        inst = tmp_path / "task-1"
        inst.mkdir()
        run.atomic_write_json(inst / "metadata.json",
                              {"instance_id": "task-1", "status": "success"})
        (inst / "patch.diff").write_text("")
        n, missing = run.rebuild_preds(tmp_path, "label")
        lines = (tmp_path / "preds.jsonl").read_text().splitlines()
        assert n == 1 and missing == []
        assert json.loads(lines[0]) == {"instance_id": "task-1",
                                        "model_name_or_path": "label", "model_patch": ""}

    def test_rebuild_preds_reads_patch_diff(self, tmp_path):
        inst = tmp_path / "task-1"
        inst.mkdir()
        run.atomic_write_json(inst / "metadata.json",
                              {"instance_id": "task-1", "status": "timeout_partial"})
        (inst / "patch.diff").write_text("diff --git a/x b/x\n")
        n, missing = run.rebuild_preds(tmp_path, "label")
        assert (n, missing) == (1, [])
        line = json.loads((tmp_path / "preds.jsonl").read_text().splitlines()[0])
        assert line["model_patch"] == "diff --git a/x b/x\n"

    def test_rebuild_preds_dedupes_crash_leftover(self, tmp_path):
        # P0 regression: a crash between the old append-preds and metadata
        # writes left a duplicate line; the rebuild derives the file from
        # instance metadata, so duplicates cannot survive.
        inst = tmp_path / "task-1"
        inst.mkdir()
        run.atomic_write_json(inst / "metadata.json",
                              {"instance_id": "task-1", "status": "success"})
        (inst / "patch.diff").write_text("")
        (tmp_path / "preds.jsonl").write_text(
            json.dumps({"instance_id": "task-1", "model_name_or_path": "old",
                        "model_patch": ""}) + "\n" + "torn garbage\n")
        n, _ = run.rebuild_preds(tmp_path, "label")
        lines = (tmp_path / "preds.jsonl").read_text().splitlines()
        assert n == 1 and len(lines) == 1
        assert json.loads(lines[0])["model_name_or_path"] == "label"

    def test_rebuild_preds_excludes_infra_statuses(self, tmp_path):
        for iid, status in (("task-1", "success"), ("task-2", "setup_error"),
                            ("task-3", "extension_bypass"), ("task-4", "collection_error")):
            inst = tmp_path / iid
            inst.mkdir()
            run.atomic_write_json(inst / "metadata.json", {"instance_id": iid, "status": status})
            (inst / "patch.diff").write_text("")
        assert run.rebuild_preds(tmp_path, "label")[0] == 1
        got = [json.loads(l)["instance_id"] for l in
               (tmp_path / "preds.jsonl").read_text().splitlines()]
        assert got == ["task-1"]

    def test_rebuild_preds_is_idempotent(self, tmp_path):
        inst = tmp_path / "task-1"
        inst.mkdir()
        run.atomic_write_json(inst / "metadata.json",
                              {"instance_id": "task-1", "status": "success"})
        (inst / "patch.diff").write_text("")
        assert run.rebuild_preds(tmp_path, "label")[0] == 1
        assert run.rebuild_preds(tmp_path, "label")[0] == 1
        assert len((tmp_path / "preds.jsonl").read_text().splitlines()) == 1

    def test_rebuild_preds_reports_missing_patch_file(self, tmp_path):
        # P1 regression: a recorded outcome without its patch file is a torn
        # record, not an empty patch. The line still ships (dropping it would
        # shrink the graded denominator) but the id is reported so the caller
        # can fail the run.
        inst = tmp_path / "task-1"
        inst.mkdir()
        run.atomic_write_json(inst / "metadata.json",
                              {"instance_id": "task-1", "status": "success"})
        n, missing = run.rebuild_preds(tmp_path, "label")
        assert n == 1 and missing == ["task-1"]
        line = json.loads((tmp_path / "preds.jsonl").read_text().splitlines()[0])
        assert line["model_patch"] == ""

    def test_atomic_write_has_no_tmp_leftover(self, tmp_path):
        p = tmp_path / "metadata.json"
        run.atomic_write_json(p, {"a": 1})
        assert json.loads(p.read_text()) == {"a": 1}
        assert [f.name for f in tmp_path.iterdir()] == ["metadata.json"]

    def test_atomic_write_bytes_has_no_tmp_leftover(self, tmp_path):
        p = tmp_path / "run.log"
        run.atomic_write_bytes(p, b'{"a": 1}\n')
        assert p.read_bytes() == b'{"a": 1}\n'
        assert [f.name for f in tmp_path.iterdir()] == ["run.log"]


# ------------------------------------------------------------------ parse_pi_log

def write_log(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "run.log"
    p.write_text("\n".join(lines) + "\n")
    return p


def msg(stop="stop", **usage) -> str:
    return json.dumps({"type": "message_end", "message": {
        "role": "assistant", "provider": "openrouter", "model": "m",
        "stopReason": stop, "usage": usage}})


class TestParsePiLog:
    def test_ordered_stop_sequence_and_final(self, tmp_path):
        log = write_log(tmp_path, [
            msg("toolUse"), msg("toolUse"), msg("error"), ])
        stats = run.parse_pi_log(log)
        assert stats["stop_sequence"] == ["toolUse", "toolUse", "error"]
        assert stats["final_stop_reason"] == "error"
        assert stats["stop_reasons"] == {"toolUse": 2, "error": 1}

    def test_cache_tokens_counted(self, tmp_path):
        log = write_log(tmp_path, [msg("stop", cacheRead=1000, cacheWrite=200)])
        stats = run.parse_pi_log(log)
        assert stats["cache_read_tokens"] == 1000
        assert stats["cache_write_tokens"] == 200

    def test_cost_dict_total(self, tmp_path):
        log = write_log(tmp_path, [msg("stop", cost={"input": 0.1, "output": 0.2, "total": 0.3})])
        assert run.parse_pi_log(log)["cost_usd"] == 0.3

    def test_parse_errors_counted(self, tmp_path):
        log = write_log(tmp_path, ['{"broken', msg("stop")])
        assert run.parse_pi_log(log)["parse_errors"] == 1

    def test_settled_and_aborted(self, tmp_path):
        log = write_log(tmp_path, [
            json.dumps({"type": "agent_settled", "aborted": True}), msg("stop")])
        stats = run.parse_pi_log(log)
        assert stats["agent_settled"] is True and stats["aborted"] is True

    def test_missing_file_returns_defaults(self, tmp_path):
        stats = run.parse_pi_log(tmp_path / "nope.log")
        assert stats["model_calls"] == 0 and stats["final_stop_reason"] is None

    def test_events_counted_only_for_valid_json(self, tmp_path):
        log = write_log(tmp_path, ['not json', '{"broken', msg("stop")])
        stats = run.parse_pi_log(log)
        assert stats["events"] == 1 and stats["parse_errors"] == 1


# ---------------------------------------------------------------- parse_decisions

def route(effective, tier=None) -> str:
    answers = {"tier": {"type": "choice", "value": tier, "confidence": 0.9}} if tier else {}
    return json.dumps({"decisionKind": "route", "answers": answers, "effective": effective,
                       "latencyMs": 120, "usage": {"cost": 0.001, "input_tokens": 10,
                                                   "output_tokens": 2}})


class TestParseDecisions:
    def test_route_tier_distribution(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text("\n".join([
            route("phase=executing; tier=small", "small"),
            route("phase=executing; tier=small", "small"),
            route("phase=executing; tier=strong", "strong"),
        ]) + "\n")
        stats = run.parse_decisions(p)
        assert stats["route"] == 3
        assert stats["route_tiers"] == {"small": 2, "strong": 1}
        assert stats["route_effective_tiers"] == {"small": 2, "strong": 1}

    def test_skipped_switch_recorded(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text(route("phase=executing; tier=small (skipped: not_scoped)", "small") + "\n")
        stats = run.parse_decisions(p)
        assert stats["route_skips"] == {"not_scoped": 1}
        assert stats["route_effective_tiers"] == {}

    def test_kept_tier(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text(route("phase=planning; tier=kept", "strong") + "\n")
        stats = run.parse_decisions(p)
        assert stats["route_kept"] == 1

    def test_degradations_recorded(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text(route("phase=executing; tier=normal; tier_timeout_fallback", "normal") + "\n")
        stats = run.parse_decisions(p)
        assert stats["route_degradations"] == {"tier_timeout_fallback": 1}

    def test_jev_cost_and_latency(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text(route("phase=executing; tier=kept", "small") + "\n")
        stats = run.parse_decisions(p)
        assert stats["jev_cost_usd"] == 0.001
        assert stats["jev_latency_ms"] == 120
        assert stats["jev_input_tokens"] == 10

    def test_permission_counts(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text("\n".join([
            json.dumps({"decisionKind": "permission", "effective": "allow"},
                       ),
            json.dumps({"decisionKind": "permission", "effective": "block (fail_closed, x)"}),
        ]) + "\n")
        stats = run.parse_decisions(p)
        assert stats["permission_allow"] == 1 and stats["permission_block"] == 1

    def test_parse_errors_counted(self, tmp_path):
        p = tmp_path / "decisions.jsonl"
        p.write_text('{"broken\n' + route("phase=x; tier=kept", "small") + "\n")
        assert run.parse_decisions(p)["parse_errors"] == 1


# ------------------------------------------------------------- env / scope / id

class TestEnvScopeRunId:
    def test_filter_env_allowlist(self):
        env = {
            "OPENROUTER_API_KEY": "a", "PI_JEV_API_KEY": "b", "PI_JEV_MODEL_SMALL": "m1",
            "AWS_SECRET_ACCESS_KEY": "leak", "SOME_OTHER_TOKEN": "leak2",
        }
        kept, dropped = run.filter_env(env)
        assert set(kept) == {"OPENROUTER_API_KEY", "PI_JEV_API_KEY", "PI_JEV_MODEL_SMALL"}
        assert dropped == ["AWS_SECRET_ACCESS_KEY", "SOME_OTHER_TOKEN"]

    def test_build_scope_vanilla_is_single_model(self):
        assert run.build_scope("vanilla", "a/b", {"small": "x", "normal": "y", "strong": "z"}) == "a/b"

    def test_build_scope_governor_includes_all_tiers(self):
        scope = run.build_scope("governor", "strong-model", {"small": "s", "normal": "n", "strong": "g"})
        assert scope.split(",") == ["strong-model", "s", "n", "g"]

    def test_build_scope_dedupes_initial_model(self):
        scope = run.build_scope("governor", "g", {"small": "s", "normal": "n", "strong": "g"})
        assert scope.split(",") == ["g", "s", "n"]

    def test_validate_run_id_rejects_traversal(self):
        with pytest.raises(SystemExit):
            run.validate_run_id("../../evil")

    def test_validate_run_id_accepts_normal(self):
        run.validate_run_id("pilot-01_2026.10")

    def test_validate_run_id_rejects_dot_and_dotdot(self):
        # "." / ".." pass the character check but would redirect the run root
        # (and its manifests) outside the output directory.
        for bad in (".", ".."):
            with pytest.raises(SystemExit):
                run.validate_run_id(bad)

    def test_load_instances_zero_selection_returns_empty(self, monkeypatch):
        # 0 is an explicit value (rejected by main()): it must yield an empty
        # run, not silently fall through to the 1-instance default.
        fake = SimpleNamespace(load_dataset=lambda *a, **k: [{"instance_id": "t1"}])
        monkeypatch.setitem(sys.modules, "datasets", fake)
        assert run.load_instances([], 0, None, 0) == []
        assert run.load_instances([], None, 0, 0) == []


# ----------------------------------------------------- run spec / config hash

class TestRunSpecAndFingerprint:
    def _spec(self, **overrides):
        kwargs = {
            "variant": "governor", "model": "strong-model",
            "tiers": {"small": "s", "normal": "n", "strong": "strong-model"},
            "instances": [{"instance_id": "task-1", "base_commit": "a" * 40}],
            "dataset_rev": "r" * 40, "args": make_args(), "asset_sha": "x" * 64,
            "pi_deps_sha": "y" * 64, "append_prompt_sha": "p" * 64,
            "template_sha": "t" * 64, "runner_sha": "r" * 64,
            "env_kv": {"OPENROUTER_API_KEY": "sk-secret", "PI_JEV_API_KEY": "sk-secret",
                       "PI_JEV_MODEL_SMALL": "openrouter:s",
                       "PI_JEV_UNATTENDED_POLICY": "benchmark"},
        }
        kwargs.update(overrides)
        return run.build_run_spec(**kwargs)

    def test_config_hash_is_64_hex_and_stable(self):
        h1 = run.compute_config_hash(self._spec())
        h2 = run.compute_config_hash(self._spec())
        assert h1 == h2 and len(h1) == 64

    def test_config_hash_changes_with_thinking(self):
        base = run.compute_config_hash(self._spec())
        assert run.compute_config_hash(self._spec(args=make_args(thinking="high"))) != base

    def test_config_hash_changes_with_jev_env(self):
        base = run.compute_config_hash(self._spec())
        assert run.compute_config_hash(self._spec(env_kv={"PI_JEV_TIMEOUT_MS": "30000"})) != base

    def test_config_hash_changes_with_dry_run(self):
        base = run.compute_config_hash(self._spec())
        assert run.compute_config_hash(self._spec(args=make_args(dry_run=True))) != base

    def test_config_hash_changes_with_runner_sha(self):
        # The eval tree is untracked in git: the runner's own hash is the only
        # thing that makes a modified run.py visible to the resume refusal.
        base = run.compute_config_hash(self._spec())
        assert run.compute_config_hash(self._spec(runner_sha="s" * 64)) != base

    def test_runner_sha256_pins_the_runner_file(self):
        assert run.runner_sha256() == hashlib.sha256(RUN_PY.read_bytes()).hexdigest()

    def test_spec_excludes_credentials(self):
        spec = self._spec()
        assert "OPENROUTER_API_KEY" not in spec["jev_env"]
        assert "PI_JEV_API_KEY" not in spec["jev_env"]
        assert spec["jev_env"]["PI_JEV_UNATTENDED_POLICY"] == "benchmark"

    def test_dataset_fingerprint_tracks_base_commit(self):
        a = [{"instance_id": "t", "base_commit": "a" * 40}]
        b = [{"instance_id": "t", "base_commit": "b" * 40}]
        assert len(run.dataset_fingerprint(a)) == 64
        assert run.dataset_fingerprint(a) == run.dataset_fingerprint(list(a))
        assert run.dataset_fingerprint(a) != run.dataset_fingerprint(b)

    def test_describe_spec_diff_lists_changed_keys(self):
        old = {"model": "m1", "timeout_s": 1800}
        new = {"model": "m2", "timeout_s": 1800}
        msg = run.describe_spec_diff(old, new)
        assert "model" in msg and "timeout_s" not in msg

    def test_describe_spec_diff_detects_no_difference(self):
        assert "no spec field differs" in run.describe_spec_diff({"a": 1}, {"a": 1})


# ------------------------------------------------- infra errors / crash safety

class TestInfraAndCollect:
    def test_launch_pi_docker_failure_is_infra_error(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("docker daemon gone")
        monkeypatch.setattr(run.subprocess, "run", boom)
        with pytest.raises(run.InfraError):
            run.launch_pi("c1", "vanilla", "m", "m", 10)

    def test_restart_container_never_raises_on_docker_failure(self, monkeypatch):
        # P0 regression: launch_pi's TimeoutExpired handler calls this; a
        # docker failure escaping restart_container used to bypass the
        # InfraError channel and surface as a phantom agent_error.
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["docker", "stop"], timeout=120)
        monkeypatch.setattr(run, "docker", boom)
        assert run.restart_container("c1") is False

    def test_cleanup_container_never_raises(self, monkeypatch):
        # Regression: cleanup runs in a finally block; a docker failure there
        # must not replace the attempt's result or abort the batch.
        def boom(*a, **k):
            raise OSError("docker gone")
        monkeypatch.setattr(run, "docker", boom)
        run.cleanup_container("c1", keep=False)  # must not raise

    def test_describe_exception_keeps_docker_stderr(self):
        # "No such image" lives in docker's stderr; without it a missing task
        # image records only a bare "exit status 1" in the metadata.
        exc = subprocess.CalledProcessError(
            1, ["docker", "image", "inspect"], stderr=b"Error: No such image: img")
        text = run.describe_exception(exc)
        assert "CalledProcessError" in text and "No such image" in text

    def test_collect_run_log_writes_atomically(self, tmp_path, monkeypatch):
        class R:
            returncode = 0
            stdout = b'{"type":"agent_settled"}\n'
            stderr = b""
        monkeypatch.setattr(run, "docker_exec", lambda *a, **k: R())
        dest = tmp_path / "run.log"
        run.collect_run_log("c1", dest)
        assert dest.read_bytes() == R.stdout
        assert [f.name for f in tmp_path.iterdir()] == ["run.log"]

    def test_timeout_with_restart_hard_failure_is_collection_error(self, tmp_path, monkeypatch):
        # P0 regression (end to end): docker exec hits the pi timeout, and the
        # stop/inspect calls of the follow-up restart also fail at the docker
        # level; the attempt must end as a retryable collection_error, never
        # as an agent_error written into preds.
        info = {"name": "fake", "image": "img", "image_id": "sha256:x",
                "node_version": "v22", "pi_version": "1.1.0",
                "base_commit": "a" * 40, "image_head": "b" * 40,
                "task_sha256": "c", "env_dir": None}
        monkeypatch.setattr(run, "setup_container", lambda *a, **k: info)

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(cmd, **kwargs):
            if "rm" in cmd:                 # cleanup_container's docker rm
                return R()
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr(run.subprocess, "run", fake_run)
        inst = {"instance_id": "task-1"}
        meta = run.run_one_attempt(inst, "governor", "m", "m", make_args(),
                                   tmp_path, {}, tmp_path, tmp_path, tmp_path / "task-1")
        assert meta["status"] == "collection_error"
        assert meta["timed_out"] is True
        assert meta["container_stable"] is False

    def test_agent_phase_infra_error_is_collection_error(self, tmp_path, monkeypatch):
        info = {"name": "fake", "image": "img", "image_id": "sha256:x",
                "node_version": "v22", "pi_version": "1.1.0",
                "base_commit": "a" * 40, "image_head": "b" * 40,
                "task_sha256": "c", "env_dir": None}
        monkeypatch.setattr(run, "setup_container", lambda *a, **k: info)
        monkeypatch.setattr(run, "cleanup_container", lambda *a, **k: None)

        def boom(*a, **k):
            raise run.InfraError("docker exec failed")
        monkeypatch.setattr(run, "launch_pi", boom)
        inst = {"instance_id": "task-1"}
        meta = run.run_one_attempt(inst, "governor", "m", "m", make_args(),
                                   tmp_path, {}, tmp_path, tmp_path, tmp_path / "task-1")
        assert meta["status"] == "collection_error"

    def test_agent_phase_unexpected_exception_is_collection_error(self, tmp_path, monkeypatch):
        # P0 regression: a non-InfraError exception in the agent phase landed
        # in agent_error -- a RESULT status that entered preds as the model's
        # fault without any classification evidence. It must map to a
        # retryable infrastructure status like every other phase failure.
        info = {"name": "fake", "image": "img", "image_id": "sha256:x",
                "node_version": "v22", "pi_version": "1.1.0",
                "base_commit": "a" * 40, "image_head": "b" * 40,
                "task_sha256": "c", "env_dir": None}
        monkeypatch.setattr(run, "setup_container", lambda *a, **k: info)
        monkeypatch.setattr(run, "cleanup_container", lambda *a, **k: None)

        def boom(*a, **k):
            raise RuntimeError("runner bug")
        monkeypatch.setattr(run, "launch_pi", boom)
        inst = {"instance_id": "task-1"}
        meta = run.run_one_attempt(inst, "governor", "m", "m", make_args(),
                                   tmp_path, {}, tmp_path, tmp_path, tmp_path / "task-1")
        assert meta["status"] == "collection_error"
        assert "RuntimeError" in meta["error"]

    def test_phase_status_never_returns_agent_error(self):
        assert run.phase_status("agent") == "collection_error"
        assert run.phase_status("setup") == "setup_error"
        assert run.phase_status("collect") == "collection_error"
        assert run.phase_status("classify") == "collection_error"
        assert run.phase_status("unknown") == "setup_error"

    def test_collect_run_log_rc_check(self, tmp_path, monkeypatch):
        class R:
            returncode = 1
            stdout = b""
            stderr = b"no such file"
        monkeypatch.setattr(run, "docker_exec", lambda *a, **k: R())
        with pytest.raises(run.InfraError):
            run.collect_run_log("c1", tmp_path / "run.log")

    def test_read_decision_logs_reports_per_file_errors(self, monkeypatch):
        class R:
            def __init__(self, rc, out=b""):
                self.returncode, self.stdout, self.stderr = rc, out, b""

        def fake_exec(name, cmd, timeout=None, check=False):
            if cmd.startswith("ls -1"):
                return R(0, b"a.jsonl\nb.jsonl\n")
            if cmd.endswith("/a.jsonl"):
                return R(0, b'{"decisionKind":"route"}\n')
            return R(1, b"")

        monkeypatch.setattr(run, "docker_exec", fake_exec)
        text, files, errors = run.read_decision_logs("c1")
        assert files == 2
        assert len(errors) == 1 and "b.jsonl" in errors[0]
        assert '"route"' in text

    def test_collect_decisions_writes_and_counts_errors(self, tmp_path, monkeypatch):
        class R:
            def __init__(self, rc, out=b""):
                self.returncode, self.stdout, self.stderr = rc, out, b""

        def fake_exec(name, cmd, timeout=None, check=False):
            if cmd.startswith("ls -1"):
                return R(0, b"d.jsonl\n")
            return R(0, b'{"decisionKind":"permission","effective":"allow"}\n')

        monkeypatch.setattr(run, "docker_exec", fake_exec)
        files, errors = run.collect_decisions("c1", tmp_path / "jev")
        assert files == 1 and errors == []
        assert "permission" in (tmp_path / "jev" / "decisions.jsonl").read_text()

    def test_collect_decisions_clears_stale_file_on_empty_read(self, tmp_path, monkeypatch):
        # P0 regression: inst_dir is reused across retries. When a retry
        # produces no decision logs at all, the previous attempt's
        # decisions.jsonl must be cleared, or parse_decisions would reuse its
        # stale route records as fresh governor evidence.
        jev_dir = tmp_path / "jev"
        jev_dir.mkdir()
        (jev_dir / "decisions.jsonl").write_text(
            '{"decisionKind":"route","effective":"tier=kept"}\n')

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        monkeypatch.setattr(run, "docker_exec", lambda *a, **k: R())
        files, errors = run.collect_decisions("c1", jev_dir)
        assert files == 0 and errors == []
        assert (jev_dir / "decisions.jsonl").read_text() == ""
        assert run.parse_decisions(jev_dir / "decisions.jsonl")["route"] == 0

    def test_retry_cost_tracked_across_attempts(self, tmp_path, monkeypatch):
        # First attempt: infra failure with cost; second: success with cost.
        results = [
            {"status": "setup_error", "error": "x", "cost_usd": 0.1, "instance_id": "task-1",
             "variant": "governor"},
            {"status": "success", "cost_usd": 0.3, "container": "c1", "parse_errors": 0,
             "jev": {"jev_cost_usd": 0.05, "parse_errors": 0}, "instance_id": "task-1",
             "variant": "governor"},
        ]

        def fake_attempt(*a, **k):
            return dict(results.pop(0))
        monkeypatch.setattr(run, "run_one_attempt", fake_attempt)
        inst = {"instance_id": "task-1"}
        meta = run.run_one(inst, "governor", "m", "m", make_args(), tmp_path,
                           "label", {}, tmp_path, tmp_path)
        assert meta["attempts"] == 2
        assert meta["total_cost_usd"] == 0.35               # final attempt
        assert meta["total_cost_usd_all_attempts"] == 0.45  # 0.1 + 0.35
        assert [h["cost_usd"] for h in meta["attempt_history"]] == [0.1, 0.35]
        assert meta["cost_complete"] is True
        # and the preds file was rebuilt once, atomically
        lines = (tmp_path / "preds.jsonl").read_text().splitlines()
        assert len(lines) == 1

    def test_cost_complete_false_when_cost_capture_failed(self, tmp_path, monkeypatch):
        # An attempt that had a container but never captured run.log cannot
        # prove its spend is zero: the all-attempts sum must be flagged as a
        # lower bound, not reported as an exact total.
        results = [
            {"status": "collection_error", "error": "log lost", "container": "c1",
             "instance_id": "task-1", "variant": "governor"},
            {"status": "success", "cost_usd": 0.3, "container": "c1", "parse_errors": 0,
             "jev": {"jev_cost_usd": 0.0, "parse_errors": 0}, "instance_id": "task-1",
             "variant": "governor"},
        ]

        def fake_attempt(*a, **k):
            return dict(results.pop(0))
        monkeypatch.setattr(run, "run_one_attempt", fake_attempt)
        inst = {"instance_id": "task-1"}
        meta = run.run_one(inst, "governor", "m", "m", make_args(), tmp_path,
                           "label", {}, tmp_path, tmp_path)
        assert meta["cost_complete"] is False
        assert [h["cost_collected"] for h in meta["attempt_history"]] == [False, True]

    def test_cost_incomplete_when_decision_reads_failed(self):
        # P1 regression: a decision-log file whose read failed drops that
        # file's JEV spend, yet the remaining records parse cleanly. Without
        # checking decision_read_errors the cost would be reported complete.
        meta = {"container": "c1", "cost_usd": 0.3, "parse_errors": 0,
                "decision_read_errors": ["cat decisions.jsonl failed (rc=1)"],
                "collection_errors": 1,
                "jev": {"jev_cost_usd": 0.05, "parse_errors": 0}}
        assert run.cost_was_collected(make_args(), meta) is False
        meta["decision_read_errors"] = []
        assert run.cost_was_collected(make_args(), meta) is True

    def test_cost_complete_false_when_decision_read_failed(self, tmp_path, monkeypatch):
        # End to end: an attempt that lost a decision-log file to a read error
        # must flag the final metadata's cost_complete, matching the reviewer's
        # minimal reproduction.
        result = {"status": "success", "cost_usd": 0.3, "container": "c1",
                  "parse_errors": 0,
                  "decision_read_errors": ["cat decisions.jsonl failed (rc=1)"],
                  "collection_errors": 1,
                  "jev": {"jev_cost_usd": 0.05, "parse_errors": 0},
                  "instance_id": "task-1", "variant": "governor"}
        monkeypatch.setattr(run, "run_one_attempt", lambda *a, **k: dict(result))
        inst = {"instance_id": "task-1"}
        meta = run.run_one(inst, "governor", "m", "m", make_args(), tmp_path,
                           "label", {}, tmp_path, tmp_path)
        assert meta["cost_complete"] is False
        assert meta["attempt_history"][0]["cost_collected"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
