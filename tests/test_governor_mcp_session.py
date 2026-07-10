"""Locks the committed MCP governor session to the code, so the demo cannot drift.

``examples/mcp-governor-session/transcript.jsonl`` is a scripted, deterministic JSON-RPC
session captured against the real ``plimsoll-governor`` stdio server (see
``scripts/build_mcp_governor_session.py``). These tests replay the recorded gate calls, so a
governor whose verdicts drift — or a stale transcript — fails the suite:

* always (no ``mcp`` SDK needed): every recorded ``propose_tool_call``'s arguments are fed
  through the same :class:`GovernorTools` surface the server wraps, and the resulting
  decision must equal the recorded ``structuredContent`` exactly;
* when the optional ``mcp`` extra is installed: the recorded client messages are replayed
  against a fresh, real stdio server subprocess and the responses' verdicts must match.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

from plimsoll.governor_mcp import GovernorTools

ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = ROOT / "examples" / "mcp-governor-session"
TRANSCRIPT_PATH = SESSION_DIR / "transcript.jsonl"
POLICY_PATH = SESSION_DIR / "policy.json"
SCRIPT_PATH = ROOT / "scripts" / "build_mcp_governor_session.py"

# The three documented outcomes, in session order: (proposed tool, decision, rule_ids).
EXPECTED_OUTCOMES = [
    ("read_record", "allow", []),
    ("grant_access", "block", ["tool_order", "tool_order"]),
    ("summarize", "block", ["max_tokens"]),
]


def _load_script():
    spec = importlib.util.spec_from_file_location("build_mcp_governor_session", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolve their owning module via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# The builder script owns the one id-pairing implementation; replaying through the same
# helper means the tests cannot diverge from how the transcript was actually captured.
_SCRIPT = _load_script()


def _load_transcript() -> list[dict]:
    lines = TRANSCRIPT_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def _gate_exchanges(records: list[dict]) -> list[tuple[dict, dict]]:
    """The recorded (request, response) pairs for ``tools/call``, matched by JSON-RPC id."""
    return _SCRIPT.gate_exchanges(records)


class McpGovernorTranscriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = _load_transcript()
        cls.exchanges = _gate_exchanges(cls.records)

    def test_transcript_records_the_three_documented_outcomes(self) -> None:
        self.assertEqual(len(self.exchanges), len(EXPECTED_OUTCOMES))
        for (request, response), (tool, decision, rules) in zip(self.exchanges, EXPECTED_OUTCOMES):
            self.assertEqual(request["params"]["name"], "propose_tool_call")
            self.assertEqual(request["params"]["arguments"]["proposed_call"]["tool"], tool)
            result = response["result"]
            self.assertFalse(result["isError"])
            recorded = result["structuredContent"]
            self.assertEqual(recorded["decision"], decision)
            self.assertEqual([finding["rule_id"] for finding in recorded["blocking_findings"]], rules)
            # The unstructured text content must agree with the structured decision.
            self.assertEqual(json.loads(result["content"][0]["text"]), recorded)

    def test_denied_call_is_the_allowed_goal_action_not_a_strawman(self) -> None:
        # The DENY outcome must stay a genuinely tempting call: grant_access is ON the
        # allowlist and is the task's goal action — only the missing approvals block it.
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.assertIn("grant_access", policy["allowed_tools"])
        _, response = self.exchanges[1]
        findings = response["result"]["structuredContent"]["blocking_findings"]
        self.assertEqual({finding["evidence"]["before"] for finding in findings}, {"manager_review", "security_review"})
        self.assertTrue(all(finding["severity"] == "critical" for finding in findings))

    def test_budget_block_evidence_shows_the_cumulative_overrun(self) -> None:
        _, response = self.exchanges[2]
        (finding,) = response["result"]["structuredContent"]["blocking_findings"]
        self.assertEqual(finding["rule_id"], "max_tokens")
        self.assertGreater(finding["evidence"]["actual"], finding["evidence"]["limit"])

    def test_recorded_arguments_reproduce_identical_decisions_without_the_sdk(self) -> None:
        # Feed each recorded request through the same GovernorTools surface the server
        # wraps: the live decision must equal the committed structuredContent exactly.
        # This pins the demo to the engine with no optional dependency involved.
        tools = GovernorTools.from_policy(policy_path=POLICY_PATH)
        for request, response in self.exchanges:
            arguments = request["params"]["arguments"]
            decision = tools.propose_tool_call(arguments["partial_trace"], arguments["proposed_call"])
            self.assertEqual(decision, response["result"]["structuredContent"])


class McpGovernorStdioReplayTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("mcp") is not None, "requires the optional mcp extra")
    def test_replaying_the_committed_session_against_a_real_server_matches(self) -> None:
        # End-to-end: send the committed client messages to a fresh real stdio server
        # subprocess and require the same three verdicts on the wire.
        script = _SCRIPT
        records = _load_transcript()
        client_messages = [record["message"] for record in records if record["direction"] == "client->server"]
        replayed = script.run_session(script.default_server_command(POLICY_PATH), client_messages)
        recorded_responses = [response for _, response in _gate_exchanges(records)]
        replayed_responses = [response for _, response in _gate_exchanges(replayed)]
        self.assertEqual(len(replayed_responses), len(recorded_responses))
        for recorded, replayed_response in zip(recorded_responses, replayed_responses):
            self.assertFalse(replayed_response["result"]["isError"])
            self.assertEqual(replayed_response["result"]["structuredContent"], recorded["result"]["structuredContent"])


if __name__ == "__main__":
    unittest.main()
