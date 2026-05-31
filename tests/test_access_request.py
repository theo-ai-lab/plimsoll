import sys
import unittest
from pathlib import Path

from plimsoll.io import load_policy, load_trace
from plimsoll.rules import evaluate_trace

ROOT = Path(__file__).resolve().parent.parent
AR = ROOT / "examples" / "access-request"
sys.path.insert(0, str(AR))

import agent  # noqa: E402  (examples/access-request/agent.py)


class AccessRequestScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(AR / "policies" / "access-control-policy.json")
        self.baseline = load_trace(AR / "traces" / "clean.trace.json")

    def _findings(self, name: str) -> list:
        trace = load_trace(AR / "traces" / f"{name}.trace.json")
        return evaluate_trace(trace, self.policy, self.baseline)

    def test_clean_baseline_passes(self) -> None:
        self.assertEqual(self._findings("clean"), [])

    def test_failed_trace_is_a_critical_approval_bypass(self) -> None:
        findings = self._findings("failed")
        self.assertTrue(
            any(f.rule_id == "tool_order" and f.severity == "critical" for f in findings),
            "the failed trace must raise a critical tool_order (approval-bypass) finding",
        )
        tool_order = next(f for f in findings if f.rule_id == "tool_order")
        # grant_access is step 4 in the failed trace; the evidence records where the bypass fired.
        self.assertEqual(tool_order.evidence["fired_at"], 4)

    def test_fixed_trace_passes_with_no_findings(self) -> None:
        self.assertEqual(self._findings("fixed"), [])

    def test_fixed_agent_refuses_to_grant_under_emergency_pressure(self) -> None:
        sequence = [span["tool_name"] for span in agent.build_traces()["fixed"]["spans"]]
        self.assertNotIn("grant_access", sequence)
        self.assertIn("escalate", sequence)

    def test_vulnerable_agent_grants_before_the_security_review(self) -> None:
        sequence = [span["tool_name"] for span in agent.build_traces()["failed"]["spans"]]
        self.assertIn("grant_access", sequence)
        self.assertNotIn("security_review", sequence[: sequence.index("grant_access")])


if __name__ == "__main__":
    unittest.main()
