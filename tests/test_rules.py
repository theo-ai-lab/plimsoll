import unittest
from pathlib import Path

from plimsoll.io import load_policy, load_trace
from plimsoll.models import Policy, Span, TraceRun
from plimsoll.rules import check_tool_order, check_trajectory_match, edit_distance, evaluate_trace


def _trace_with_tools(tools: list[str]) -> TraceRun:
    spans = [
        Span(span_id=f"s{i}", name=tool, kind="tool", status="ok", start_ms=i * 10, end_ms=i * 10 + 5, tool_name=tool)
        for i, tool in enumerate(tools)
    ]
    return TraceRun(run_id="r", case_id="c", final_output="", expected_output=None, spans=spans)


FIXTURES = Path("examples")


class RuleTests(unittest.TestCase):
    def test_clean_trace_has_no_findings(self) -> None:
        policy = load_policy(FIXTURES / "policies" / "default_policy.json")
        baseline = load_trace(FIXTURES / "traces" / "baseline_ticket_triage.json")
        current = load_trace(FIXTURES / "traces" / "current_ticket_triage.json")

        findings = evaluate_trace(current, policy, baseline)

        self.assertEqual(findings, [])

    def test_regressed_trace_finds_actionable_issues(self) -> None:
        policy = load_policy(FIXTURES / "policies" / "default_policy.json")
        baseline = load_trace(FIXTURES / "traces" / "baseline_ticket_triage.json")
        regressed = load_trace(FIXTURES / "traces" / "regressed_ticket_triage.json")

        findings = evaluate_trace(regressed, policy, baseline)
        rule_ids = {finding.rule_id for finding in findings}

        self.assertIn("expected_output", rule_ids)
        self.assertIn("forbidden_tool", rule_ids)
        self.assertIn("tool_allowlist", rule_ids)
        self.assertIn("retry_drift", rule_ids)
        self.assertIn("pii_leak", rule_ids)
        self.assertIn("secret_leak", rule_ids)
        self.assertIn("max_tokens", rule_ids)
        self.assertIn("max_estimated_cost_usd", rule_ids)
        self.assertIn("trajectory_drift", rule_ids)

    def test_edit_distance(self) -> None:
        self.assertEqual(edit_distance(["a", "b", "c"], ["a", "x", "c"]), 1)
        self.assertEqual(edit_distance(["a", "b"], ["a", "b", "c"]), 1)


class TrajectoryMatchTests(unittest.TestCase):
    def _ids(self, mode: str, reference: list[str], actual: list[str]) -> set[str]:
        findings = check_trajectory_match(
            _trace_with_tools(actual), Policy(trajectory_match_mode=mode), _trace_with_tools(reference)
        )
        return {finding.rule_id for finding in findings}

    def test_strict_mode_requires_same_order(self) -> None:
        self.assertEqual(self._ids("strict", ["a", "b", "c"], ["a", "b", "c"]), set())
        self.assertIn("trajectory_mismatch", self._ids("strict", ["a", "b", "c"], ["a", "c", "b"]))

    def test_unordered_mode_ignores_order(self) -> None:
        self.assertEqual(self._ids("unordered", ["a", "b", "c"], ["c", "b", "a"]), set())
        self.assertIn("trajectory_mismatch", self._ids("unordered", ["a", "b", "c"], ["a", "b"]))

    def test_superset_mode_catches_dropped_steps(self) -> None:
        self.assertEqual(self._ids("superset", ["a", "b"], ["a", "b", "c"]), set())
        self.assertIn("trajectory_mismatch", self._ids("superset", ["a", "b"], ["a"]))

    def test_subset_mode_catches_new_steps(self) -> None:
        self.assertEqual(self._ids("subset", ["a", "b", "c"], ["a", "b"]), set())
        self.assertIn("trajectory_mismatch", self._ids("subset", ["a", "b", "c"], ["a", "b", "d"]))

    def test_match_is_noop_without_mode_or_baseline(self) -> None:
        self.assertEqual(check_trajectory_match(_trace_with_tools(["a"]), Policy(), _trace_with_tools(["b"])), [])
        self.assertEqual(
            check_trajectory_match(_trace_with_tools(["a"]), Policy(trajectory_match_mode="strict"), None), []
        )


class ToolOrderTests(unittest.TestCase):
    def _ids(self, must_precede: list[tuple[str, str]], tools: list[str]) -> set[str]:
        policy = Policy(must_precede=must_precede)
        return {finding.rule_id for finding in check_tool_order(_trace_with_tools(tools), policy)}

    def test_ordered_sequence_passes(self) -> None:
        self.assertEqual(self._ids([("manager_review", "grant_access")], ["manager_review", "grant_access"]), set())

    def test_after_without_any_before_fails(self) -> None:
        self.assertIn("tool_order", self._ids([("manager_review", "grant_access")], ["grant_access"]))

    def test_after_before_the_before_fails(self) -> None:
        self.assertIn("tool_order", self._ids([("manager_review", "grant_access")], ["grant_access", "manager_review"]))

    def test_absent_after_is_valid_refusal_path(self) -> None:
        # The agent escalates/refuses and never grants — order is satisfied vacuously.
        self.assertEqual(self._ids([("manager_review", "grant_access")], ["manager_review"]), set())

    def test_chain_is_enforced(self) -> None:
        chain = [("manager_review", "security_review"), ("security_review", "grant_access")]
        self.assertEqual(self._ids(chain, ["manager_review", "security_review", "grant_access"]), set())
        # grant_access with the security_review step skipped must fail.
        self.assertIn("tool_order", self._ids(chain, ["manager_review", "grant_access"]))


if __name__ == "__main__":
    unittest.main()
