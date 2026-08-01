"""MCP-style tool surface for the Plimsoll runtime :class:`~plimsoll.governor.Governor`.

This exposes three tools a live agent (or an MCP host) can call:

  * ``open_session()`` — start a gate session. The server keeps the record of what it
    allows; the handle it returns identifies that record.
  * ``propose_tool_call(session_id, proposed_call, partial_trace=None)`` — the
    pre-execution GATE: decide whether the next tool call is allowed, with the rule that
    fired. History comes from the session, never from the caller: ``partial_trace`` is
    optional and, when sent, is only cross-checked (a mismatch fails closed).
  * ``check_trace(trace)`` — the full post-hoc audit, mirroring the CLI (``evaluate_trace``).

A proposal with no valid session is refused (``session_unknown``) rather than judged
against an empty history — an unsessioned gate would treat every call as the first one,
which is exactly the free budget and forged ordering the session exists to prevent.

All handlers take and return plain JSON-able values, so they work with or without the
MCP SDK and are trivially unit-testable.

Optional dependency
-------------------
The ``mcp`` SDK is OPTIONAL. If it is not installed, this module still works as plain
callables — build them with :func:`make_handlers` or use :class:`GovernorTools` directly.
The MCP server wiring (:func:`build_server`) is only available when ``mcp`` is importable.

    pip install mcp

Without it, ``_HAS_MCP`` is False and only the plain-callable surface is exposed.

This preserves Plimsoll's zero-dependency identity: the core engine never imports ``mcp``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from plimsoll.governor import SESSION_UNKNOWN_RULE, Decision, Governor, ProposedToolCall
from plimsoll.io import load_policy, parse_trace
from plimsoll.models import Finding, JsonObject, Policy, TraceRun, ValidationError

try:  # The MCP SDK is an optional extra; the engine works fine without it.
    import mcp  # type: ignore  # noqa: F401

    _HAS_MCP = True
except ImportError:  # pragma: no cover - exercised only where the optional extra is absent
    # MCP SDK not installed. To serve these tools over MCP, run `pip install mcp`.
    # The plain callables below remain fully functional without it.
    _HAS_MCP = False


def _finding_to_dict(finding: Finding) -> JsonObject:
    return {
        "rule_id": finding.rule_id,
        "severity": finding.severity,
        "case_id": finding.case_id,
        "message": finding.message,
        "evidence": finding.evidence,
    }


class GovernorTools:
    """Thin, JSON-in/JSON-out adapter over a :class:`Governor` for the tool surface."""

    def __init__(self, governor: Governor) -> None:
        self.governor = governor

    @classmethod
    def from_policy(
        cls,
        policy: Policy | None = None,
        *,
        policy_path: str | Path | None = None,
    ) -> GovernorTools:
        """Construct from an in-memory :class:`Policy` (preferred for tests) or a file path."""
        if policy is not None:
            return cls(Governor(policy))
        return cls(Governor(load_policy(Path(policy_path) if policy_path is not None else None)))

    def open_session(self) -> JsonObject:
        """Open a gate session. The returned handle names the governor's own call record."""
        session = self.governor.open_session()
        return {
            "session_id": session.session_id,
            "policy_digest": self.governor.policy_digest,
            "authorized_tools": session.tool_sequence,
        }

    def propose_tool_call(self, session_id: Any, proposed_call: Any, partial_trace: Any = None) -> JsonObject:
        """Gate a proposed tool call before it executes. Returns a serialized Decision.

        ``session_id`` must be a handle from :meth:`open_session`; the history is the one
        the governor recorded for it. ``partial_trace`` is optional and never used as the
        record — if sent, it must agree with the governor's or the call is blocked.
        """
        session = self.governor.session(session_id)
        if session is None:
            return self._no_session(session_id, proposed_call).to_dict()
        decision: Decision = session.propose(proposed_call, client_history=partial_trace)
        return decision.to_dict()

    def _no_session(self, session_id: Any, proposed_call: Any) -> Decision:
        """Fail closed: a proposal without a live session is refused, not judged blind."""
        try:
            tool = ProposedToolCall.from_obj(proposed_call).tool
        except ValidationError:
            tool = "<unreadable>"
        finding = Finding(
            rule_id=SESSION_UNKNOWN_RULE,
            severity="critical",
            case_id="session",
            message=f"'{tool}' is blocked: no open governor session. Call open_session first.",
            evidence={"session_id": session_id if isinstance(session_id, str) else None},
        )
        return Decision(proposed_tool=tool, blocking_findings=[finding], policy_digest=self.governor.policy_digest)

    def check_trace(self, trace: Any, baseline: Any = None) -> JsonObject:
        """Run the full deterministic audit over a completed trace (post-hoc)."""
        run = trace if isinstance(trace, TraceRun) else parse_trace(trace, source="<mcp>")
        base = None
        if baseline is not None:
            base = baseline if isinstance(baseline, TraceRun) else parse_trace(baseline, source="<mcp:baseline>")
        findings = self.governor.check_trace(run, base)
        return {
            "findings": [_finding_to_dict(finding) for finding in findings],
            "finding_count": len(findings),
            "ok": not any(finding.severity in {"critical", "high"} for finding in findings),
        }


