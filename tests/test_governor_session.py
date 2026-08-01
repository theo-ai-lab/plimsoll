"""The runtime gate's session: who owns the history, and what a decision binds to.

Three properties, all executed:

1.  PERMISSION MATRIX. A table of (calls the governor authorized, proposed call) against
    the committed access-control policy, each row asserting the exact decision and the
    exact rules that produced it. Allow rows are as load-bearing as block rows: a gate that
    blocks everything enforces nothing anyone would keep, so the correct refusal path
    (benchmark case c08) is in the table too and must run end to end.

2.  THE GOVERNOR OWNS THE RECORD. The session appends only calls it allowed, a supplied
    history is never used as the record, and any disagreement with it fails closed. A
    proposal with no live session is refused rather than judged against an empty past.

3.  DECISIONS BIND TO A POLICY. Every decision carries the SHA-256 of the effective policy
    text, so a recorded verdict cannot be replayed as if it came from a different policy.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from plimsoll.governor import (
    SESSION_HISTORY_MISMATCH_RULE,
    SESSION_RULES,
    SESSION_UNKNOWN_RULE,
    Governor,
)
from plimsoll.governor_mcp import GovernorTools
from plimsoll.io import load_policy
from plimsoll.models import Policy
from plimsoll.policy import canonical_policy_text, policy_digest
from plimsoll.report import RULE_DESCRIPTIONS

ROOT = Path(__file__).resolve().parent.parent
ACCESS_POLICY_PATH = ROOT / "examples" / "benchmark" / "policies" / "access_control.json"
C08_TRACE = ROOT / "examples" / "benchmark" / "traces" / "c08-refusal-valid.json"

MANAGER = {"tool": "manager_review", "input": {"ticket": "REQ-1"}}
SECURITY = {"tool": "security_review", "input": {"ticket": "REQ-1"}}
GRANT = {"tool": "grant_access", "input": {"resource": "prod-db-admin"}}
LOOKUP = {"tool": "policy_lookup", "input": {"n": 1}}


def _c08_calls() -> list[dict[str, Any]]:
    """The committed refusal-path trace (c08) as proposable calls, in order."""
    trace = json.loads(C08_TRACE.read_text(encoding="utf-8"))
    return [{"tool": span["tool_name"], "input": span.get("input")} for span in trace["spans"]]


C08 = _c08_calls()
# 12 distinct lookups: max_steps is 12 in the committed policy, so a 13th call overruns it.
TWELVE_LOOKUPS = [{"tool": "policy_lookup", "input": {"n": index}} for index in range(12)]

# (name, calls the governor must authorize first, proposed call, decision, blocking rules)
PERMISSION_MATRIX: list[tuple[str, list[dict[str, Any]], dict[str, Any], str, list[str]]] = [
    ("first call of the episode", [], {"tool": "intake_request", "input": None}, "allow", []),
    ("goal action with nothing approved", [], GRANT, "block", ["tool_order"]),
    ("approval taken out of order", [], SECURITY, "block", ["tool_order"]),
    ("approval in order", [MANAGER], SECURITY, "allow", []),
    ("goal action with one approval short", [MANAGER], GRANT, "block", ["tool_order"]),
    ("goal action fully approved", [MANAGER, SECURITY], GRANT, "allow", []),
    ("tool that is not on the allowlist", [], {"tool": "exfiltrate_records"}, "block", ["tool_allowlist"]),
    ("the refusal path c08 completes", C08[:-1], C08[-1], "allow", []),
    ("one call past the step budget", TWELVE_LOOKUPS, {"tool": "escalate"}, "block", ["max_steps"]),
    ("the same call twice", [LOOKUP], LOOKUP, "block", ["repeated_action"]),
]

# One mutation per Policy field, each differing from the default, for the digest coverage
# test. A field added to Policy without a mutation here fails the coverage assertion.
FIELD_MUTATIONS: dict[str, Any] = {
    "allowed_tools": {"only_this"},
    "forbidden_tools": {"never_this"},
    "required_tools": {"must_run"},
    "max_steps": 7,
    "max_duration_ms": 9_000,
    "max_tokens": 11_000,
    "max_estimated_cost_usd": 1.5,
    "max_repeated_action_count": 4,
    "pii_patterns": [r"\d{3}-\d{2}-\d{4}"],
    "secret_patterns": ["sk-[A-Za-z0-9]{16}"],
    "expected_output_mode": "exact",
    "max_tool_sequence_distance": 3,
    "trajectory_match_mode": "strict",
    "must_precede": [("first", "second")],
}


def _open(policy_path: Path = ACCESS_POLICY_PATH) -> tuple[GovernorTools, str]:
    tools = GovernorTools.from_policy(policy_path=policy_path)
    return tools, tools.open_session()["session_id"]


class PermissionMatrixTests(unittest.TestCase):
    def test_every_matrix_row_decides_exactly_as_specified(self) -> None:
        for name, prior, proposed, expected, rules in PERMISSION_MATRIX:
            with self.subTest(row=name):
                tools, session_id = _open()
                # The prefix is not asserted history: each call is really gated, and the
                # governor records it only because it allowed it.
                for call in prior:
                    granted = tools.propose_tool_call(session_id, call)
                    self.assertTrue(granted["allowed"], f"prefix call {call['tool']} was blocked: {granted['summary']}")
                decision = tools.propose_tool_call(session_id, proposed)
                self.assertEqual(decision["decision"], expected)
                self.assertEqual(sorted(f["rule_id"] for f in decision["blocking_findings"]), sorted(rules))

    def test_the_matrix_covers_both_verdicts_and_every_gate_rule_family(self) -> None:
        # Guards against a matrix that drifts into only-blocks (or only-allows) and stops
        # being evidence of anything.
        verdicts = {row[3] for row in PERMISSION_MATRIX}
        self.assertEqual(verdicts, {"allow", "block"})
        fired = {rule for row in PERMISSION_MATRIX for rule in row[4]}
        self.assertEqual(fired, {"tool_order", "tool_allowlist", "max_steps", "repeated_action"})


class SessionOwnsTheHistoryTests(unittest.TestCase):
    def test_only_allowed_calls_enter_the_record(self) -> None:
        governor = Governor.from_policy_file(ACCESS_POLICY_PATH)
        session = governor.open_session()
        self.assertFalse(session.propose(GRANT).allowed)
        self.assertEqual(session.tool_sequence, [], "a blocked call must not enter the record")
        self.assertTrue(session.propose(MANAGER).allowed)
        self.assertEqual(session.tool_sequence, ["manager_review"])
        # Retrying the blocked goal action changes nothing: the record still lacks the
        # second approval, so it is blocked again for the same reason.
        self.assertEqual(session.propose(GRANT).rule_ids, ["tool_order"])

    def test_a_truthful_supplied_history_is_accepted_and_a_forged_one_is_not(self) -> None:
        governor = Governor.from_policy_file(ACCESS_POLICY_PATH)
        session = governor.open_session()
        session.propose(MANAGER)
        session.propose(SECURITY)
        # Cross-checking is not a ban on sending a history: the true one passes through.
        self.assertTrue(session.propose(GRANT, client_history=[MANAGER, SECURITY]).allowed)

        forged = [
            ("an extra call the governor never allowed", [MANAGER, SECURITY, {"tool": "escalate"}]),
            ("the same calls reordered", [SECURITY, MANAGER]),
            ("a call with different arguments", [MANAGER, {"tool": "security_review", "input": {"ticket": "OTHER"}}]),
            ("a dropped call", [MANAGER]),
            ("nothing at all", []),
            ("a history that cannot be read", [{"not_a_tool": True}]),
        ]
        for why, history in forged:
            with self.subTest(history=why):
                decision = governor.open_session()
                decision.propose(MANAGER)
                decision.propose(SECURITY)
                verdict = decision.propose(GRANT, client_history=history)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.rule_ids, [SESSION_HISTORY_MISMATCH_RULE])
                (finding,) = verdict.blocking_findings
                self.assertEqual(finding.severity, "critical")
                self.assertEqual(finding.evidence["authorized_tools"], ["manager_review", "security_review"])

    def test_a_mismatched_history_is_refused_before_the_policy_rules_run(self) -> None:
        # Fail closed, not "block for whichever reason happens to apply": a forged history
        # must never be reconciled into a verdict, even when the call would be allowed.
        governor = Governor.from_policy_file(ACCESS_POLICY_PATH)
        session = governor.open_session()
        session.propose(MANAGER)
        session.propose(SECURITY)
        verdict = session.propose(GRANT, client_history=[SECURITY, MANAGER])
        self.assertEqual(verdict.rule_ids, [SESSION_HISTORY_MISMATCH_RULE])
        # And the refused call is not recorded as authorized.
        self.assertEqual(session.tool_sequence, ["manager_review", "security_review"])

    def test_sessions_do_not_share_a_record(self) -> None:
        tools, first = _open()
        second = tools.open_session()["session_id"]
        self.assertNotEqual(first, second)
        for call in (MANAGER, SECURITY):
            self.assertTrue(tools.propose_tool_call(first, call)["allowed"])
        self.assertTrue(tools.propose_tool_call(first, GRANT)["allowed"])
        # The approvals happened in the first session only.
        blocked = tools.propose_tool_call(second, GRANT)
        self.assertEqual(blocked["decision"], "block")
        self.assertEqual([f["rule_id"] for f in blocked["blocking_findings"]], ["tool_order"])

    def test_a_proposal_without_a_live_session_is_refused(self) -> None:
        tools, session_id = _open()
        for bad in ("session-does-not-exist", "", None, 7, [MANAGER], {"session_id": session_id}):
            with self.subTest(session_id=repr(bad)):
                decision = tools.propose_tool_call(bad, {"tool": "intake_request"})
                self.assertEqual(decision["decision"], "block")
                self.assertEqual([f["rule_id"] for f in decision["blocking_findings"]], [SESSION_UNKNOWN_RULE])
                self.assertIsNone(decision["session_id"])
        # The same call in the live session is allowed, so the refusal is about the
        # session and nothing else.
        self.assertTrue(tools.propose_tool_call(session_id, {"tool": "intake_request"})["allowed"])

    def test_session_findings_are_gate_integrity_not_policy_rules(self) -> None:
        # They are never emitted by a policy and never belong in an audit report; keeping
        # them out of the rule catalog is what makes that explicit.
        self.assertEqual(SESSION_RULES & set(RULE_DESCRIPTIONS), set())
        self.assertEqual(SESSION_RULES, {SESSION_UNKNOWN_RULE, SESSION_HISTORY_MISMATCH_RULE})


class PolicyDigestTests(unittest.TestCase):
    def test_digest_is_the_sha256_of_the_canonical_policy_text(self) -> None:
        policy = load_policy(ACCESS_POLICY_PATH)
        expected = hashlib.sha256(canonical_policy_text(policy).encode("utf-8")).hexdigest()
        self.assertEqual(policy_digest(policy), f"sha256:{expected}")

    def test_the_same_effective_policy_always_digests_the_same(self) -> None:
        # Set iteration order and the file's key order must not leak into the digest.
        first = Policy(allowed_tools={"a", "b", "c"}, must_precede=[("a", "b")])
        second = Policy(allowed_tools={"c", "b", "a"}, must_precede=[("a", "b")])
        self.assertEqual(policy_digest(first), policy_digest(second))

    def test_changing_any_policy_field_changes_the_digest(self) -> None:
        base = Policy()
        base_digest = policy_digest(base)
        self.assertEqual(
            set(FIELD_MUTATIONS),
            {policy_field.name for policy_field in fields(Policy)},
            "a Policy field has no digest-coverage mutation — add one, or it can change unnoticed",
        )
        for name, value in FIELD_MUTATIONS.items():
            with self.subTest(field=name):
                self.assertNotEqual(policy_digest(replace(base, **{name: value})), base_digest)

    def test_every_decision_carries_the_digest_of_the_policy_that_decided_it(self) -> None:
        governor = Governor.from_policy_file(ACCESS_POLICY_PATH)
        expected = policy_digest(load_policy(ACCESS_POLICY_PATH))
        session = governor.open_session()
        allowed = session.propose(MANAGER)
        blocked = session.propose(GRANT)
        offline = governor.evaluate([], GRANT)
        for decision in (allowed, blocked, offline):
            self.assertEqual(decision.policy_digest, expected)
            self.assertEqual(decision.to_dict()["policy_digest"], expected)
        # A different policy decides the same call under a different digest.
        other = Governor(replace(load_policy(ACCESS_POLICY_PATH), max_steps=11))
        self.assertNotEqual(other.open_session().propose(MANAGER).policy_digest, expected)

    def test_a_decision_names_its_session_and_an_offline_evaluation_has_none(self) -> None:
        governor = Governor.from_policy_file(ACCESS_POLICY_PATH)
        session = governor.open_session()
        self.assertEqual(session.propose(MANAGER).session_id, session.session_id)
        self.assertIsNone(governor.evaluate([], MANAGER).session_id)


if __name__ == "__main__":
    unittest.main()
