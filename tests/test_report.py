"""Rendering tests for the HTML report's trajectory drawer and ordering chain.

These lock the honest behaviour of the must_precede explanation: each violation gets
its own drawer keyed to its own evidence, the "requires" chain is built from real
transitive predecessors (independent rules are never spliced together), and bypass
counts are computed, not fabricated.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.models import CaseReport, Finding
from plimsoll.report import _required_chain, write_html_report


class RequiredChainTests(unittest.TestCase):
    def test_transitive_chain_lists_all_predecessors_in_order(self) -> None:
        pairs = [("manager_review", "security_review"), ("security_review", "grant_access")]
        self.assertEqual(
            _required_chain("grant_access", pairs),
            ["manager_review", "security_review", "grant_access"],
        )

    def test_independent_rules_are_not_spliced(self) -> None:
        pairs = [("authorize", "charge"), ("pack", "ship")]
        self.assertEqual(_required_chain("charge", pairs), ["authorize", "charge"])
        self.assertEqual(_required_chain("ship", pairs), ["pack", "ship"])


class TrajectoryDrawerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-report-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render_two_independent_violations(self) -> str:
        # Candidate ran charge and ship but dropped both their required predecessors,
        # violating two *independent* must_precede rules at once.
        findings = [
            Finding(
                "tool_order",
                "critical",
                "case-x",
                "'charge' occurred before the required 'authorize'.",
                {"before": "authorize", "after": "charge", "fired_at": 1, "sequence": ["charge", "ship"]},
            ),
            Finding(
                "tool_order",
                "critical",
                "case-x",
                "'ship' occurred before the required 'pack'.",
                {"before": "pack", "after": "ship", "fired_at": 2, "sequence": ["charge", "ship"]},
            ),
        ]
        diff = {
            "tool_sequence": {
                "baseline": ["authorize", "charge", "pack", "ship"],
                "current": ["charge", "ship"],
                "changes": [],
            },
            "steps": [
                {"tool": "authorize", "op": "delete", "delta_ms": -100, "delta_tokens": -50},
                {"tool": "charge", "op": "equal", "delta_ms": 0, "delta_tokens": 0},
                {"tool": "pack", "op": "delete", "delta_ms": -100, "delta_tokens": -50},
                {"tool": "ship", "op": "equal", "delta_ms": 0, "delta_tokens": 0},
            ],
            "must_precede": [["authorize", "charge"], ["pack", "ship"]],
            "metrics_delta": {"steps": -2, "duration_ms": -200, "tokens": -100, "estimated_cost_usd": 0.0},
            "final_output": {"baseline": "ok", "current": "ok", "changed": False},
        }
        report = CaseReport(
            case_id="case-x",
            run_id="run-x",
            score=10,
            passed=False,
            metrics={
                "steps": 2,
                "tool_steps": 2,
                "duration_ms": 300,
                "tokens": 150,
                "estimated_cost_usd": 0.0,
                "tool_sequence": ["charge", "ship"],
            },
            findings=findings,
            trajectory_diff=diff,
        )
        out = self.tmp / "report.html"
        write_html_report(out, [report])
        return out.read_text(encoding="utf-8")

    def test_each_violation_gets_its_own_drawer(self) -> None:
        html = self._render_two_independent_violations()
        # Exactly one drawer per violation — no duplicates from delete+insert decomposition.
        self.assertEqual(html.count('class="drawer"'), 2)

    def test_drawers_are_attributed_to_their_own_finding(self) -> None:
        html = self._render_two_independent_violations()
        self.assertIn("with no preceding authorize", html)
        self.assertIn("with no preceding pack", html)
        self.assertIn("step <b>1</b>", html)
        self.assertIn("step <b>2</b>", html)

    def test_independent_chains_are_not_spliced(self) -> None:
        html = self._render_two_independent_violations()
        self.assertIn("authorize &rarr; charge", html)
        self.assertIn("pack &rarr; ship", html)
        # The two rules must never be merged into one invented chain.
        self.assertNotIn("authorize &rarr; pack", html)
        self.assertNotIn("charge &rarr; pack", html)

    def test_bypass_counts_are_honest(self) -> None:
        html = self._render_two_independent_violations()
        # Each rule dropped exactly one predecessor, so each drawer reports one bypass.
        self.assertEqual(html.count("(1 bypassed)"), 2)
        self.assertNotIn("(0 bypassed)", html)
        self.assertIn("required approval (1 bypassed)", html)
        # Both dropped predecessors are tagged as dropped required steps.
        self.assertEqual(html.count("required step dropped"), 2)


if __name__ == "__main__":
    unittest.main()
