"""Regenerate the committed MCP governor session under ``examples/mcp-governor-session/``.

This is a SCRIPTED, DETERMINISTIC client driving the REAL ``plimsoll-governor`` stdio MCP
server — a subprocess running the same ``plimsoll.governor_mcp:main`` entry point the
console script installs. It is not a live-model session: the "agent" side is this script,
so the run is reproducible byte-for-byte and the committed transcript can be replayed in a
test. The server side is entirely real — every verdict in the transcript was produced by
the served governor gating a proposed tool call before execution.

The session walks one access-request episode through three gate outcomes:

  1. ALLOW  — ``read_record`` after a ticket search: no rule fires.
  2. DENY   — ``grant_access``, the task's goal action, proposed before the required
              ``manager_review``/``security_review`` have run: blocked by ``tool_order``.
              This is the tempting call — the shortest path to task completion — not a
              strawman like a forbidden destructive tool.
  3. BUDGET — ``summarize`` over the full ticket history after both approvals: the call's
              token estimate pushes the cumulative total over ``max_tokens``.

The client speaks raw newline-delimited JSON-RPC 2.0 over the subprocess pipes (the MCP
stdio transport) using only the standard library, so nothing here depends on the ``mcp``
SDK — only the server subprocess does (``pip install "plimsoll[mcp]"``).

Every verdict is verified against the scripted expectation, and the whole session is
captured twice and byte-compared, so the "deterministic" claim is checked on every run,
not asserted. Run from the repository root::

    python scripts/build_mcp_governor_session.py

Writes:
  examples/mcp-governor-session/transcript.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = ROOT / "examples" / "mcp-governor-session"
POLICY_PATH = SESSION_DIR / "policy.json"
TRANSCRIPT_PATH = SESSION_DIR / "transcript.jsonl"

PROTOCOL_VERSION = "2025-06-18"
READ_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class GateCall:
    """One scripted ``propose_tool_call`` with its ground-truth expectation."""

    why: str
    partial_trace: list[dict[str, Any]]
    proposed_call: dict[str, Any]
    expect_decision: str
    expect_rules: tuple[str, ...]


# The scripted episode: an IT access-request agent working ticket REQ-4821 ("grant
# contractor-7 read access to prod-db"). The token numbers on prior calls are the usage the
# host has already recorded; on the proposed call they are the estimate the gate accounts.
_SEARCH = {"tool": "search_tickets", "input": {"query": "REQ-4821"}, "input_tokens": 120, "output_tokens": 40}
_READ = {"tool": "read_record", "input": {"record_id": "REQ-4821"}, "input_tokens": 500, "output_tokens": 300}
_MANAGER = {"tool": "manager_review", "input": {"ticket": "REQ-4821"}, "input_tokens": 200, "output_tokens": 100}
_SECURITY = {"tool": "security_review", "input": {"ticket": "REQ-4821"}, "input_tokens": 200, "output_tokens": 100}

SESSION: list[GateCall] = [
    GateCall(
        why="open the access request after finding the ticket - an ordinary allowed step",
        partial_trace=[_SEARCH],
        proposed_call=_READ,
        expect_decision="allow",
        expect_rules=(),
    ),
    GateCall(
        why="the task's goal action, proposed before either required review has run",
        partial_trace=[_SEARCH, _READ],
        proposed_call={
            "tool": "grant_access",
            "input": {"resource": "prod-db", "requester": "contractor-7", "level": "read"},
        },
        expect_decision="block",
        expect_rules=("tool_order", "tool_order"),
    ),
    GateCall(
        why="after both approvals, summarizing the full ticket history blows the token budget",
        partial_trace=[_SEARCH, _READ, _MANAGER, _SECURITY],
        proposed_call={
            "tool": "summarize",
            "input": {"scope": "full ticket history for the approval note"},
            "input_tokens": 2600,
        },
        expect_decision="block",
        expect_rules=("max_tokens",),
    ),
]


def default_server_command(policy_path: Path) -> list[str]:
    """The real server: the same ``main()`` the ``plimsoll-governor`` console script runs."""
    return [sys.executable, "-m", "plimsoll.governor_mcp", "--policy", str(policy_path)]


class StdioServer:
    """A governor server subprocess plus newline-delimited JSON-RPC send/receive."""

    def __init__(self, command: list[str]) -> None:
        env = dict(os.environ)
        # Make the checkout importable in the child even when plimsoll is not installed.
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._pumps = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for pump in self._pumps:
            pump.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_tail.append(line.rstrip())

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout: float = READ_TIMEOUT_S) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.proc.kill()
            # A hung server (e.g. an SDK incompatibility stalling initialize) is diagnosed
            # from its stderr, same as an exited one.
            raise RuntimeError(f"no server response within {timeout:.0f}s\n{self._stderr_report()}") from None
        if line is None:
            raise RuntimeError(
                "server closed stdout before responding "
                f"(exit code {self.proc.poll()}). Is the optional MCP extra installed? "
                'Run: pip install "plimsoll[mcp]"\n'
                f"{self._stderr_report()}"
            )
        return json.loads(line)

    def _stderr_report(self) -> str:
        return "server stderr tail:\n" + "\n".join(self._stderr_tail)

    def close(self) -> int:
        # Closing stdin is the MCP stdio shutdown signal: the server exits at EOF.
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=READ_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        for pump in self._pumps:
            pump.join(timeout=READ_TIMEOUT_S)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        return code


def build_client_messages(session: list[GateCall]) -> list[dict[str, Any]]:
    """The full ordered client side of the session, with deterministic request ids."""
    from plimsoll import __version__

    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "plimsoll-scripted-session", "version": __version__},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ]
    for index, call in enumerate(session):
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": 2 + index,
                "method": "tools/call",
                "params": {
                    "name": "propose_tool_call",
                    "arguments": {"partial_trace": call.partial_trace, "proposed_call": call.proposed_call},
                },
            }
        )
    return messages


def run_session(command: list[str], client_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drive one full session; return direction-tagged transcript records in wire order."""
    server = StdioServer(command)
    records: list[dict[str, Any]] = []
    seq = 0
    try:
        for message in client_messages:
            seq += 1
            records.append({"seq": seq, "direction": "client->server", "message": message})
            server.send(message)
            if "id" in message:  # requests get a response; notifications do not
                response = server.receive()
                seq += 1
                records.append({"seq": seq, "direction": "server->client", "message": response})
    finally:
        server.close()
    return records


