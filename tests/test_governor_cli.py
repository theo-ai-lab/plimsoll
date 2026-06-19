"""CLI surface for the runtime governor: `plimsoll governor` and `plimsoll-governor` (MCP).

These lock the productized entry points — the one-shot deterministic gate subcommand and the
MCP server launcher — so the governor is usable, not library-only. Everything here stays
offline: no LLM, no network, no third-party import.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plimsoll.cli import main
from plimsoll.governor_mcp import main as mcp_main


class GovernorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-gov-cli-"))
        self.policy = self.tmp / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "allowed_tools": ["search", "manager_review", "grant_access"],
                    "forbidden_tools": ["delete_database"],
                    "must_precede": [{"before": "manager_review", "after": "grant_access"}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, payload) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _run(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        ctx = mock.patch("sys.stdin", io.StringIO(stdin)) if stdin is not None else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["governor", *args])
        return code, out.getvalue(), err.getvalue()

    def test_clean_call_is_allowed_exit_0(self) -> None:
        call = self._write("call.json", {"tool": "search"})
        code, _, err = self._run("--policy", str(self.policy), "--call", str(call))
        self.assertEqual(code, 0)
        self.assertIn("allow", err)
        self.assertIn("search", err)

    def test_forbidden_call_is_blocked_exit_1(self) -> None:
        call = self._write("call.json", {"tool": "delete_database"})
        code, _, err = self._run("--policy", str(self.policy), "--call", str(call))
        self.assertEqual(code, 1)
        self.assertIn("block", err)
        self.assertIn("forbidden_tool", err)

    def test_call_is_read_from_stdin_when_call_omitted(self) -> None:
        code, _, err = self._run("--policy", str(self.policy), stdin='"search"')
        self.assertEqual(code, 0)
        self.assertIn("allow", err)

    def test_dash_call_reads_stdin(self) -> None:
        code, _, _ = self._run("--policy", str(self.policy), "--call", "-", stdin='{"tool": "delete_database"}')
        self.assertEqual(code, 1)

    def test_partial_trace_gates_ordering(self) -> None:
        call = self._write("call.json", {"tool": "grant_access"})
        # grant_access before any manager_review -> blocked.
        before = self._write("before.json", ["search"])
        code, _, err = self._run("--policy", str(self.policy), "--call", str(call), "--partial-trace", str(before))
        self.assertEqual(code, 1)
        self.assertIn("tool_order", err)
        # Once manager_review has run, the same call is allowed.
        after = self._write("after.json", ["search", "manager_review"])
        code, _, _ = self._run("--policy", str(self.policy), "--call", str(call), "--partial-trace", str(after))
        self.assertEqual(code, 0)

    def test_json_output_is_machine_readable(self) -> None:
        call = self._write("call.json", {"tool": "grant_access"})
        code, out, _ = self._run("--policy", str(self.policy), "--call", str(call), "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["proposed_tool"], "grant_access")
        self.assertIn("tool_order", {f["rule_id"] for f in payload["blocking_findings"]})

    def test_quiet_suppresses_human_line(self) -> None:
        call = self._write("call.json", {"tool": "search"})
        code, _, err = self._run("--policy", str(self.policy), "--call", str(call), "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_no_policy_is_permissive(self) -> None:
        # With no policy, an empty Policy applies: nothing is gated, so any call is allowed.
        code, _, _ = self._run(stdin='{"tool": "anything_at_all"}')
        self.assertEqual(code, 0)

    def test_empty_stdin_is_usage_error_exit_2(self) -> None:
        code, _, err = self._run("--policy", str(self.policy), stdin="")
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_invalid_json_is_usage_error_exit_2(self) -> None:
        code, _, err = self._run("--policy", str(self.policy), stdin="{not json")
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_call_without_tool_field_is_usage_error_exit_2(self) -> None:
        call = self._write("call.json", {"input": {"q": "x"}})
        code, _, _ = self._run("--policy", str(self.policy), "--call", str(call))
        self.assertEqual(code, 2)


class GovernorMcpEntryPointTests(unittest.TestCase):
    """The `plimsoll-governor` console entry point (plimsoll.governor_mcp:main)."""

    def test_without_sdk_returns_2_with_install_hint(self) -> None:
        err = io.StringIO()
        with mock.patch("plimsoll.governor_mcp._HAS_MCP", False), contextlib.redirect_stderr(err):
            code = mcp_main([])
        self.assertEqual(code, 2)
        self.assertIn("mcp", err.getvalue())
        self.assertIn("not installed", err.getvalue())

    def test_with_sdk_builds_server_and_runs(self) -> None:
        # Exercise main's wiring without the optional SDK: a fake build_server stands in for
        # the FastMCP server so we can assert main loads a policy, builds, and runs the server.
        served = {}

        class _FakeServer:
            def run(self) -> None:
                served["ran"] = True

        def _fake_build_server(governor, name="plimsoll-governor"):
            served["name"] = name
            served["governor"] = governor
            return _FakeServer()

        with (
            mock.patch("plimsoll.governor_mcp._HAS_MCP", True),
            mock.patch("plimsoll.governor_mcp.build_server", _fake_build_server),
        ):
            code = mcp_main(["--name", "custom-governor"])
        self.assertEqual(code, 0)
        self.assertTrue(served["ran"])
        self.assertEqual(served["name"], "custom-governor")


if __name__ == "__main__":
    unittest.main()
