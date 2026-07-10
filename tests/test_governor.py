import unittest

from plimsoll.governor import Decision, Governor, ProposedToolCall, coerce_partial_trace
from plimsoll.governor_mcp import GovernorTools, make_handlers
from plimsoll.models import Policy, Span, TraceRun


def _trace_with_tools(tools: list[str]) -> TraceRun:
    spans = [
        Span(span_id=f"s{i}", name=tool, kind="tool", status="ok", start_ms=i * 10, end_ms=i * 10 + 5, tool_name=tool)
        for i, tool in enumerate(tools)
    ]
    return TraceRun(run_id="r", case_id="c", final_output="", expected_output=None, spans=spans)


class GovernorAllowTests(unittest.TestCase):
    def test_clean_proposal_is_allowed(self) -> None:
        governor = Governor(Policy(allowed_tools={"search", "summarize"}))
        decision = governor.evaluate(["search"], "summarize")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.blocking_findings, [])
        self.assertEqual(decision.to_dict()["decision"], "allow")

    def test_allows_convenience_matches_evaluate(self) -> None:
        governor = Governor(Policy(allowed_tools={"search"}))
        self.assertTrue(governor.allows([], "search"))
        self.assertFalse(governor.allows([], "delete"))

    def test_empty_partial_trace_is_the_start_state(self) -> None:
        governor = Governor(Policy())
        # Nothing has run yet and no rule applies: the first call is allowed.
        self.assertTrue(governor.allows([], "search"))


class GovernorToolPolicyTests(unittest.TestCase):
    def test_forbidden_tool_is_blocked(self) -> None:
        governor = Governor(Policy(forbidden_tools={"delete_database"}))
        decision = governor.evaluate(["search"], "delete_database")
        self.assertFalse(decision.allowed)
        self.assertIn("forbidden_tool", decision.rule_ids)
        finding = next(f for f in decision.blocking_findings if f.rule_id == "forbidden_tool")
        self.assertEqual(finding.severity, "critical")
        self.assertEqual(finding.evidence["forbidden_tools"], ["delete_database"])

    def test_tool_outside_allowlist_is_blocked(self) -> None:
        governor = Governor(Policy(allowed_tools={"search", "read"}))
        decision = governor.evaluate(["search"], "wire_transfer")
        self.assertFalse(decision.allowed)
        self.assertIn("tool_allowlist", decision.rule_ids)

    def test_historical_forbidden_call_does_not_block_a_clean_proposal(self) -> None:
        # A forbidden tool already ran in the partial trace; a *different* allowed call
        # is still safe to make now — the gate attributes to the proposed call only.
        governor = Governor(Policy(allowed_tools={"search"}, forbidden_tools={"delete_database"}))
        decision = governor.evaluate(["delete_database"], "search")
        self.assertTrue(decision.allowed)


class GovernorToolOrderTests(unittest.TestCase):
    POLICY = Policy(must_precede=[("manager_review", "grant_access")])

    def test_out_of_order_call_is_blocked(self) -> None:
        governor = Governor(self.POLICY)
        decision = governor.evaluate([], "grant_access")
        self.assertFalse(decision.allowed)
        self.assertIn("tool_order", decision.rule_ids)
        finding = next(f for f in decision.blocking_findings if f.rule_id == "tool_order")
        self.assertEqual(finding.severity, "critical")
        self.assertEqual(finding.evidence["before"], "manager_review")
        self.assertEqual(finding.evidence["after"], "grant_access")

    def test_call_is_allowed_once_prerequisite_has_run(self) -> None:
        governor = Governor(self.POLICY)
        self.assertTrue(governor.allows(["manager_review"], "grant_access"))

    def test_chained_prerequisite_is_enforced(self) -> None:
        chain = Policy(must_precede=[("manager_review", "security_review"), ("security_review", "grant_access")])
        governor = Governor(chain)
        # manager_review ran but the security_review step is still missing.
        self.assertFalse(governor.allows(["manager_review"], "grant_access"))
        self.assertTrue(governor.allows(["manager_review", "security_review"], "grant_access"))


