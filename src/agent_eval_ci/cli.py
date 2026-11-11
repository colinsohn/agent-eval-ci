"""CLI entry point for agent-eval-ci."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .evaluator import (
    EXIT_INVALID_INPUT,
    TraceValidationError,
    compare_runs,
    load_trace_file,
    render_markdown_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-eval-ci",
        description="Offline CI regression checker for recorded synthetic tool-use traces.",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help="Path to the baseline JSON trace suite.",
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="Path to the candidate JSON trace suite.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Primary stdout report format (default: markdown).",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional path to write machine-readable JSON comparison output.",
    )
    parser.add_argument(
        "--markdown-out",
        default=None,
        help="Optional path to write Markdown summary output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_INVALID_INPUT

    try:
        baseline = load_trace_file(args.baseline)
        candidate = load_trace_file(args.candidate)
        report = compare_runs(baseline, candidate)
    except TraceValidationError as exc:
        sys.stderr.write(f"INVALID_INPUT: {exc}\n")
        return EXIT_INVALID_INPUT

    json_text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    md_text = render_markdown_report(report)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json_text, encoding="utf-8")

    if args.markdown_out:
        md_path = Path(args.markdown_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md_text, encoding="utf-8")

    if args.format == "json":
        sys.stdout.write(json_text)
    else:
        sys.stdout.write(md_text)

    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
