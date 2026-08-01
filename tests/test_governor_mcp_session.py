"""Locks the committed MCP governor session to the code, so the demo cannot drift.

``examples/mcp-governor-session/transcript.jsonl`` is a scripted, deterministic JSON-RPC
session captured against the real ``plimsoll-governor`` stdio server (see
``scripts/build_mcp_governor_session.py``). These tests replay the recorded gate calls, so a
governor whose verdicts drift — or a stale transcript — fails the suite:

* always (no ``mcp`` SDK needed): every recorded ``tools/call``'s arguments are fed through
  the same SDK-free handlers the server wraps, in wire order, and each resulting decision
  must equal the recorded ``structuredContent`` exactly;
* when the optional ``mcp`` extra is installed: the recorded client messages are replayed
  against a fresh, real stdio server subprocess and the responses' verdicts must match.

The session is also the security walkthrough: the server opens the session and keeps the
record of what it allowed, and the recorded retry that supplies a forged history — one
claiming the two approvals already ran — is refused on the wire.
"""

import contextlib
import importlib
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path

from plimsoll import governor_mcp
from plimsoll.governor import Governor
from plimsoll.governor_mcp import make_handlers
from plimsoll.io import load_policy
from plimsoll.policy import policy_digest

ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = ROOT / "examples" / "mcp-governor-session"
TRANSCRIPT_PATH = SESSION_DIR / "transcript.jsonl"
POLICY_PATH = SESSION_DIR / "policy.json"
SCRIPT_PATH = ROOT / "scripts" / "build_mcp_governor_session.py"

# The documented outcomes, in session order: (proposed tool, decision, rule_ids).
EXPECTED_OUTCOMES = [
    ("search_tickets", "allow", []),
    ("read_record", "allow", []),
    ("grant_access", "block", ["tool_order", "tool_order"]),
    ("grant_access", "block", ["session_history_mismatch"]),
    ("manager_review", "allow", []),
    ("security_review", "allow", []),
    ("grant_access", "allow", []),
    ("summarize", "block", ["max_tokens"]),
]
# Index in EXPECTED_OUTCOMES of the two calls the walkthrough turns on.
DENIED_GOAL_ACTION = 2
FORGED_HISTORY_RETRY = 3


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
        cls.responses = {
            record["message"]["id"]: record["message"]
            for record in cls.records
            if record["direction"] == "server->client" and "id" in record["message"]
        }

    def test_transcript_records_the_documented_outcomes(self) -> None:
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
        _, response = self.exchanges[DENIED_GOAL_ACTION]
        findings = response["result"]["structuredContent"]["blocking_findings"]
        self.assertEqual({finding["evidence"]["before"] for finding in findings}, {"manager_review", "security_review"})
        self.assertTrue(all(finding["severity"] == "critical" for finding in findings))

    def test_the_session_holds_the_history_and_a_forged_one_is_refused(self) -> None:
        # The retry sends a partial_trace claiming both reviews ran. The server compares it
        # with the record it kept and fails closed instead of taking the client's word.
        request, response = self.exchanges[FORGED_HISTORY_RETRY]
        claimed = [call["tool"] for call in request["params"]["arguments"]["partial_trace"]]
        self.assertIn("manager_review", claimed)
        self.assertIn("security_review", claimed)
        (finding,) = response["result"]["structuredContent"]["blocking_findings"]
        self.assertEqual(finding["rule_id"], "session_history_mismatch")
        self.assertEqual(finding["severity"], "critical")
        # The evidence contrasts what the governor authorized with what was claimed.
        self.assertEqual(finding["evidence"]["authorized_tools"], ["search_tickets", "read_record"])
        self.assertEqual(finding["evidence"]["supplied_tools"], claimed)
        # Every other gate call rides the session alone — no history is supplied at all.
        for index, (other, _) in enumerate(self.exchanges):
            if index != FORGED_HISTORY_RETRY:
                self.assertNotIn("partial_trace", other["params"]["arguments"])

    def test_every_decision_binds_to_the_session_and_the_exact_policy(self) -> None:
        _, opened = _SCRIPT.session_exchange(self.records)
        handle = opened["result"]["structuredContent"]
        # The digest is the SHA-256 of the effective served policy, recomputed here from
        # the committed policy file, so the transcript cannot claim a policy it did not use.
        expected = policy_digest(load_policy(POLICY_PATH))
        self.assertEqual(handle["policy_digest"], expected)
        for _, response in self.exchanges:
            decision = response["result"]["structuredContent"]
            self.assertEqual(decision["session_id"], handle["session_id"])
            self.assertEqual(decision["policy_digest"], expected)

    def test_budget_block_evidence_shows_the_cumulative_overrun(self) -> None:
        _, response = self.exchanges[-1]
        (finding,) = response["result"]["structuredContent"]["blocking_findings"]
        self.assertEqual(finding["rule_id"], "max_tokens")
        self.assertGreater(finding["evidence"]["actual"], finding["evidence"]["limit"])

    def test_recorded_arguments_reproduce_identical_decisions_without_the_sdk(self) -> None:
        # Replay every recorded tools/call through the same SDK-free handlers the server
        # wraps, in wire order: each live decision must equal the committed
        # structuredContent exactly. This pins the demo to the engine with no optional
        # dependency involved — and only reproduces if the fresh governor assigns the same
        # session handle, which is the determinism the transcript depends on.
        handlers = make_handlers(Governor(load_policy(POLICY_PATH)))
        replayed = 0
        for record in self.records:
            message = record["message"]
            if record["direction"] != "client->server" or message.get("method") != "tools/call":
                continue
            params = message["params"]
            result = handlers[params["name"]](**params["arguments"])
            self.assertEqual(result, self.responses[message["id"]]["result"]["structuredContent"])
            replayed += 1
        self.assertEqual(replayed, len(EXPECTED_OUTCOMES) + 1)  # the gate calls plus open_session