def make_handlers(governor: Governor) -> dict[str, Any]:
    """Return plain ``{name: callable}`` handlers — the SDK-free tool surface.

    All three share one :class:`GovernorTools`, so a session opened through the returned
    ``open_session`` is the session ``propose_tool_call`` gates against.
    """
    tools = GovernorTools(governor)
    return {
        "open_session": tools.open_session,
        "propose_tool_call": tools.propose_tool_call,
        "check_trace": tools.check_trace,
    }


def build_server(governor: Governor, name: str = "plimsoll-governor") -> Any:
    """Build an MCP ``FastMCP`` server exposing the two governor tools.

    Requires the optional ``mcp`` SDK. When it is absent this raises; use
    :func:`make_handlers` for the SDK-free path. The wiring is intentionally thin —
    all logic lives in :class:`GovernorTools`/:class:`Governor`.
    """
    if not _HAS_MCP:  # pragma: no cover - depends on the optional extra being absent
        raise RuntimeError(
            "the 'mcp' SDK is not installed; run `pip install mcp` to serve the governor "
            "over MCP, or use make_handlers() for the SDK-free callable surface"
        )
    from mcp.server.fastmcp import FastMCP  # type: ignore  # imported lazily; optional extra

    server = FastMCP(name)
    tools = GovernorTools(governor)

    @server.tool()
    def open_session() -> JsonObject:
        """Open a gate session; the server keeps the record of the calls it allows."""
        return tools.open_session()

    @server.tool()
    def propose_tool_call(session_id: str, proposed_call: Any, partial_trace: Any = None) -> JsonObject:
        """Decide whether a proposed next tool call is allowed in this gate session.

        The history is the server's own record for ``session_id``. ``partial_trace`` is
        optional; when supplied it is only cross-checked, and a mismatch blocks the call.
        """
        return tools.propose_tool_call(session_id, proposed_call, partial_trace)

    @server.tool()
    def check_trace(trace: Any, baseline: Any = None) -> JsonObject:
        """Run the full deterministic Plimsoll audit over a completed trace."""
        return tools.check_trace(trace, baseline)

    return server


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``plimsoll-governor``): serve the governor over MCP on stdio.

    Loads a policy, builds the :func:`build_server` ``FastMCP`` server, and runs it so an MCP
    host can call ``propose_tool_call`` (the gate) and ``check_trace`` (the audit). The ``mcp``
    SDK is an OPTIONAL extra: when it is absent this prints an honest install hint and exits 2
    — no silent fallback — so the zero-dependency core install is never affected. The SDK-free
    callable surface (:func:`make_handlers` / :class:`GovernorTools`) needs no extra at all.
    """
    parser = argparse.ArgumentParser(
        prog="plimsoll-governor",
        description="Serve the deterministic Plimsoll governor over MCP (stdio transport). "
        "No LLM, no outbound network — the same offline rule engine, exposed as MCP tools.",
    )
    parser.add_argument(
        "--policy", type=Path, default=None, help="policy JSON file (default: a permissive empty policy)"
    )
    parser.add_argument(
        "--name",
        default="plimsoll-governor",
        help="server name advertised to the MCP host (default: plimsoll-governor)",
    )
    args = parser.parse_args(argv)

    if not _HAS_MCP:
        print(
            "error: the 'mcp' SDK is not installed. Install the optional extra "
            "(from a clone: `python -m pip install -e '.[mcp]'`, or `pip install mcp`) "
            "to serve the governor over MCP. "
            "The SDK-free callable surface (make_handlers / GovernorTools) works without it.",
            file=sys.stderr,
        )
        return 2

    server = build_server(Governor(load_policy(args.policy)), name=args.name)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
