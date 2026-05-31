"""Validation against a REAL pydantic-ai OpenTelemetry export.

The fixtures under examples/access-request/traces/real/ are emitted by the actual
pydantic-ai library's GenAI OpenTelemetry instrumentation (see build_real_otel_trace.py),
not hand-authored. These tests prove Plimsoll's generic `otel` adapter ingests that
real export with no framework-specific code, and that the must_precede gate catches a
real ordering bypass. They run against the committed JSON only — no SDK import needed.
"""

import json
import unittest
from pathlib import Path

from plimsoll.io import load_policy
from plimsoll.otel import load_otel_trace
from plimsoll.rules import evaluate_trace, trace_metrics

ROOT = Path(__file__).resolve().parent.parent
AR = ROOT / "examples" / "access-request"
REAL = AR / "traces" / "real"


def _agent_span_tokens(fixture: Path) -> int:
    """Aggregate token usage reported on the OTel `invoke_agent` span (the agent-level
    aggregate of all child operations)."""
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    for resource in raw["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                attrs = {a["key"]: a["value"] for a in span["attributes"]}
                op = attrs.get("gen_ai.operation.name", {}).get("stringValue")
                if op == "invoke_agent":
                    return int(attrs["gen_ai.usage.input_tokens"]["intValue"]) + int(
                        attrs["gen_ai.usage.output_tokens"]["intValue"]
                    )
    raise AssertionError("no invoke_agent span found")


class RealTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(AR / "policies" / "access-control-policy.json")
        self.clean = load_otel_trace(REAL / "clean.otel.json")
        self.bypass = load_otel_trace(REAL / "bypass.otel.json")

    def test_real_export_yields_a_clean_tool_sequence(self) -> None:
        # The adapter must surface only the real tool calls — not the per-turn `chat`
        # spans or the `invoke_agent` span that the framework also emits.
        self.assertEqual(self.clean.tool_sequence, ["manager_review", "security_review", "grant_access"])
        self.assertNotIn("chat", self.clean.tool_sequence)
        self.assertNotIn("invoke_agent", self.clean.tool_sequence)

    def test_clean_real_trace_passes_the_policy(self) -> None:
        self.assertEqual(evaluate_trace(self.clean, self.policy, self.clean), [])

    def test_bypass_real_trace_is_caught_as_a_critical_ordering_violation(self) -> None:
        findings = evaluate_trace(self.bypass, self.policy, self.clean)
        order = [f for f in findings if f.rule_id == "tool_order"]
        self.assertTrue(order, "the bypass run must raise a tool_order finding")
        self.assertEqual(order[0].severity, "critical")
        self.assertEqual(order[0].evidence["before"], "security_review")
        self.assertEqual(order[0].evidence["after"], "grant_access")
        self.assertEqual(self.bypass.tool_sequence, ["manager_review", "grant_access"])

    def test_agent_span_usage_is_not_double_counted(self) -> None:
        # trace_metrics excludes the aggregate `invoke_agent` span, so the total must
        # equal that aggregate (which is the sum of the child operations) — not twice it.
        aggregate = _agent_span_tokens(REAL / "clean.otel.json")
        self.assertEqual(trace_metrics(self.clean)["tokens"], aggregate)


if __name__ == "__main__":
    unittest.main()
