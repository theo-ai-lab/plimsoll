from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from plimsoll import __version__
from plimsoll.adapters import load_adapter_traces
from plimsoll.cascade import cascade_telemetry, cascade_to_dict
from plimsoll.diff import trajectory_diff
from plimsoll.governor import Decision, Governor, PlanFeasibility, coerce_partial_trace
from plimsoll.io import load_json, load_policy, load_traces, write_json
from plimsoll.models import TraceRun, ValidationError
from plimsoll.otel import load_otel_traces
from plimsoll.passk import aggregate_pass_k
from plimsoll.policy import infer_policy
from plimsoll.report import (
    build_case_report,
    render_markdown,
    report_to_dict,
    summarize,
    write_html_report,
    write_junit_report,
    write_markdown_report,
    write_sarif_report,
)
from plimsoll.rules import evaluate_trace

# CI exit-code contract (matches ruff/eslint/pytest convention):
#   0 = clean (no failing findings)
#   1 = failing findings present (high/critical)
#   2 = the tool could not run (bad input/usage)
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "init-policy":
            return init_policy_command(args)
        if args.command == "governor":
            return governor_command(args)
        parser.print_help()
        return EXIT_ERROR
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plimsoll",
        description="Run deterministic reliability checks on local AI agent traces.",
        epilog="Exit codes: 0 = clean, 1 = failing findings, 2 = tool error. "
        "Use --exit-zero to always exit 0 (report-only mode).",
    )
    parser.add_argument("--version", action="version", version=f"plimsoll {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="evaluate trace files and write JSON/HTML/JUnit/SARIF reports")
    run.add_argument("--input", required=True, type=Path, help="trace JSON file or directory")
    run.add_argument("--format", choices=supported_formats(), default="native", help="input trace format")
    run.add_argument("--policy", type=Path, help="policy JSON file")
    run.add_argument("--baseline", type=Path, help="baseline trace file or directory")
    run.add_argument(
        "--baseline-format", choices=supported_formats(), help="baseline trace format, defaults to --format"
    )
    run.add_argument("--out", required=True, type=Path, help="output directory")
    run.add_argument("--junit", nargs="?", const=True, default=None, help="write JUnit XML, optionally to a path")
    run.add_argument("--sarif", nargs="?", const=True, default=None, help="write SARIF JSON, optionally to a path")
    run.add_argument(
        "--md",
        nargs="?",
        const=True,
        default=None,
        help="write a Markdown summary (for PR comments), optionally to a path",
    )
    run.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="(deprecated; failing findings now exit non-zero by default) kept for backward compatibility",
    )
    run.add_argument(
        "--exit-zero",
        action="store_true",
        help="always exit 0 even when findings exist (report-only mode; the report still records every finding)",
    )
    run.add_argument(
        "--passk",
        type=int,
        default=None,
        metavar="K",
        help="aggregate pass^1..pass^K reliability over repeated runs of the same case_id "
        "(tau-Bench: pass^K = fraction of tasks whose every recorded run passed). "
        "Defaults to the minimum runs-per-task. Report-only unless --passk-threshold is set.",
    )
    run.add_argument(
        "--passk-threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help="reliability floor in [0,1]: fail the run when the pass^K point estimate falls below it "
        "(model-free combinatorial gate). Enables pass^k aggregation even without --passk.",
    )
    run.add_argument(
        "--reliability-sla",
        dest="reliability_sla",
        type=float,
        default=None,
        metavar="FLOAT",
        help="reliability SLA in [0,1]: fail the run when the LOWER Wilson confidence band on pass^K "
        "misses it (honest worst-case gate — a lucky small-n run cannot sneak past). Also reports "
        "the reliability decay curve, k* (largest k still clearing the SLA), and the Meltdown Onset Point.",
    )
    run.add_argument(
        "--reliability-confidence",
        dest="reliability_confidence",
        type=float,
        default=0.95,
        metavar="FLOAT",
        help="confidence level in (0,1) for the Wilson reliability band (default 0.95).",
    )
    run.add_argument(
        "--cascade",
        action="store_true",
        help="emit cheap->expensive cascade telemetry (gate vs full audit: alpha / disagreement / "
        "lossless), measured deterministically with zero model spend, into report.json.",
    )
    run.add_argument(
        "--json", dest="as_json", action="store_true", help="print a machine-readable JSON summary to stdout"
    )
    run.add_argument("-q", "--quiet", action="store_true", help="suppress the human-readable summary line")
    run.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="control ANSI color in the summary line (default: auto; honors NO_COLOR/FORCE_COLOR)",
    )
    run.add_argument("--no-color", action="store_true", help="alias for --color never")

    init = sub.add_parser("init-policy", help="infer a starter policy from observed traces")
    init.add_argument("--input", required=True, type=Path, help="trace JSON file or directory")
    init.add_argument("--format", choices=supported_formats(), default="native", help="input trace format")
    init.add_argument("--out", required=True, type=Path, help="policy output JSON path")

    governor = sub.add_parser(
        "governor",
        help="gate one proposed tool call against a policy BEFORE it runs (deterministic, offline)",
        description="Pre-execution gate: decide whether a single proposed tool call may run, given the "
        "calls that already ran. Reuses the same deterministic rule engine as 'run', evaluated at the "
        "gate. Exits 0 to allow, 1 to block, 2 on a usage/input error. No LLM, no network.",
    )
    governor.add_argument("--policy", type=Path, help="policy JSON file (default: a permissive empty policy)")
    governor.add_argument(
        "--call",
        type=Path,
        default=None,
        metavar="PATH",
        help="proposed tool call as JSON: a tool-name string or an object with a 'tool' field "
        "(plus optional input/token/cost hints). Omit, or pass '-', to read it from stdin.",
    )
    governor.add_argument(
        "--plan",
        type=Path,
        default=None,
        metavar="PATH",
        help="dry-run a WHOLE proposed plan (a JSON list of calls) against the policy without "
        "executing anything: the stage-1 feasibility / scoreTrace seam. Exits 0 if every step "
        "clears the gate, 1 if any step is infeasible. Mutually exclusive with --call.",
    )
    governor.add_argument(
        "--partial-trace",
        dest="partial_trace",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON of the calls that already ran: a list of calls or a trace object. "
        "Defaults to the empty start state (nothing has run yet).",
    )
    governor.add_argument("--json", dest="as_json", action="store_true", help="print the decision as JSON to stdout")
    governor.add_argument("-q", "--quiet", action="store_true", help="suppress the human-readable decision line")
    governor.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="control ANSI color in the decision line (default: auto; honors NO_COLOR/FORCE_COLOR)",
    )
    governor.add_argument("--no-color", action="store_true", help="alias for --color never")
    return parser