class GovernorBudgetTests(unittest.TestCase):
    def test_token_budget_overshoot_is_blocked(self) -> None:
        governor = Governor(Policy(max_tokens=100))
        partial = Governor.build_partial_trace([{"tool": "search", "input_tokens": 60}])
        blocked = governor.evaluate(partial, {"tool": "summarize", "input_tokens": 50})
        self.assertFalse(blocked.allowed)
        self.assertIn("max_tokens", blocked.rule_ids)
        # Within budget, the same shape is allowed.
        allowed = governor.evaluate(partial, {"tool": "summarize", "input_tokens": 30})
        self.assertTrue(allowed.allowed)

    def test_step_budget_caps_cumulative_calls(self) -> None:
        governor = Governor(Policy(max_steps=2))
        partial = Governor.build_partial_trace(["a", "b"])
        decision = governor.evaluate(partial, "c")
        self.assertFalse(decision.allowed)
        self.assertIn("max_steps", decision.rule_ids)


class GovernorRepeatedActionTests(unittest.TestCase):
    def test_repeated_identical_action_is_blocked(self) -> None:
        governor = Governor(Policy(max_repeated_action_count=1))
        partial = Governor.build_partial_trace([{"tool": "fetch", "input": {"url": "u"}}])
        blocked = governor.evaluate(partial, {"tool": "fetch", "input": {"url": "u"}})
        self.assertFalse(blocked.allowed)
        self.assertIn("repeated_action", blocked.rule_ids)

    def test_different_input_is_not_a_repeat(self) -> None:
        governor = Governor(Policy(max_repeated_action_count=1))
        partial = Governor.build_partial_trace([{"tool": "fetch", "input": {"url": "u"}}])
        self.assertTrue(governor.allows(partial, {"tool": "fetch", "input": {"url": "v"}}))


class GovernorMessagePhrasingTests(unittest.TestCase):
    """Gate messages name the proposed call, not the audit's finished-trace voice."""

    def _only_message(self, policy: Policy, partial: list, proposed: object) -> str:
        decision = Governor(policy).evaluate(partial, proposed)
        self.assertFalse(decision.allowed)
        (finding,) = decision.blocking_findings
        return finding.message

    def test_forbidden_and_allowlist_blocks_name_the_call(self) -> None:
        message = self._only_message(Policy(forbidden_tools={"deploy"}), [], "deploy")
        self.assertEqual(message, "'deploy' is forbidden by policy.")
        message = self._only_message(Policy(allowed_tools={"search"}), [], "wire_transfer")
        self.assertEqual(message, "'wire_transfer' is not in the allowlist.")

    def test_budget_and_repeat_blocks_say_what_the_call_would_do(self) -> None:
        message = self._only_message(
            Policy(max_tokens=100), [{"tool": "search", "input_tokens": 60}], {"tool": "summarize", "input_tokens": 50}
        )
        self.assertEqual(message, "'summarize' would exceed the tokens budget.")
        message = self._only_message(
            Policy(max_repeated_action_count=1),
            [{"tool": "fetch", "input": {"url": "u"}}],
            {"tool": "fetch", "input": {"url": "u"}},
        )
        self.assertEqual(message, "'fetch' would repeat an identical action more than the policy allows.")

    def test_rephrasing_keeps_rule_id_severity_and_evidence(self) -> None:
        decision = Governor(Policy(forbidden_tools={"deploy"})).evaluate([], "deploy")
        (finding,) = decision.blocking_findings
        self.assertEqual(finding.rule_id, "forbidden_tool")
        self.assertEqual(finding.severity, "critical")
        self.assertEqual(finding.evidence["forbidden_tools"], ["deploy"])

    def test_tool_order_message_already_names_the_call(self) -> None:
        policy = Policy(must_precede=[("manager_review", "grant_access")])
        message = self._only_message(policy, ["search"], "grant_access")
        self.assertEqual(message, "'grant_access' occurred before the required 'manager_review'.")


