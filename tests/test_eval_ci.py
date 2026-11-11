"""Tests for agent-eval-ci trace validation, policy/completion checks, and CLI exit codes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_eval_ci.cli import main
from agent_eval_ci.evaluator import (
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_REGRESSION,
    TraceValidationError,
    compare_runs,
    evaluate_scenario,
    load_trace_file,
    render_markdown_report,
    validate_trace_suite,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
BASELINE_PATH = FIXTURES_DIR / "baseline_traces.json"
CANDIDATE_FAIL_PATH = FIXTURES_DIR / "candidate_traces.json"
CANDIDATE_CLEAN_PATH = FIXTURES_DIR / "candidate_clean_traces.json"


def test_clean_comparison_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--baseline", str(BASELINE_PATH), "--candidate", str(CANDIDATE_CLEAN_PATH)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "PASSED" in captured.out
    assert "Pass-to-Fail Regressions (0)" in captured.out
    assert "APPL-009" in captured.out  # fail-to-pass improvement surfaced


def test_failing_demo_exits_regression_and_identifies_events(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    json_out = tmp_path / "report.json"
    code = main(
        [
            "--baseline",
            str(BASELINE_PATH),
            "--candidate",
            str(CANDIDATE_FAIL_PATH),
            "--json-out",
            str(json_out),
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_REGRESSION
    assert "REGRESSION DETECTED" in captured.out

    report = json.loads(json_out.read_text(encoding="utf-8"))
    regressed_ids = [item["scenario_id"] for item in report["pass_to_fail"]]
    assert regressed_ids == ["APPL-002", "APPL-003", "APPL-004", "APPL-005"]

    by_id = {item["scenario_id"]: item["candidate"]["violations"] for item in report["pass_to_fail"]}
    # APPL-002: out-of-order confirmation at event[2]
    assert by_id["APPL-002"][0]["rule"] == "MISSING_CONFIRMATION"
    assert by_id["APPL-002"][0]["event_index"] == 2
    assert "out-of-order confirmation" in by_id["APPL-002"][0]["message"]

    # APPL-003: missing confirmation at event[2]
    assert by_id["APPL-003"][0]["rule"] == "MISSING_CONFIRMATION"
    assert by_id["APPL-003"][0]["event_index"] == 2

    # APPL-004: duplicate action after timeout at event[5]
    assert by_id["APPL-004"][0]["rule"] == "DUPLICATE_ACTION_REQUEST"
    assert by_id["APPL-004"][0]["event_index"] == 5

    # APPL-005: missing task_completion and unhandled error at event[1]
    rules_005 = {v["rule"]: v for v in by_id["APPL-005"]}
    assert "TASK_COMPLETION_MISSING" in rules_005
    assert rules_005["TASK_COMPLETION_MISSING"]["missing_event"] == "task_completion"
    assert "UNHANDLED_ACTION_ERROR" in rules_005
    assert rules_005["UNHANDLED_ACTION_ERROR"]["event_index"] == 1

    # APPL-009: rule-level regression where baseline failed policy_compliance and candidate additionally lost task_completion
    assert len(report["rule_regressions"]) == 1
    reg_009 = report["rule_regressions"][0]
    assert reg_009["scenario_id"] == "APPL-009"
    assert reg_009["lost_task_completion"] is True
    new_rules_009 = {v["rule"]: v for v in reg_009["newly_introduced_violations"]}
    assert "TASK_COMPLETION_UNRESOLVED" in new_rules_009
    assert new_rules_009["TASK_COMPLETION_UNRESOLVED"]["event_index"] == 4
    assert "UNHANDLED_ACTION_ERROR" in new_rules_009
    assert new_rules_009["UNHANDLED_ACTION_ERROR"]["event_index"] == 3


def test_out_of_order_confirmation_rule() -> None:
    scenario = {
        "scenario_id": "TEST-OOO",
        "title": "Out of order confirmation",
        "events": [
            {"type": "request_action", "action": "update_subscription_plan", "status": "requested"},
            {"type": "simulated_outcome", "action": "update_subscription_plan", "status": "ok"},
            {"type": "confirmation", "action": "update_subscription_plan", "status": "approved"},
            {"type": "task_completion", "status": "completed"},
        ],
    }
    res = evaluate_scenario(scenario)
    assert res.task_completion_passed is True
    assert res.policy_compliance_passed is False
    assert len(res.violations) == 1
    assert res.violations[0].rule == "MISSING_CONFIRMATION"
    assert res.violations[0].event_index == 0
    assert "event[2]" in res.violations[0].message


def test_duplicated_action_rule() -> None:
    scenario = {
        "scenario_id": "TEST-DUP",
        "title": "Duplicate action after timeout",
        "events": [
            {"type": "confirmation", "action": "schedule_paid_dispatch", "status": "approved"},
            {"type": "request_action", "action": "schedule_paid_dispatch", "status": "requested"},
            {"type": "simulated_outcome", "action": "schedule_paid_dispatch", "status": "timeout"},
            {"type": "request_action", "action": "schedule_paid_dispatch", "status": "requested"},
            {"type": "simulated_outcome", "action": "schedule_paid_dispatch", "status": "ok"},
            {"type": "task_completion", "status": "completed"},
        ],
    }
    res = evaluate_scenario(scenario)
    assert res.task_completion_passed is True
    assert res.policy_compliance_passed is False
    assert len(res.violations) == 1
    assert res.violations[0].rule == "DUPLICATE_ACTION_REQUEST"
    assert res.violations[0].event_index == 3


def test_missing_and_duplicate_scenario_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = load_trace_file(BASELINE_PATH)
    candidate_missing = copy.deepcopy(baseline)
    # Drop the last scenario so candidate is missing APPL-020
    candidate_missing["traces"] = candidate_missing["traces"][:-1]

    report = compare_runs(baseline, candidate_missing)
    assert report["missing_in_candidate"] == ["APPL-020"]
    assert report["exit_code"] == EXIT_REGRESSION

    # Duplicate scenario IDs must fail validation with EXIT_INVALID_INPUT
    dup_suite = copy.deepcopy(baseline)
    dup_suite["traces"].append(copy.deepcopy(dup_suite["traces"][0]))
    dup_path = tmp_path / "dup.json"
    dup_path.write_text(json.dumps(dup_suite), encoding="utf-8")

    code = main(["--baseline", str(BASELINE_PATH), "--candidate", str(dup_path)])
    captured = capsys.readouterr()
    assert code == EXIT_INVALID_INPUT
    assert "duplicate scenario_id" in captured.err


def test_malformed_input_returns_invalid_input_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_json = tmp_path / "malformed.json"
    bad_json.write_text("{not valid json", encoding="utf-8")

    code = main(["--baseline", str(bad_json), "--candidate", str(CANDIDATE_CLEAN_PATH)])
    captured = capsys.readouterr()
    assert code == EXIT_INVALID_INPUT
    assert "malformed JSON" in captured.err

    bad_schema = tmp_path / "bad_schema.json"
    bad_schema.write_text(json.dumps({"schema_version": "9.9", "run_id": "x", "traces": []}), encoding="utf-8")
    with pytest.raises(TraceValidationError, match="unsupported schema_version"):
        validate_trace_suite(json.loads(bad_schema.read_text(encoding="utf-8")))


def test_stable_output_across_replays() -> None:
    baseline = load_trace_file(BASELINE_PATH)
    candidate = load_trace_file(CANDIDATE_FAIL_PATH)

    first_report = compare_runs(baseline, candidate)
    first_md = render_markdown_report(first_report)
    first_json = json.dumps(first_report, indent=2)

    for _ in range(5):
        # Reverse scenario trace order in memory to confirm order-independent matching by scenario_id
        shuffled_cand = copy.deepcopy(candidate)
        shuffled_cand["traces"] = list(reversed(shuffled_cand["traces"]))
        rep = compare_runs(baseline, shuffled_cand)
        assert json.dumps(rep, indent=2) == first_json
        assert render_markdown_report(rep) == first_md


def test_appl_009_rule_level_regression_when_baseline_already_failed_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reproduce APPL-009 where baseline fails policy_compliance and candidate additionally loses task_completion."""
    baseline = load_trace_file(BASELINE_PATH)
    candidate = copy.deepcopy(baseline)

    for sc in candidate["traces"]:
        if sc["scenario_id"] == "APPL-009":
            # Keep baseline's missing confirmation, and additionally lose task_completion
            sc["events"] = [
                {"type": "request_action", "action": "backup_schedule_profile", "status": "requested"},
                {"type": "simulated_outcome", "action": "backup_schedule_profile", "status": "ok"},
                {"type": "request_action", "action": "factory_reset_appliance", "status": "requested"},
                {"type": "simulated_outcome", "action": "factory_reset_appliance", "status": "error"},
                {"type": "task_completion", "status": "unresolved"},
            ]

    cand_path = tmp_path / "cand_appl009_regressed.json"
    cand_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")

    code = main(["--baseline", str(BASELINE_PATH), "--candidate", str(cand_path)])
    captured = capsys.readouterr()

    assert code == EXIT_REGRESSION
    assert "APPL-009" in captured.out
    assert "TASK_COMPLETION_UNRESOLVED" in captured.out
    assert "UNHANDLED_ACTION_ERROR" in captured.out