def run_command(args: argparse.Namespace) -> int:
    traces = load_input_traces(args.input, args.format)
    baseline_format = args.baseline_format or args.format
    baseline_by_case = load_baselines(args.baseline, baseline_format) if args.baseline else {}
    policy = load_policy(args.policy)
    reports = []
    for trace in traces:
        baseline = baseline_by_case.get(trace.case_id)
        findings = evaluate_trace(trace, policy, baseline)
        reports.append(build_case_report(trace, findings, trajectory_diff(trace, baseline, policy)))
    # pass^k reliability is opt-in: it activates when any reliability flag is given. It
    # aggregates the per-run verdicts just built (grouped by case_id) — no trace is
    # re-evaluated. --reliability-sla additionally arms the honest worst-case CI gate.
    passk = None
    if args.passk is not None or args.passk_threshold is not None or args.reliability_sla is not None:
        passk = aggregate_pass_k(
            reports,
            k=args.passk,
            threshold=args.passk_threshold,
            sla=args.reliability_sla,
            confidence=args.reliability_confidence,
        )
    # Cascade telemetry is opt-in and measured deterministically (gate replay vs full audit),
    # so it costs no model spend. Omitting --cascade leaves report.json byte-identical.
    cascade = cascade_to_dict(cascade_telemetry(traces, policy)) if args.cascade else None
    payload = report_to_dict(reports, passk=passk, cascade=cascade)
    write_json(args.out / "report.json", payload)
    write_html_report(args.out / "report.html", reports, passk=passk)
    if args.junit is not None:
        write_junit_report(_output_path(args.junit, args.out / "report.junit.xml"), reports, passk=passk)
    if args.sarif is not None:
        write_sarif_report(
            _output_path(args.sarif, args.out / "report.sarif.json"),
            reports,
            policy_path=args.policy,
            input_path=args.input,
            passk=passk,
        )
    if args.md is not None:
        write_markdown_report(_output_path(args.md, args.out / "report.md"), reports, passk=passk)
    _maybe_write_step_summary(reports, passk)
    summary = summarize(reports)
    _emit_summary(args, summary, passk)
    if cascade is not None and not args.quiet and not getattr(args, "as_json", False):
        print(f"Plimsoll cascade: {cascade['measured_sentence']}", file=sys.stderr)
    if args.exit_zero:
        return EXIT_OK
    # Either reliability gate (the combinatorial floor or the CI band SLA), when armed, fails
    # CI on its own, independent of per-run findings.
    if summary["failed"] or (passk is not None and (passk.gate_failed or passk.sla_gate_failed)):
        return EXIT_FINDINGS
    return EXIT_OK