class GovernorCheckTraceTests(unittest.TestCase):
    def test_check_trace_delegates_to_the_full_rule_engine(self) -> None:
        governor = Governor(Policy(forbidden_tools={"delete_database"}))
        trace = _trace_with_tools(["search", "delete_database"])
        findings = governor.check_trace(trace)
        self.assertIn("forbidden_tool", {finding.rule_id for finding in findings})

    def test_check_trace_clean_run_has_no_findings(self) -> None:
        governor = Governor(Policy(allowed_tools={"search", "summarize"}))
        self.assertEqual(governor.check_trace(_trace_with_tools(["search", "summarize"])), [])


class ProposedToolCallTests(unittest.TestCase):
    def test_from_obj_accepts_str_dict_and_self(self) -> None:
        self.assertEqual(ProposedToolCall.from_obj("x").tool, "x")
        self.assertEqual(ProposedToolCall.from_obj({"tool_name": "y"}).tool, "y")
        existing = ProposedToolCall(tool="z")
        self.assertIs(ProposedToolCall.from_obj(existing), existing)

    def test_decision_is_serializable(self) -> None:
        decision = Decision(proposed_tool="t")
        payload = decision.to_dict()
        self.assertEqual(
            payload,
            {
                "decision": "allow",
                "allowed": True,
                "proposed_tool": "t",
                "summary": "allow: no governor rule blocked 't'",
                "blocking_findings": [],
            },
        )


class CoercePartialTraceTests(unittest.TestCase):
    def test_none_and_empty_list_are_the_start_state(self) -> None:
        self.assertEqual(coerce_partial_trace(None).spans, [])
        self.assertEqual(coerce_partial_trace([]).spans, [])

    def test_list_of_calls_becomes_a_trace(self) -> None:
        trace = coerce_partial_trace(["search", {"tool": "summarize"}])
        self.assertEqual(trace.tool_sequence, ["search", "summarize"])

    def test_dict_payload_with_full_spans(self) -> None:
        payload = {
            "run_id": "r",
            "case_id": "c",
            "spans": [
                {
                    "span_id": "s0",
                    "name": "search",
                    "kind": "tool",
                    "status": "ok",
                    "start_ms": 0,
                    "end_ms": 5,
                    "tool_name": "search",
                }
            ],
        }
        self.assertEqual(coerce_partial_trace(payload).tool_sequence, ["search"])


class GovernorMcpSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        policy = Policy(allowed_tools={"search", "grant_access", "manager_review"}, forbidden_tools={"delete_database"})
        self.tools = GovernorTools.from_policy(policy)

    def test_propose_tool_call_allow(self) -> None:
        result = self.tools.propose_tool_call(["search"], "manager_review")
        self.assertEqual(result["decision"], "allow")
        self.assertTrue(result["allowed"])

    def test_propose_tool_call_block_forbidden(self) -> None:
        result = self.tools.propose_tool_call([], {"tool": "delete_database"})
        self.assertEqual(result["decision"], "block")
        rule_ids = {f["rule_id"] for f in result["blocking_findings"]}
        self.assertIn("forbidden_tool", rule_ids)

    def test_check_trace_handler_runs_full_audit(self) -> None:
        payload = {
            "run_id": "r",
            "case_id": "c",
            "final_output": "",
            "spans": [
                {
                    "span_id": "s0",
                    "name": "delete_database",
                    "kind": "tool",
                    "status": "ok",
                    "start_ms": 0,
                    "end_ms": 5,
                    "tool_name": "delete_database",
                }
            ],
        }
        result = self.tools.check_trace(payload)
        self.assertGreaterEqual(result["finding_count"], 1)
        self.assertFalse(result["ok"])

    def test_make_handlers_exposes_both_tools(self) -> None:
        handlers = make_handlers(self.tools.governor)
        self.assertEqual(set(handlers), {"propose_tool_call", "check_trace"})
        self.assertTrue(callable(handlers["propose_tool_call"]))
        self.assertEqual(handlers["propose_tool_call"]([], "search")["decision"], "allow")


if __name__ == "__main__":
    unittest.main()