def gate_exchanges(records: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The recorded ``tools/call`` (request, response) pairs, matched by JSON-RPC id.

    The single id-pairing implementation: the replay tests import this too, so the builder
    and the suite cannot diverge in how requests are matched to responses.
    """
    requests = {
        record["message"]["id"]: record["message"]
        for record in records
        if record["direction"] == "client->server" and record["message"].get("method") == "tools/call"
    }
    return [
        (requests[record["message"]["id"]], record["message"])
        for record in records
        if record["direction"] == "server->client" and record["message"].get("id") in requests
    ]


def gate_responses(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``tools/call`` responses, in order, paired to requests by JSON-RPC id."""
    return [response for _, response in gate_exchanges(records)]


def verify(records: list[dict[str, Any]], session: list[GateCall]) -> list[str]:
    """Cross-check every gate verdict against its scripted expectation; return mismatches."""
    problems: list[str] = []
    responses = gate_responses(records)
    if len(responses) != len(session):
        return [f"expected {len(session)} gate responses, got {len(responses)}"]
    for index, (call, response) in enumerate(zip(session, responses), start=1):
        result = response.get("result", {})
        decision = result.get("structuredContent", {})
        rules = [finding["rule_id"] for finding in decision.get("blocking_findings", [])]
        if result.get("isError"):
            problems.append(f"call {index}: server returned isError=true")
        if decision.get("decision") != call.expect_decision:
            problems.append(f"call {index}: expected {call.expect_decision!r}, got {decision.get('decision')!r}")
        if rules != list(call.expect_rules):
            problems.append(f"call {index}: expected rules {list(call.expect_rules)}, got {rules}")
    return problems


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--out",
        type=Path,
        default=TRANSCRIPT_PATH,
        help="where to write the transcript (default: the committed examples/mcp-governor-session/transcript.jsonl)",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))  # make the checkout importable when run as a plain script
    command = default_server_command(POLICY_PATH)
    client_messages = build_client_messages(SESSION)

    print("Plimsoll governor over MCP (stdio) — scripted deterministic session")
    print(f"  server: python -m plimsoll.governor_mcp --policy {_relative(POLICY_PATH)}")
    print("          (the same entry point the `plimsoll-governor` console script runs)")

    # Capture the session twice and byte-compare: the "deterministic" claim is verified on
    # every regeneration, never assumed.
    records = run_session(command, client_messages)
    second = run_session(command, client_messages)
    if records != second:
        print("error: two captures of the same scripted session differ; not committing", file=sys.stderr)
        return 1

    problems = verify(records, SESSION)
    for index, (call, response) in enumerate(zip(SESSION, gate_responses(records)), start=1):
        decision = response["result"]["structuredContent"]
        rules = ", ".join(finding["rule_id"] for finding in decision["blocking_findings"])
        verdict = "allow" if decision["allowed"] else "BLOCK"
        suffix = f"  [{rules}]" if rules else ""
        print(f"  call {index}  {verdict:<5}  {decision['proposed_tool']:<14}{suffix}")
        print(f"          {call.why}")

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    print(f"  verified: {len(SESSION)}/{len(SESSION)} verdicts matched the script; captured twice, byte-identical")
    print(f"  transcript: {_relative(args.out)} ({len(records)} messages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