def init_policy_command(args: argparse.Namespace) -> int:
    traces = load_input_traces(args.input, args.format)
    write_json(args.out, infer_policy(traces))
    print(f"Plimsoll: wrote inferred policy to {args.out}", file=sys.stderr)
    return EXIT_OK


def governor_command(args: argparse.Namespace) -> int:
    """Gate one proposed tool call — or dry-run a whole plan — against a policy before it runs.

    Deterministic and offline. With ``--plan`` it dry-runs an entire proposed plan (the
    stage-1 feasibility / scoreTrace seam) and exits 0 (feasible) or 1 (infeasible). Otherwise
    it gates a single proposed call, exiting 0 (allow) or 1 (block).
    """
    policy = load_policy(args.policy)
    if args.plan is not None:
        if args.call is not None:
            raise ValidationError("--plan and --call are mutually exclusive")
        plan = load_json(args.plan)
        if not isinstance(plan, list):
            raise ValidationError("--plan must be a JSON list of proposed calls")
        feasibility = Governor(policy).dry_run_plan(plan)
        _emit_plan(args, feasibility)
        return EXIT_OK if feasibility.feasible else EXIT_FINDINGS
    proposed_call = _read_json_input(args.call, "proposed tool call")
    partial_payload = load_json(args.partial_trace) if args.partial_trace is not None else None
    partial = coerce_partial_trace(partial_payload)
    decision = Governor(policy).evaluate(partial, proposed_call)
    _emit_decision(args, decision)
    return EXIT_OK if decision.allowed else EXIT_FINDINGS


def _read_json_input(path: Path | None, label: str) -> object:
    """Read a JSON document from ``path``, or from stdin when ``path`` is None or '-'."""
    if path is None or str(path) == "-":
        raw = sys.stdin.read()
        if not raw.strip():
            raise ValidationError(f"no {label} provided: pass JSON on stdin or via --call PATH")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"<stdin>: invalid JSON for {label} at line {exc.lineno}: {exc.msg}") from exc
    return load_json(path)


def _emit_decision(args: argparse.Namespace, decision: Decision) -> None:
    # Machine-readable JSON goes to stdout (pipes cleanly); the human line goes to stderr.
    if getattr(args, "as_json", False):
        print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
        return
    if args.quiet:
        return
    verb = "allow" if decision.allowed else "block"
    head = f"Plimsoll governor: {verb} '{decision.proposed_tool}'"
    if decision.allowed:
        head += " (no governor rule blocked it)"
    if _use_color(args):
        color = "\033[32m" if decision.allowed else "\033[31m"
        head = f"{color}{head}\033[0m"
    print(head, file=sys.stderr)
    for finding in decision.blocking_findings:
        print(f"  - {finding.rule_id} [{finding.severity}]: {finding.message}", file=sys.stderr)


