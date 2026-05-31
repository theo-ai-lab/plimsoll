from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plimsoll.cli import load_input_traces  # noqa: E402
from plimsoll.io import load_policy  # noqa: E402
from plimsoll.rules import evaluate_trace, trace_metrics  # noqa: E402

# Defaults describe the canonical ticket-triage cross-format fixtures; any source may
# override them (e.g. the real pydantic-ai export, which is a different scenario).
DEFAULT_POLICY = "examples/policies/default_policy.json"
DEFAULT_BASELINE = "examples/traces/baseline_ticket_triage.json"
DEFAULT_EXPECTED_TOOLS = ["read_ticket", "search_docs", "summarize"]
DEFAULT_EXPECTED_TOKENS = 355
TOKEN_FORMATS = {"otel", "openinference", "openai-agents", "langgraph"}


def _one_trace(path: Path, trace_format: str, label: str) -> object:
    traces = load_input_traces(path, trace_format)
    if len(traces) != 1:
        raise ValueError(f"{label}: expected one trace, got {len(traces)}")
    return traces[0]


def main() -> int:
    source_path = ROOT / "examples/public_trace_sources.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))

    for item in payload["sources"]:
        fmt = item["format"]
        fixture = ROOT / item["fixture"]
        if not fixture.exists():
            print(f"FAIL {fmt}: missing fixture {fixture}", file=sys.stderr)
            return 1

        policy = load_policy(ROOT / item.get("policy", DEFAULT_POLICY))
        baseline = _one_trace(
            ROOT / item.get("baseline", DEFAULT_BASELINE), item.get("baseline_format", "native"), f"{fmt} baseline"
        )
        expected_tools = item.get("expected_tools", DEFAULT_EXPECTED_TOOLS)
        expected_tokens = item.get("expected_tokens", DEFAULT_EXPECTED_TOKENS)

        trace = _one_trace(fixture, fmt, fmt)
        if trace.tool_sequence != expected_tools:
            print(f"FAIL {fmt}: tool sequence {trace.tool_sequence} != {expected_tools}", file=sys.stderr)
            return 1
        metrics = trace_metrics(trace)
        if fmt in TOKEN_FORMATS and metrics["tokens"] != expected_tokens:
            print(f"FAIL {fmt}: expected {expected_tokens} tokens, got {metrics['tokens']}", file=sys.stderr)
            return 1
        findings = evaluate_trace(trace, policy, baseline)
        if findings:
            rule_ids = [finding.rule_id for finding in findings]
            print(f"FAIL {fmt}: unexpected findings {rule_ids}", file=sys.stderr)
            return 1
        print(f"OK {fmt} {fixture.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
