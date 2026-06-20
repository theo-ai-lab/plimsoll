"""Subset-selection completeness for the Governor's pre-execution gate.

The gate (``Governor.evaluate`` / ``propose_tool_call``) can only enforce the rules that
are *decidable before a tool runs*: tool membership (allowlist / forbidden), required
ordering (``must_precede``), cumulative budgets, and repeated-action limits. The remaining
rules need the call's result or the finished trajectory and are deferred to the post-hoc
``check_trace`` audit.

The danger with any such subset is a *silent pass*: a call that a decidable rule says must
be blocked, but which the gate lets through because the rule was left out of the subset or
mis-attributed. These tests prove that does not happen:

1.  COMPLETE on the decidable subset. Over the exhaustive cross-product of the decidable
    signal families, an *independent oracle* (derived from the rule semantics, not from the
    gate's code) computes exactly which rules must block. The gate's decision must equal the
    oracle for every combination — so it never silently passes a should-block call, and never
    over-blocks within the subset either.

2.  DEFERS, never drops. Every rule outside the decidable subset is still enforced by
    ``check_trace``; the gate allowing such a call is the correct "I cannot decide this yet"
    answer, not a silent pass.

3.  PARTITION. The decidable + deferred sets together cover every rule the engine can emit,
    and are disjoint. A newly added rule that is classified as neither fails this test,
    forcing an explicit decision instead of an accidental silent pass.
"""

from __future__ import annotations

import itertools
import unittest

from plimsoll.governor import Governor, ProposedToolCall
from plimsoll.models import Policy, Span, TraceRun, stable_repr
from plimsoll.passk import PASSK_RULE_ID, PASSK_SLA_RULE_ID
from plimsoll.report import RULE_DESCRIPTIONS

# The gate's decidable subset and the rules deliberately deferred to check_trace. These are
# the *spec* the gate is tested against — kept here (not imported from governor.py) so the
# test is an independent statement of intent.
GATE_RULES = {
    "forbidden_tool",
    "tool_allowlist",
    "tool_order",
    "max_steps",
    "max_duration_ms",
    "max_tokens",
    "max_estimated_cost_usd",
    "repeated_action",
}
DEFERRED_RULES = {
    "expected_output",
    "required_tool",
    "retry_drift",
    "pii_leak",
    "secret_leak",
    "trajectory_drift",
    "trajectory_mismatch",
}


def _signature(call: ProposedToolCall) -> str:
    """Mirror Span.action_signature for a proposed/prior call (tool_name is always set)."""
    return f"{call.tool}:{stable_repr(call.input)}"


def oracle_blocks(policy: Policy, prior: list[ProposedToolCall], proposed: ProposedToolCall) -> set[str]:
    """Independently derive the set of decidable rules that MUST block ``proposed``.

    Written straight from the rule definitions and the canonical metric summation, with no
    reference to Governor internals, so agreement with the gate is meaningful evidence.
    """
    seq = [call.tool for call in prior]
    blocks: set[str] = set()

    if proposed.tool in policy.forbidden_tools:
        blocks.add("forbidden_tool")
    if policy.allowed_tools and proposed.tool not in policy.allowed_tools:
        blocks.add("tool_allowlist")
    for before, after in policy.must_precede:
        if after == proposed.tool and before not in seq:
            blocks.add("tool_order")

    everything = [*prior, proposed]
    steps = len(everything)
    tokens = sum(call.input_tokens + call.output_tokens for call in everything)
    duration = sum(call.duration_ms for call in everything)
    cost = round(sum(call.estimated_cost_usd for call in everything), 6)
    if policy.max_steps is not None and steps > policy.max_steps:
        blocks.add("max_steps")
    if policy.max_tokens is not None and tokens > policy.max_tokens:
        blocks.add("max_tokens")
    if policy.max_duration_ms is not None and duration > policy.max_duration_ms:
        blocks.add("max_duration_ms")
    if policy.max_estimated_cost_usd is not None and cost > policy.max_estimated_cost_usd:
        blocks.add("max_estimated_cost_usd")

    proposed_sig = _signature(proposed)
    repeat_count = sum(1 for call in everything if _signature(call) == proposed_sig)
    if repeat_count > policy.max_repeated_action_count:
        blocks.add("repeated_action")

    return blocks


# The five decidable signal families. Each is toggled independently so the cross-product
# exercises every combination of decidable signals (2**5 = 32 cases).
SIGNALS = ["forbidden", "allowlist", "order", "budget", "repeat"]