def _emit_plan(args: argparse.Namespace, feasibility: PlanFeasibility) -> None:
    """Print the whole-plan dry-run result (JSON to stdout, human line to stderr)."""
    if getattr(args, "as_json", False):
        print(json.dumps(feasibility.to_dict(), indent=2, sort_keys=True))
        return
    if args.quiet:
        return
    head = f"Plimsoll governor (dry-run): {feasibility.summary}"
    if _use_color(args):
        color = "\033[32m" if feasibility.feasible else "\033[31m"
        head = f"{color}{head}\033[0m"
    print(head, file=sys.stderr)
    for finding in feasibility.blocking_findings:
        print(f"  - {finding.rule_id} [{finding.severity}]: {finding.message}", file=sys.stderr)


def load_baselines(path: Path, trace_format: str) -> dict[str, TraceRun]:
    return {trace.case_id: trace for trace in load_input_traces(path, trace_format)}


def load_input_traces(path: Path, trace_format: str) -> list[TraceRun]:
    if trace_format == "otel":
        return load_otel_traces(path)
    if trace_format in {"langgraph", "openai-agents", "openinference"}:
        return load_adapter_traces(path, trace_format)
    return load_traces(path)


def _maybe_write_step_summary(reports: list, passk=None) -> None:
    """Inside GitHub Actions, append a Markdown summary to the job's run summary (zero config)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    markdown = render_markdown(reports, passk)
    if len(markdown.encode("utf-8")) > 1_000_000:
        markdown = markdown[:200_000] + "\n\n_…truncated; see the full report artifact._\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError:
        pass


def _emit_summary(args: argparse.Namespace, summary: dict[str, object], passk=None) -> None:
    # Machine-readable output goes to stdout so it pipes cleanly; the human banner is
    # diagnostic and goes to stderr (clig.dev). Report files are written under --out.
    if getattr(args, "as_json", False):
        machine: dict[str, object] = {"summary": summary}
        if passk is not None:
            machine["reliability"] = passk.to_dict()
        print(json.dumps(machine, indent=2, sort_keys=True))
        return
    if args.quiet:
        return
    line = (
        f"Plimsoll: {summary['passed']}/{summary['cases']} passed, "
        f"avg score {summary['average_score']}, "
        f"findings: {_format_severity_counts(summary['severity_counts'])}"
    )
    if _use_color(args):
        color = "\033[32m" if not summary["failed"] else "\033[31m"
        line = f"{color}{line}\033[0m"
    print(line, file=sys.stderr)
    if passk is not None:
        passk_line = f"Plimsoll reliability: {passk.headline}"
        if _use_color(args):
            color = "\033[31m" if passk.gate_failed else "\033[32m"
            passk_line = f"{color}{passk_line}\033[0m"
        print(passk_line, file=sys.stderr)


# Fixed severity order for the human summary, worst first. Severities outside this
# vocabulary (there are none today) would sort alphabetically after it.
_SEVERITY_ORDER = ("critical", "high", "medium", "low")


def _format_severity_counts(counts: dict[str, int]) -> str:
    """Format severity counts for the human summary: '3 critical, 3 high' / 'none'."""
    if not counts:
        return "none"
    ordered = [s for s in _SEVERITY_ORDER if s in counts]
    ordered += sorted(s for s in counts if s not in _SEVERITY_ORDER)
    return ", ".join(f"{counts[severity]} {severity}" for severity in ordered)


def _use_color(args: argparse.Namespace) -> bool:
    mode = "never" if getattr(args, "no_color", False) else getattr(args, "color", "auto")
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    if mode == "always" or os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stderr.isatty()


def _output_path(value: object, default: Path) -> Path:
    return default if value is True else Path(str(value))


def supported_formats() -> list[str]:
    return ["native", "otel", "openinference", "langgraph", "openai-agents"]


if __name__ == "__main__":
    raise SystemExit(main())
