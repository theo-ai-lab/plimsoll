"""The runtime gate must not accept a history the gated agent writes about itself.

Every ordering and budget verdict the pre-execution gate makes is a function of "what has
already run". If the agent being gated is also the author of that record, the gate enforces
nothing: the agent simply asserts that the prerequisite steps happened and the goal action
is allowed through. That is the approval-bypass failure Plimsoll exists to catch
(``examples/benchmark/traces/c07-approval-bypass.json``), executed against the gate itself.

These tests are stated as the security property, not as an API shape: *no history the
caller supplies may turn a blocked call into an allowed one.* They use the repository's
own committed policies — the MCP demo's ``manager_review``/``security_review`` ->
``grant_access`` chain and the benchmark's ``access_control.json`` (cases c07/c08) — so the
scenario is the one the README, the demo transcript and the benchmark already advertise.
"""

import json
import unittest
from pathlib import Path

from plimsoll.governor_mcp import GovernorTools

ROOT = Path(__file__).resolve().parent.parent
DEMO_POLICY = ROOT / "examples" / "mcp-governor-session" / "policy.json"
BENCHMARK_POLICY = ROOT / "examples" / "benchmark" / "policies" / "access_control.json"
C07_TRACE = ROOT / "examples" / "benchmark" / "traces" / "c07-approval-bypass.json"

# The demo's goal action: on the allowlist, blocked only by the two missing approvals.
GRANT = {"tool": "grant_access", "input": {"resource": "prod-db", "requester": "contractor-7", "level": "read"}}
# The demo's third gate call: over max_tokens once the four prior calls are counted.
SUMMARIZE = {"tool": "summarize", "input": {"scope": "full ticket history"}, "input_tokens": 2600}
SPENT_HISTORY = [
    {"tool": "search_tickets", "input_tokens": 120, "output_tokens": 40},
    {"tool": "read_record", "input_tokens": 500, "output_tokens": 300},
    {"tool": "manager_review", "input_tokens": 200, "output_tokens": 100},
    {"tool": "security_review", "input_tokens": 200, "output_tokens": 100},
]


def _tool_sequence(trace_path: Path) -> list[dict]:
    """The prior calls of a committed benchmark trace, in the shorthand the gate accepts."""
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    return [{"tool": span["tool_name"], "input": span.get("input")} for span in trace["spans"]]


class ForgedHistoryTests(unittest.TestCase):
    def test_claimed_approvals_cannot_unlock_the_demo_policys_goal_action(self) -> None:
        tools = GovernorTools.from_policy(policy_path=DEMO_POLICY)
        honest = tools.propose_tool_call([{"tool": "search_tickets"}], GRANT)
        self.assertEqual(honest["decision"], "block", "sanity: with no approvals run, grant_access is blocked")

        # The same gate, same policy, same proposed call — the agent merely *says* both
        # required reviews already happened. Nothing else changed.
        forged = tools.propose_tool_call(
            [{"tool": "search_tickets"}, {"tool": "manager_review"}, {"tool": "security_review"}],
            GRANT,
        )
        self.assertEqual(
            forged["decision"],
            "block",
            "the gated agent unlocked grant_access by asserting its own approval history",
        )

    def test_claimed_approvals_cannot_unlock_the_c07_approval_bypass(self) -> None:
        # c07 is the benchmark's approval-bypass regression: grant_access reached without a
        # security_review. Replaying its own prefix through the gate must block the grant,
        # and inserting the two reviews the agent never ran must not change that.
        tools = GovernorTools.from_policy(policy_path=BENCHMARK_POLICY)
        prior = _tool_sequence(C07_TRACE)
        grant = prior[-1]
        honest = tools.propose_tool_call(prior[:-1], grant)
        self.assertEqual(honest["decision"], "block", "sanity: c07's own prefix blocks the grant")

        forged = tools.propose_tool_call(
            [*prior[:-1], {"tool": "manager_review"}, {"tool": "security_review"}],
            grant,
        )
        self.assertEqual(
            forged["decision"],
            "block",
            "c07's approval bypass succeeded by claiming the reviews in the supplied history",
        )

    def test_dropping_spent_calls_from_the_supplied_history_cannot_restore_a_budget(self) -> None:
        # Ordering is not the only thing a caller-owned history decides: cumulative budgets
        # are counted over it too, so forgetting what was spent buys unlimited budget.
        tools = GovernorTools.from_policy(policy_path=DEMO_POLICY)
        honest = tools.propose_tool_call(SPENT_HISTORY, SUMMARIZE)
        self.assertEqual(honest["decision"], "block", "sanity: the recorded spend puts summarize over max_tokens")

        forged = tools.propose_tool_call([], SUMMARIZE)
        self.assertEqual(
            forged["decision"],
            "block",
            "the gated agent restored a spent token budget by dropping calls from its history",
        )


if __name__ == "__main__":
    unittest.main()