def build_case(flags: dict[str, bool]) -> tuple[Policy, list[ProposedToolCall], ProposedToolCall]:
    """Construct a (policy, prior trace, proposed call) realising exactly ``flags``."""
    policy = Policy(
        forbidden_tools={"act"} if flags["forbidden"] else set(),
        allowed_tools={"prelude"} if flags["allowlist"] else set(),  # "act" is deliberately absent
        must_precede=[("gate_before", "act")] if flags["order"] else [],
        max_tokens=10 if flags["budget"] else None,
        max_repeated_action_count=1,
    )
    prior: list[ProposedToolCall] = []
    if flags["repeat"]:
        # An identical prior "act" so the proposal becomes the 2nd occurrence (limit is 1).
        prior.append(ProposedToolCall(tool="act", input={"q": "same"}, input_tokens=0))
    proposed = ProposedToolCall(
        tool="act",
        input={"q": "same"} if flags["repeat"] else {"q": "solo"},
        input_tokens=100 if flags["budget"] else 0,
    )
    return policy, prior, proposed


class GateCompletenessTests(unittest.TestCase):
    def test_gate_matches_oracle_for_every_decidable_signal_combination(self) -> None:
        for combo in itertools.product([False, True], repeat=len(SIGNALS)):
            flags = dict(zip(SIGNALS, combo))
            with self.subTest(**flags):
                policy, prior, proposed = build_case(flags)
                decision = Governor(policy).evaluate(prior, proposed)
                expected = oracle_blocks(policy, prior, proposed)

                # COMPLETE + SOUND on the subset: the gate decides exactly the should-block set.
                self.assertEqual(set(decision.rule_ids), expected)
                # The core anti-silent-pass invariant: if anything must block, the call is blocked.
                self.assertEqual(decision.allowed, not expected)
                # The gate only ever emits rules from its decidable subset.
                self.assertTrue(set(decision.rule_ids) <= GATE_RULES)

    def test_no_decidable_signal_combination_silently_passes(self) -> None:
        """Restate the headline guarantee directly: oracle-should-block ==> gate blocks."""
        leaks = []
        for combo in itertools.product([False, True], repeat=len(SIGNALS)):
            flags = dict(zip(SIGNALS, combo))
            policy, prior, proposed = build_case(flags)
            decision = Governor(policy).evaluate(prior, proposed)
            if oracle_blocks(policy, prior, proposed) and decision.allowed:
                leaks.append(flags)
        self.assertEqual(leaks, [], f"gate silently passed should-block calls: {leaks}")


class PerRuleGateBlocksTests(unittest.TestCase):
    """Each decidable rule, in isolation, must produce a block — so the grid is not vacuous
    and every budget dimension (steps/duration/tokens/cost) is individually exercised."""

    def _assert_blocks(self, policy: Policy, prior: list, proposed, rule_id: str) -> None:
        decision = Governor(policy).evaluate(prior, proposed)
        self.assertFalse(decision.allowed)
        self.assertIn(rule_id, decision.rule_ids)
        # The gate stays within its subset even on a real block.
        self.assertTrue(set(decision.rule_ids) <= GATE_RULES)

    def test_forbidden_tool(self) -> None:
        self._assert_blocks(Policy(forbidden_tools={"rm"}), [], "rm", "forbidden_tool")

    def test_tool_allowlist(self) -> None:
        self._assert_blocks(Policy(allowed_tools={"safe"}), [], "danger", "tool_allowlist")

    def test_tool_order(self) -> None:
        self._assert_blocks(Policy(must_precede=[("review", "grant")]), [], "grant", "tool_order")

    def test_max_steps(self) -> None:
        self._assert_blocks(Policy(max_steps=1), ["a"], "b", "max_steps")

    def test_max_tokens(self) -> None:
        self._assert_blocks(Policy(max_tokens=50), [], {"tool": "t", "input_tokens": 100}, "max_tokens")

    def test_max_duration_ms(self) -> None:
        self._assert_blocks(Policy(max_duration_ms=50), [], {"tool": "t", "duration_ms": 100}, "max_duration_ms")

    def test_max_estimated_cost_usd(self) -> None:
        self._assert_blocks(
            Policy(max_estimated_cost_usd=0.01), [], {"tool": "t", "estimated_cost_usd": 0.05}, "max_estimated_cost_usd"
        )

    def test_repeated_action(self) -> None:
        policy = Policy(max_repeated_action_count=1)
        prior = [{"tool": "fetch", "input": {"u": "x"}}]
        self._assert_blocks(policy, prior, {"tool": "fetch", "input": {"u": "x"}}, "repeated_action")


def _span(index: int, tool: str, *, status: str = "ok", inp=None, out=None, error=None, attrs=None) -> Span:
    return Span(
        span_id=f"s{index}",
        name=tool,
        kind="tool",
        status=status,
        start_ms=index * 10,
        end_ms=index * 10 + 5,
        tool_name=tool,
        input=inp,
        output=out,
        error=error,
        attributes=attrs or {},
    )


