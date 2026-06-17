"""MCP-style tool surface for the Plimsoll runtime :class:`~plimsoll.governor.Governor`.

This exposes two tools a live agent (or an MCP host) can call:

  * ``propose_tool_call(partial_trace, proposed_call)`` — the pre-execution GATE: decide
    whether the next tool call is allowed, with the rule that fired.
  * ``check_trace(trace)`` — the full post-hoc audit, mirroring the CLI (``evaluate_trace``).

Both handlers take and return plain JSON-able values, so they work with or without the
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

from pathlib import Path
from typing import Any

from plimsoll.governor import Decision, Governor, coerce_partial_trace
from plimsoll.io import load_policy, parse_trace
from plimsoll.models import Finding, JsonObject, Policy, TraceRun

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

    def propose_tool_call(self, partial_trace: Any, proposed_call: Any) -> JsonObject:
        """Gate a proposed tool call before it executes. Returns a serialized Decision."""
        partial = coerce_partial_trace(partial_trace)
        decision: Decision = self.governor.evaluate(partial, proposed_call)
        return decision.to_dict()

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
    """Return plain ``{name: callable}`` handlers — the SDK-free tool surface."""
    tools = GovernorTools(governor)
    return {
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
    def propose_tool_call(partial_trace: Any, proposed_call: Any) -> JsonObject:
        """Decide whether a proposed next tool call is allowed, given the partial trace."""
        return tools.propose_tool_call(partial_trace, proposed_call)

    @server.tool()
    def check_trace(trace: Any, baseline: Any = None) -> JsonObject:
        """Run the full deterministic Plimsoll audit over a completed trace."""
        return tools.check_trace(trace, baseline)

    return server
