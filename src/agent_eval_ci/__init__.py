"""Offline CI regression checker for synthetic tool-use traces."""

from .evaluator import (
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_REGRESSION,
    TraceValidationError,
    compare_runs,
    evaluate_scenario,
    load_trace_file,
    render_markdown_report,
)

__all__ = [
    "EXIT_OK",
    "EXIT_REGRESSION",
    "EXIT_INVALID_INPUT",
    "TraceValidationError",
    "load_trace_file",
    "evaluate_scenario",
    "compare_runs",
    "render_markdown_report",
]
