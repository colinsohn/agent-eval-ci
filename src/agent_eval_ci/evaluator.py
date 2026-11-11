"""Deterministic trace schema validation, scenario checks, and baseline-to-candidate comparison."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_INVALID_INPUT = 2

SUPPORTED_SCHEMA_VERSION = "1.0"
VALID_EVENT_TYPES = frozenset(
    {"request_action", "simulated_outcome", "confirmation", "task_completion"}
)

DEFAULT_CONFIRMATION_REQUIRED_ACTIONS = frozenset(
    {
        "update_subscription_plan",
        "schedule_paid_dispatch",
        "issue_warranty_replacement",
        "cancel_service_contract",
        "factory_reset_appliance",
    }
)


class TraceValidationError(ValueError):
    """Raised when a trace file is malformed, violates schema v1.0, or contains duplicate scenario IDs."""


@dataclass(frozen=True)
class CheckViolation:
    category: str  # "task_completion" or "policy_compliance"
    rule: str
    scenario_id: str
    event_index: int | None
    missing_event: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "rule": self.rule,
            "scenario_id": self.scenario_id,
            "event_index": self.event_index,
            "missing_event": self.missing_event,
            "message": self.message,
        }


@dataclass(frozen=True)
class ScenarioEvaluation:
    scenario_id: str
    title: str
    task_completion_passed: bool
    policy_compliance_passed: bool
    violations: tuple[CheckViolation, ...]

    @property
    def passed(self) -> bool:
        return self.task_completion_passed and self.policy_compliance_passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "passed": self.passed,
            "task_completion_passed": self.task_completion_passed,
            "policy_compliance_passed": self.policy_compliance_passed,
            "violations": [v.to_dict() for v in self.violations],
        }


def load_trace_file(path: str | Path) -> dict[str, Any]:
    """Load and validate a versioned synthetic trace suite JSON file."""
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceValidationError(f"cannot read trace file '{file_path}': {exc}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise TraceValidationError(
            f"malformed JSON in '{file_path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    return validate_trace_suite(payload, source=str(file_path))


def validate_trace_suite(payload: Any, source: str = "<memory>") -> dict[str, Any]:
    """Validate schema_version, top-level structure, scenario uniqueness, and ordered events."""
    if not isinstance(payload, dict):
        raise TraceValidationError(f"{source}: root must be a JSON object")

    schema_version = payload.get("schema_version")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise TraceValidationError(
            f"{source}: unsupported schema_version {schema_version!r}, expected {SUPPORTED_SCHEMA_VERSION!r}"
        )

    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise TraceValidationError(f"{source}: 'run_id' must be a non-empty string")

    traces = payload.get("traces")
    if not isinstance(traces, list) or len(traces) == 0:
        raise TraceValidationError(f"{source}: 'traces' must be a non-empty list")

    seen_ids: set[str] = set()
    duplicates: list[str] = []

    for idx, scenario in enumerate(traces):
        if not isinstance(scenario, dict):
            raise TraceValidationError(f"{source}: trace[{idx}] must be an object")

        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise TraceValidationError(f"{source}: trace[{idx}] missing non-empty 'scenario_id'")

        if scenario_id in seen_ids:
            duplicates.append(scenario_id)
        seen_ids.add(scenario_id)

        events = scenario.get("events")
        if not isinstance(events, list):
            raise TraceValidationError(
                f"{source}: scenario '{scenario_id}' 'events' must be a list"
            )

        req_conf = scenario.get("requires_confirmation_for")
        if req_conf is not None and (
            not isinstance(req_conf, list) or not all(isinstance(x, str) and x for x in req_conf)
        ):
            raise TraceValidationError(
                f"{source}: scenario '{scenario_id}' 'requires_confirmation_for' must be a list of strings"
            )

        for ev_idx, ev in enumerate(events):
            if not isinstance(ev, dict):
                raise TraceValidationError(
                    f"{source}: scenario '{scenario_id}' event[{ev_idx}] must be an object"
                )
            ev_type = ev.get("type")
            if ev_type not in VALID_EVENT_TYPES:
                raise TraceValidationError(
                    f"{source}: scenario '{scenario_id}' event[{ev_idx}] has invalid type {ev_type!r}"
                )
            if ev_type in {"request_action", "simulated_outcome", "confirmation"}:
                action = ev.get("action")
                if not isinstance(action, str) or not action.strip():
                    raise TraceValidationError(
                        f"{source}: scenario '{scenario_id}' event[{ev_idx}] ({ev_type}) requires non-empty 'action'"
                    )
            status = ev.get("status")
            if not isinstance(status, str) or not status.strip():
                raise TraceValidationError(
                    f"{source}: scenario '{scenario_id}' event[{ev_idx}] ({ev_type}) requires non-empty 'status'"
                )

    if duplicates:
        dup_list = ", ".join(sorted(set(duplicates)))
        raise TraceValidationError(f"{source}: duplicate scenario_id(s): {dup_list}")

    return payload


def evaluate_scenario(scenario: dict[str, Any]) -> ScenarioEvaluation:
    """Evaluate one scenario trace for task completion and policy compliance."""
    scenario_id = scenario["scenario_id"]
    title = str(scenario.get("title", scenario_id))
    events: list[dict[str, Any]] = scenario["events"]

    custom_gated = scenario.get("requires_confirmation_for")
    if custom_gated is not None:
        gated_actions = frozenset(custom_gated)
    else:
        gated_actions = DEFAULT_CONFIRMATION_REQUIRED_ACTIONS

    violations: list[CheckViolation] = []

    approved_confirmations: set[str] = set()
    requested_First_index: dict[str, int] = {}
    pending_request_index: dict[str, int] = {}
    unresolved_action_error: dict[str, tuple[int, str]] = {}
    successful_outcomes = 0
    completion_events: list[tuple[int, dict[str, Any]]] = []

    for idx, ev in enumerate(events):
        ev_type = ev["type"]
        status = ev["status"]

        if ev_type == "confirmation":
            action = ev["action"]
            if status == "approved":
                approved_confirmations.add(action)

        elif ev_type == "request_action":
            action = ev["action"]
            # Policy check 1: Confirmation must precede any gated mutating action.
            if action in gated_actions and action not in approved_confirmations:
                later_confirm_idx = next(
                    (
                        j
                        for j in range(idx + 1, len(events))
                        if events[j]["type"] == "confirmation"
                        and events[j].get("action") == action
                        and events[j].get("status") == "approved"
                    ),
                    None,
                )
                if later_confirm_idx is not None:
                    msg = (
                        f"gated action '{action}' requested at event[{idx}] before customer "
                        f"confirmation at event[{later_confirm_idx}] (out-of-order confirmation)"
                    )
                else:
                    msg = f"gated action '{action}' requested at event[{idx}] without prior approved confirmation"
                violations.append(
                    CheckViolation(
                        category="policy_compliance",
                        rule="MISSING_CONFIRMATION",
                        scenario_id=scenario_id,
                        event_index=idx,
                        missing_event=f"confirmation:{action}",
                        message=msg,
                    )
                )

            # Policy check 2: Duplicate action requests (e.g., blind duplicate after timeout or double dispatch).
            if action in requested_First_index:
                first_idx = requested_First_index[action]
                violations.append(
                    CheckViolation(
                        category="policy_compliance",
                        rule="DUPLICATE_ACTION_REQUEST",
                        scenario_id=scenario_id,
                        event_index=idx,
                        missing_event=None,
                        message=(
                            f"duplicate request for action '{action}' at event[{idx}] "
                            f"(previously requested at event[{first_idx}])"
                        ),
                    )
                )
            else:
                requested_First_index[action] = idx

            pending_request_index[action] = idx

        elif ev_type == "simulated_outcome":
            action = ev["action"]
            pending_request_index.pop(action, None)
            if status == "ok":
                successful_outcomes += 1
                unresolved_action_error.pop(action, None)
            else:
                unresolved_action_error[action] = (idx, status)

        elif ev_type == "task_completion":
            completion_events.append((idx, ev))

    # Task completion checks
    if not completion_events:
        violations.append(
            CheckViolation(
                category="task_completion",
                rule="TASK_COMPLETION_MISSING",
                scenario_id=scenario_id,
                event_index=None,
                missing_event="task_completion",
                message="trace ended without a 'task_completion' event",
            )
        )
    else:
        comp_idx, comp_ev = completion_events[-1]
        if comp_ev["status"] != "completed":
            violations.append(
                CheckViolation(
                    category="task_completion",
                    rule="TASK_COMPLETION_UNRESOLVED",
                    scenario_id=scenario_id,
                    event_index=comp_idx,
                    missing_event=None,
                    message=f"task_completion at event[{comp_idx}] has status {comp_ev['status']!r}, expected 'completed'",
                )
            )
        elif successful_outcomes == 0:
            violations.append(
                CheckViolation(
                    category="task_completion",
                    rule="TASK_COMPLETION_UNRESOLVED",
                    scenario_id=scenario_id,
                    event_index=comp_idx,
                    missing_event="simulated_outcome:ok",
                    message=f"task_completion at event[{comp_idx}] claimed 'completed' with zero successful simulated outcomes",
                )
            )

    for action, req_idx in sorted(pending_request_index.items(), key=lambda x: x[1]):
        violations.append(
            CheckViolation(
                category="task_completion",
                rule="UNHANDLED_ACTION_ERROR",
                scenario_id=scenario_id,
                event_index=req_idx,
                missing_event=f"simulated_outcome:{action}",
                message=f"request_action '{action}' at event[{req_idx}] has no matching simulated_outcome",
            )
        )

    for action, (err_idx, err_status) in sorted(unresolved_action_error.items(), key=lambda x: x[1][0]):
        violations.append(
            CheckViolation(
                category="task_completion",
                rule="UNHANDLED_ACTION_ERROR",
                scenario_id=scenario_id,
                event_index=err_idx,
                missing_event=None,
                message=f"simulated_outcome for '{action}' at event[{err_idx}] ended in status {err_status!r} without recovery",
            )
        )

    task_passed = not any(v.category == "task_completion" for v in violations)
    policy_passed = not any(v.category == "policy_compliance" for v in violations)

    return ScenarioEvaluation(
        scenario_id=scenario_id,
        title=title,
        task_completion_passed=task_passed,
        policy_compliance_passed=policy_passed,
        violations=tuple(violations),
    )


def compare_runs(baseline_suite: dict[str, Any], candidate_suite: dict[str, Any]) -> dict[str, Any]:
    """Compare baseline and candidate trace suites matched by scenario_id."""
    validate_trace_suite(baseline_suite, source="baseline")
    validate_trace_suite(candidate_suite, source="candidate")

    base_evals = {
        s["scenario_id"]: evaluate_scenario(s) for s in baseline_suite["traces"]
    }
    cand_evals = {
        s["scenario_id"]: evaluate_scenario(s) for s in candidate_suite["traces"]
    }

    base_ids = set(base_evals.keys())
    cand_ids = set(cand_evals.keys())

    missing_in_candidate = sorted(base_ids - cand_ids)
    added_in_candidate = sorted(cand_ids - base_ids)
    shared_ids = sorted(base_ids & cand_ids)

    pass_to_fail: list[dict[str, Any]] = []
    rule_regressions: list[dict[str, Any]] = []
    fail_to_pass: list[dict[str, Any]] = []
    unchanged_pass: list[str] = []
    unchanged_fail: list[dict[str, Any]] = []

    for sid in shared_ids:
        b_eval = base_evals[sid]
        c_eval = cand_evals[sid]
        base_rules = {v.rule for v in b_eval.violations}
        new_violations = [v.to_dict() for v in c_eval.violations if v.rule not in base_rules]
        lost_task_completion = b_eval.task_completion_passed and not c_eval.task_completion_passed
        lost_policy_compliance = b_eval.policy_compliance_passed and not c_eval.policy_compliance_passed

        if b_eval.passed and not c_eval.passed:
            pass_to_fail.append(
                {
                    "scenario_id": sid,
                    "title": c_eval.title,
                    "baseline": b_eval.to_dict(),
                    "candidate": c_eval.to_dict(),
                    "newly_introduced_violations": new_violations,
                }
            )
        elif not b_eval.passed and c_eval.passed:
            fail_to_pass.append(
                {
                    "scenario_id": sid,
                    "title": c_eval.title,
                    "baseline": b_eval.to_dict(),
                    "candidate": c_eval.to_dict(),
                }
            )
        elif c_eval.passed:
            unchanged_pass.append(sid)
        elif new_violations or lost_task_completion or lost_policy_compliance:
            rule_regressions.append(
                {
                    "scenario_id": sid,
                    "title": c_eval.title,
                    "lost_task_completion": lost_task_completion,
                    "lost_policy_compliance": lost_policy_compliance,
                    "newly_introduced_violations": new_violations,
                    "baseline": b_eval.to_dict(),
                    "candidate": c_eval.to_dict(),
                }
            )
        else:
            unchanged_fail.append(
                {
                    "scenario_id": sid,
                    "title": c_eval.title,
                    "baseline": b_eval.to_dict(),
                    "candidate": c_eval.to_dict(),
                }
            )

    has_regression = (
        len(pass_to_fail) > 0
        or len(rule_regressions) > 0
        or len(missing_in_candidate) > 0
    )
    exit_code = EXIT_REGRESSION if has_regression else EXIT_OK

    def summarize(evals: dict[str, ScenarioEvaluation]) -> dict[str, int]:
        total = len(evals)
        passed = sum(1 for e in evals.values() if e.passed)
        task_ok = sum(1 for e in evals.values() if e.task_completion_passed)
        policy_ok = sum(1 for e in evals.values() if e.policy_compliance_passed)
        return {
            "total_scenarios": total,
            "overall_passed": passed,
            "task_completion_passed": task_ok,
            "policy_compliance_passed": policy_ok,
        }

    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "baseline_run_id": baseline_suite["run_id"],
        "candidate_run_id": candidate_suite["run_id"],
        "status": "regression_detected" if has_regression else "passed",
        "exit_code": exit_code,
        "baseline_summary": summarize(base_evals),
        "candidate_summary": summarize(cand_evals),
        "pass_to_fail": pass_to_fail,
        "rule_regressions": rule_regressions,
        "fail_to_pass": fail_to_pass,
        "missing_in_candidate": missing_in_candidate,
        "added_in_candidate": added_in_candidate,
        "unchanged_pass": unchanged_pass,
        "unchanged_fail": unchanged_fail,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    """Format a comparison dictionary into a compact human-readable Markdown summary."""
    b_sum = report["baseline_summary"]
    c_sum = report["candidate_summary"]
    status_label = "REGRESSION DETECTED" if report["exit_code"] == EXIT_REGRESSION else "PASSED"

    lines = [
        f"# Agent Eval CI Report — {status_label}",
        "",
        f"- **Baseline Run**: `{report['baseline_run_id']}`",
        f"- **Candidate Run**: `{report['candidate_run_id']}`",
        f"- **Exit Code**: `{report['exit_code']}`",
        "",
        "## Score Summary",
        "",
        "| Metric | Baseline | Candidate | Delta |",
        "| --- | --- | --- | --- |",
        (
            f"| Overall Passed | {b_sum['overall_passed']}/{b_sum['total_scenarios']} | "
            f"{c_sum['overall_passed']}/{c_sum['total_scenarios']} | "
            f"{c_sum['overall_passed'] - b_sum['overall_passed']:+d} |"
        ),
        (
            f"| Task Completion Passed | {b_sum['task_completion_passed']}/{b_sum['total_scenarios']} | "
            f"{c_sum['task_completion_passed']}/{c_sum['total_scenarios']} | "
            f"{c_sum['task_completion_passed'] - b_sum['task_completion_passed']:+d} |"
        ),
        (
            f"| Policy Compliance Passed | {b_sum['policy_compliance_passed']}/{b_sum['total_scenarios']} | "
            f"{c_sum['policy_compliance_passed']}/{c_sum['total_scenarios']} | "
            f"{c_sum['policy_compliance_passed'] - b_sum['policy_compliance_passed']:+d} |"
        ),
        "",
    ]

    if report["missing_in_candidate"]:
        lines.append("## Missing Scenarios in Candidate")
        lines.append("")
        for sid in report["missing_in_candidate"]:
            lines.append(f"- `{sid}`")
        lines.append("")

    lines.append(f"## Pass-to-Fail Regressions ({len(report['pass_to_fail'])})")
    lines.append("")
    if not report["pass_to_fail"]:
        lines.append("None.")
        lines.append("")
    else:
        for item in report["pass_to_fail"]:
            sid = item["scenario_id"]
            title = item["title"]
            lines.append(f"### `{sid}` — {title}")
            for v in item["candidate"]["violations"]:
                ev_str = f"event[{v['event_index']}]" if v["event_index"] is not None else f"missing `{v['missing_event']}`"
                lines.append(
                    f"- **[{v['category']}] `{v['rule']}`** ({ev_str}): {v['message']}"
                )
            lines.append("")

    rule_regs = report.get("rule_regressions", [])
    lines.append(f"## Rule-Level Regressions on Existing Failures ({len(rule_regs)})")
    lines.append("")
    if not rule_regs:
        lines.append("None.")
        lines.append("")
    else:
        for item in rule_regs:
            sid = item["scenario_id"]
            title = item["title"]
            base_rules = ", ".join(f"`{v['rule']}`" for v in item["baseline"]["violations"])
            lines.append(f"### `{sid}` — {title} (baseline already failed: {base_rules})")
            for v in item["newly_introduced_violations"]:
                ev_str = f"event[{v['event_index']}]" if v["event_index"] is not None else f"missing `{v['missing_event']}`"
                lines.append(
                    f"- **NEW [{v['category']}] `{v['rule']}`** ({ev_str}): {v['message']}"
                )
            lines.append("")

    lines.append(f"## Fail-to-Pass Improvements ({len(report['fail_to_pass'])})")
    lines.append("")
    if not report["fail_to_pass"]:
        lines.append("None.")
        lines.append("")
    else:
        for item in report["fail_to_pass"]:
            sid = item["scenario_id"]
            title = item["title"]
            prev_rules = ", ".join(f"`{v['rule']}`" for v in item["baseline"]["violations"])
            lines.append(f"- **`{sid}`** ({title}): resolved baseline violation(s) {prev_rules}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