def _trace(spans: list[Span], *, final_output: str = "", expected: str | None = None) -> TraceRun:
    return TraceRun(run_id="r", case_id="c", final_output=final_output, expected_output=expected, spans=spans)


class DeferredRulesAreEnforcedNotDroppedTests(unittest.TestCase):
    """Every rule outside the decidable subset is still caught post-hoc by check_trace.

    The gate allowing such a call is correct (it cannot decide pre-execution); these tests
    prove the rule is *deferred*, not silently abandoned — check_trace enforces it, and the
    rule is never one the gate claims to decide.
    """

    def test_expected_output_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy())
        # Pre-execution: the gate cannot see the not-yet-produced output, so it allows.
        self.assertNotIn("expected_output", governor.evaluate([], "produce").rule_ids)
        # Post-hoc: a completed run whose output is wrong is caught.
        trace = _trace([_span(0, "produce")], final_output="WRONG", expected="RIGHT")
        self.assertIn("expected_output", {f.rule_id for f in governor.check_trace(trace)})

    def test_required_tool_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy(required_tools={"finalize"}))
        # A required tool is legitimately absent mid-run, so proposing something else is allowed.
        self.assertTrue(governor.allows([], "act"))
        self.assertNotIn("required_tool", governor.evaluate([], "act").rule_ids)
        # A finished run that never called finalize is caught.
        trace = _trace([_span(0, "act")])
        self.assertIn("required_tool", {f.rule_id for f in governor.check_trace(trace)})

    def test_secret_leak_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy())
        # The gate cannot inspect a result that has not happened: a benign proposal is allowed.
        decision = governor.evaluate([], {"tool": "summarize", "input": {"q": "hello"}})
        self.assertTrue(decision.allowed)
        self.assertNotIn("secret_leak", decision.rule_ids)
        # The completed trace leaking a secret in its output is caught.
        trace = _trace([_span(0, "summarize")], final_output="token sk-abcdef0123456789ABCDEF leaked")
        self.assertIn("secret_leak", {f.rule_id for f in governor.check_trace(trace)})

    def test_pii_leak_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy())
        trace = _trace([_span(0, "lookup")], final_output="SSN 123-45-6789")
        self.assertIn("pii_leak", {f.rule_id for f in governor.check_trace(trace)})

    def test_retry_drift_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy())
        # Proposing the retry call itself triggers no gate rule (drift is a cross-span property).
        prior = [{"tool": "fetch", "status": "error", "input": {"u": "a"}}]
        decision = governor.evaluate(prior, {"tool": "fetch", "input": {"u": "b"}})
        self.assertNotIn("retry_drift", decision.rule_ids)
        trace = _trace(
            [
                _span(0, "fetch", status="error", inp={"u": "a"}),
                _span(1, "fetch", status="ok", inp={"u": "b"}),
            ]
        )
        self.assertIn("retry_drift", {f.rule_id for f in governor.check_trace(trace)})

    def test_trajectory_drift_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy(max_tool_sequence_distance=0))
        baseline = _trace([_span(0, "a"), _span(1, "b")])
        trace = _trace([_span(0, "a"), _span(1, "c")])
        self.assertIn("trajectory_drift", {f.rule_id for f in governor.check_trace(trace, baseline)})

    def test_trajectory_mismatch_is_deferred_then_enforced(self) -> None:
        governor = Governor(Policy(trajectory_match_mode="strict"))
        baseline = _trace([_span(0, "a"), _span(1, "b")])
        trace = _trace([_span(0, "b"), _span(1, "a")])
        self.assertIn("trajectory_mismatch", {f.rule_id for f in governor.check_trace(trace, baseline)})


class RuleClassificationPartitionTests(unittest.TestCase):
    def test_every_engine_rule_is_classified_gate_or_deferred(self) -> None:
        # The canonical rule catalog, minus the cross-run reliability gates (pass^k point and
        # the SLA confidence band) — those are aggregate metrics over repeated runs, not
        # per-trace rules the engine emits, so the gate/deferred partition does not apply.
        catalog = set(RULE_DESCRIPTIONS) - {PASSK_RULE_ID, PASSK_SLA_RULE_ID}
        self.assertEqual(
            GATE_RULES | DEFERRED_RULES,
            catalog,
            "a rule is classified as neither gate-decidable nor deferred — classify it explicitly",
        )

    def test_gate_and_deferred_sets_are_disjoint(self) -> None:
        self.assertEqual(GATE_RULES & DEFERRED_RULES, set())


if __name__ == "__main__":
    unittest.main()
