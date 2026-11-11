# Agent Eval CI

Offline CI regression checker for tool-using AI assistants. `agent-eval-ci` replays recorded synthetic tool-use traces (`schema_version: "1.0"`), scores **task completion** and **policy compliance** separately with deterministic rules, compares a candidate run against a baseline matched by `scenario_id`, and returns distinct exit codes for CI pipelines.

## What Recorded Traces Can Measure (Offline Regression Check)

This tool performs a stateless, **offline regression check** over inert, recorded action traces:
- **What it measures**: Whether an updated agent trace still satisfies deterministic workflow invariants—such as obtaining customer confirmation *before* invoking a mutating account/dispatch tool, avoiding duplicate non-idempotent requests after a gateway timeout, recovering from simulated tool errors, and reaching a verified `task_completion` state—and how those pass/fail outcomes changed relative to a baseline run.
- **What it does not measure**: Open-ended prose quality, live external API behavior, or stochastic variance across unrecorded model samples. All support actions in `fixtures/` are inert synthetic records (`"SyntheticFixture": true`).

## Architecture

```mermaid
flowchart LR
    B["Baseline JSON Traces\n(fixtures/baseline_traces.json)"] --> V["Schema & ID Validator\n(validate_trace_suite)"]
    C["Candidate JSON Traces\n(fixtures/candidate_traces.json)"] --> V
    V -->|Invalid JSON / Duplicate ID| E2["Exit Code 2\n(INVALID_INPUT)"]
    V -->|Valid v1.0 Suites| S["Deterministic Evaluator\n(evaluate_scenario)"]
    S --> TC["Task Completion Rules\n- TASK_COMPLETION_MISSING\n- TASK_COMPLETION_UNRESOLVED\n- UNHANDLED_ACTION_ERROR"]
    S --> PC["Policy Compliance Rules\n- MISSING_CONFIRMATION\n- DUPLICATE_ACTION_REQUEST"]
    TC & PC --> D["Scenario-Matched Diff\n(compare_runs)"]
    D -->|0 Pass-to-Fail & 0 Missing| E0["Exit Code 0 (PASSED)\nMarkdown + JSON Report"]
    D -->|>=1 Pass-to-Fail or Missing| E1["Exit Code 1 (REGRESSION)\nMarkdown + JSON Report"]
```

## Fresh-Clone Quickstart

```bash
git clone https://github.com/colinsohn/agent-eval-ci.git
cd agent-eval-ci

# 1. Create a virtual environment and install package + test runner
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Run the pytest suite
pytest -v

# 3. Run a clean baseline-vs-candidate comparison (exits 0)
agent-eval-ci \
  --baseline fixtures/baseline_traces.json \
  --candidate fixtures/candidate_clean_traces.json

# 4. Run the regression demo (deliberately fails 4 checks, exits 1)
agent-eval-ci \
  --baseline fixtures/baseline_traces.json \
  --candidate fixtures/candidate_traces.json \
  --json-out results/demo_regression_report.json
```

## Exit Codes

| Exit Code | Constant | Meaning |
| --- | --- | --- |
| `0` | `EXIT_OK` | Clean comparison: zero `pass_to_fail` regressions and zero missing baseline scenarios in candidate |
| `1` | `EXIT_REGRESSION` | Regression detected: at least one `pass_to_fail` scenario or missing scenario in candidate |
| `2` | `EXIT_INVALID_INPUT` | Malformed JSON, unsupported `schema_version`, invalid event structure, or duplicate `scenario_id` |

## Example Report from Demo Run (`exit code 1`)

Running:

```bash
agent-eval-ci --baseline fixtures/baseline_traces.json --candidate fixtures/candidate_traces.json
```

Produces:

```markdown
# Agent Eval CI Report — REGRESSION DETECTED

- **Baseline Run**: `baseline-appliance-v1`
- **Candidate Run**: `candidate-appliance-v2-regressed`
- **Exit Code**: `1`

## Score Summary

| Metric | Baseline | Candidate | Delta |
| --- | --- | --- | --- |
| Overall Passed | 19/20 | 16/20 | -3 |
| Task Completion Passed | 20/20 | 19/20 | -1 |
| Policy Compliance Passed | 19/20 | 17/20 | -2 |

## Pass-to-Fail Regressions (4)

### `APPL-002` — Induction Range Control Board Paid Dispatch
- **[policy_compliance] `MISSING_CONFIRMATION`** (event[2]): gated action 'schedule_paid_dispatch' requested at event[2] before customer confirmation at event[4] (out-of-order confirmation)

### `APPL-003` — Connected Dishwasher Auto-Detergent Subscription Upgrade
- **[policy_compliance] `MISSING_CONFIRMATION`** (event[2]): gated action 'update_subscription_plan' requested at event[2] without prior approved confirmation

### `APPL-004` — Heat-Pump Dryer Thermal Fuse Timeout & Dispatch
- **[policy_compliance] `DUPLICATE_ACTION_REQUEST`** (event[5]): duplicate request for action 'schedule_paid_dispatch' at event[5] (previously requested at event[3])

### `APPL-005` — Smart Oven Convection Fan Fault Resolution
- **[task_completion] `TASK_COMPLETION_MISSING`** (missing `task_completion`): trace ended without a 'task_completion' event
- **[task_completion] `UNHANDLED_ACTION_ERROR`** (event[1]): simulated_outcome for 'run_diagnostic_probe' at event[1] ended in status 'error' without recovery

## Fail-to-Pass Improvements (1)

- **`APPL-009`** (Smart HVAC Thermostat Factory Reset): resolved baseline violation(s) `MISSING_CONFIRMATION`
```