class OptionalSdkAvailabilityTests(unittest.TestCase):
    """`_HAS_MCP` must mean "we can serve", not merely "something named mcp is installed".

    The SDK's server layout is not stable across majors: the wiring `build_server` imports
    (`mcp.server.fastmcp`) is absent from mcp 2.x. Detecting only the top-level package
    turns that into a traceback out of the launcher instead of the documented install hint.
    """

    def test_availability_tracks_the_wiring_the_server_actually_imports(self) -> None:
        expected = (
            importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("mcp.server.fastmcp") is not None
        )
        self.assertEqual(governor_mcp._HAS_MCP, expected)

    def test_an_sdk_without_the_server_wiring_reports_an_install_hint_not_a_traceback(self) -> None:
        # Simulate an installed SDK whose server wiring cannot be imported: `import mcp`
        # succeeds, `from mcp.server.fastmcp import FastMCP` does not.
        saved = {name: module for name, module in sys.modules.items() if name == "mcp" or name.startswith("mcp.")}
        for name in saved:
            del sys.modules[name]
        sys.modules["mcp"] = types.ModuleType("mcp")
        try:
            module = importlib.reload(governor_mcp)
            self.assertFalse(module._HAS_MCP)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = module.main(["--policy", str(POLICY_PATH)])
            self.assertEqual(code, 2)
            self.assertIn("mcp", stderr.getvalue())
            # The SDK-free surface still works with no SDK we can serve with.
            handlers = module.make_handlers(Governor(load_policy(POLICY_PATH)))
            session = handlers["open_session"]()["session_id"]
            self.assertTrue(handlers["propose_tool_call"](session, "search_tickets")["allowed"])
        finally:
            del sys.modules["mcp"]
            sys.modules.update(saved)
            importlib.reload(governor_mcp)


class McpGovernorStdioReplayTests(unittest.TestCase):
    @unittest.skipUnless(governor_mcp._HAS_MCP, "requires an optional mcp SDK the server can wire up")
    def test_replaying_the_committed_session_against_a_real_server_matches(self) -> None:
        # End-to-end: send the committed client messages to a fresh real stdio server
        # subprocess and require the same verdicts on the wire.
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
