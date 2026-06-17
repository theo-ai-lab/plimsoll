"""Runtime governor: the Plimsoll rule engine, run BEFORE a tool executes.

Plimsoll's identity is a deterministic, offline, zero-dependency *post-hoc* trace
checker (see ``cli.py`` and ``rules.py``). This module is purely additive: it reuses
the very same rule functions to answer a *live* question instead of an after-the-fact
one —

    "Given the partial trace so far, is it safe to run this proposed next tool call?"

Nothing here calls an LLM, opens a socket, or imports a third-party package; it is the
same pure-stdlib, deterministic engine, just evaluated at the gate rather than at the
end. The CLI, the policy schema, and ``rules.py`` are untouched.

Gate semantics
--------------
A pre-execution decision can only honour the rules that are *decidable before the call
runs* — the membership, ordering, budget and repetition rules. Rules that need the
call's result (expected-output match, PII/secret leakage, retry drift) or the whole
trajectory (baseline distance, trajectory match) or the finished run (required-tool
completion) are intentionally NOT evaluated at the gate; run :meth:`Governor.check_trace`
(a thin wrapper over ``rules.evaluate_trace``) once the run completes for those.

Unlike the CLI's pass/fail — which only *fails* on critical/high findings — the gate is
deliberately preventive: it BLOCKS on any rule in its subset that fires for the proposed
call, regardless of that rule's severity (an over-budget loop is "medium" but you still
want to stop it before it runs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plimsoll.io import load_policy, parse_span
from plimsoll.models import Finding, JsonObject, Policy, Span, TraceRun, ValidationError
from plimsoll.rules import (
    check_budgets,
    check_repeated_actions,
    check_tool_order,
    check_tool_policy,
    evaluate_trace,
)

# Rules from check_tool_policy that are a pure membership test on the proposed tool and
# therefore decidable at the gate. (required_tool is a *completion* check — a required
# tool is legitimately absent mid-run — so it is not a gate rule.)
_MEMBERSHIP_GATE_RULES = {"forbidden_tool", "tool_allowlist"}

# Keys that mark a fully-specified span (native/OTel shape) vs. a shorthand call dict.
_FULL_SPAN_KEYS = {"span_id", "name", "kind", "status", "start_ms", "end_ms"}


@dataclass(frozen=True)
class ProposedToolCall:
    """A tool call an agent is *about* to make, described before it executes.

    Only ``tool`` is required. The cost hints (tokens/cost/duration) let the budget
    rules account for the call's marginal contribution; omit them and they count as zero.
    """

    tool: str
    input: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: int = 0
    status: str = "ok"

    @classmethod
    def from_obj(cls, obj: ProposedToolCall | dict[str, Any] | str) -> ProposedToolCall:
        """Coerce a ProposedToolCall, a plain str (tool name), or a JSON object."""
        if isinstance(obj, cls):
            return obj
        if isinstance(obj, str):
            return cls(tool=obj)
        if isinstance(obj, dict):
            tool = obj.get("tool") or obj.get("tool_name") or obj.get("name")
            if not isinstance(tool, str) or not tool:
                raise ValidationError("proposed tool call requires a non-empty 'tool' name")
            return cls(
                tool=tool,
                input=obj.get("input"),
                input_tokens=int(obj.get("input_tokens", 0) or 0),
                output_tokens=int(obj.get("output_tokens", 0) or 0),
                estimated_cost_usd=float(obj.get("estimated_cost_usd", 0.0) or 0.0),
                duration_ms=int(obj.get("duration_ms", 0) or 0),
                status=str(obj.get("status", "ok") or "ok"),
            )
        raise ValidationError(f"cannot read a proposed tool call from {type(obj).__name__}")


@dataclass(frozen=True)
class Decision:
    """The outcome of gating one proposed tool call.

    ``allowed`` is True only when no gate rule fired. ``blocking_findings`` are real
    ``Finding`` objects produced by ``rules.py`` (same shape the CLI reports), so the
    caller gets the exact rule, severity and evidence that blocked the call.
    """

    proposed_tool: str
    blocking_findings: list[Finding] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.blocking_findings

    @property
    def rule_ids(self) -> list[str]:
        return [finding.rule_id for finding in self.blocking_findings]

    @property
    def summary(self) -> str:
        if self.allowed:
            return f"allow: no governor rule blocked '{self.proposed_tool}'"
        return f"block: '{self.proposed_tool}' blocked by {', '.join(self.rule_ids)}"

    def to_dict(self) -> JsonObject:
        return {
            "decision": "allow" if self.allowed else "block",
            "allowed": self.allowed,
            "proposed_tool": self.proposed_tool,
            "summary": self.summary,
            "blocking_findings": [
                {
                    "rule_id": finding.rule_id,
                    "severity": finding.severity,
                    "message": finding.message,
                    "evidence": finding.evidence,
                }
                for finding in self.blocking_findings
            ],
        }


class Governor:
    """Evaluate a partial trace + a proposed next tool call against a :class:`Policy`."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    @classmethod
    def from_policy_file(cls, path: str | Path) -> Governor:
        """Build a Governor from a policy JSON file (reuses ``io.load_policy``)."""
        return cls(load_policy(Path(path)))

    def evaluate(self, partial_trace: TraceRun | list[Any], proposed_call: Any) -> Decision:
        """Decide whether ``proposed_call`` may run, given the partial trace so far.

        The proposed call is appended as a hypothetical span and the gate subset of
        ``rules.py`` is run over the result. Each finding is attributed to the proposed
        call so historical violations already in the partial trace never block a call
        that is itself safe right now.
        """
        partial = partial_trace if isinstance(partial_trace, TraceRun) else self.build_partial_trace(partial_trace)
        proposed = ProposedToolCall.from_obj(proposed_call)
        prior_sequence = partial.tool_sequence

        last_end = max((span.end_ms for span in partial.spans), default=0)
        proposed_span = _span_from_call(proposed, span_id=f"proposed-{len(partial.spans)}", start_ms=last_end)
        hypothetical = TraceRun(
            run_id=partial.run_id,
            case_id=partial.case_id,
            final_output=partial.final_output,
            expected_output=partial.expected_output,
            spans=[*partial.spans, proposed_span],
            metadata=partial.metadata,
        )

        blocking: list[Finding] = []

        # 1) Allowlist + forbidden — a membership test on the proposed tool alone, so we
        #    evaluate a single-span trace of just the proposal (a historical forbidden
        #    call must not block a different, allowed proposal).
        proposed_only = TraceRun(
            run_id=partial.run_id,
            case_id=partial.case_id,
            final_output="",
            expected_output=None,
            spans=[proposed_span],
        )
        blocking.extend(
            finding
            for finding in check_tool_policy(proposed_only, self.policy)
            if finding.rule_id in _MEMBERSHIP_GATE_RULES
        )

        # 2) Required ordering (must_precede). Attribute to the proposed call: the
        #    proposed tool is the gated 'after' and its required 'before' has not yet
        #    happened in the prior sequence. (Mirrors check_tool_order's own predicate.)
        for finding in check_tool_order(hypothetical, self.policy):
            if finding.evidence.get("after") == proposed.tool and finding.evidence.get("before") not in prior_sequence:
                blocking.append(finding)

        # 3) Budgets — the gate keeps *cumulative* usage within the cap, so any budget
        #    the hypothetical (partial + proposed) exceeds blocks the call.
        blocking.extend(check_budgets(hypothetical, self.policy))

        # 4) Repeated identical actions — block only when the proposed call's own
        #    signature is what crosses the repeat limit.
        proposed_signature = proposed_span.action_signature
        for finding in check_repeated_actions(hypothetical, self.policy):
            if proposed_signature in finding.evidence.get("repeated_actions", {}):
                blocking.append(finding)

        return Decision(proposed_tool=proposed.tool, blocking_findings=blocking)

    def allows(self, partial_trace: TraceRun | list[Any], proposed_call: Any) -> bool:
        """Convenience boolean wrapper around :meth:`evaluate`."""
        return self.evaluate(partial_trace, proposed_call).allowed

    def check_trace(self, trace: TraceRun, baseline: TraceRun | None = None) -> list[Finding]:
        """Full post-hoc audit of a completed trace (delegates to ``rules.evaluate_trace``)."""
        return evaluate_trace(trace, self.policy, baseline)

    @staticmethod
    def build_partial_trace(
        prior_calls: list[Any],
        *,
        run_id: str = "partial",
        case_id: str = "partial",
        final_output: str = "",
    ) -> TraceRun:
        """Build a partial :class:`TraceRun` from an ordered list of prior tool calls.

        Each item may be a :class:`ProposedToolCall`, a tool-name str, or a JSON object
        (see :meth:`ProposedToolCall.from_obj`). An empty list yields a valid zero-span
        partial trace — the legitimate "nothing has run yet" starting state.
        """
        spans: list[Span] = []
        cursor = 0
        for index, raw in enumerate(prior_calls):
            call = ProposedToolCall.from_obj(raw)
            span = _span_from_call(call, span_id=f"prior-{index}", start_ms=cursor)
            spans.append(span)
            cursor = span.end_ms
        return TraceRun(
            run_id=run_id,
            case_id=case_id,
            final_output=final_output,
            expected_output=None,
            spans=spans,
        )


def coerce_partial_trace(payload: Any) -> TraceRun:
    """Build a partial :class:`TraceRun` from loosely-typed input (for the MCP surface).

    Accepts an existing ``TraceRun``, ``None`` / empty (the start state), a list of prior
    calls, or a trace-shaped dict whose ``spans`` may be full span objects and/or call
    shorthands. Never goes through ``parse_trace`` so a zero-span partial trace is valid.
    """
    if isinstance(payload, TraceRun):
        return payload
    if payload is None:
        return Governor.build_partial_trace([])
    if isinstance(payload, list):
        return Governor.build_partial_trace(payload)
    if isinstance(payload, dict):
        spans_data = payload.get("spans")
        if spans_data is None:
            return Governor.build_partial_trace([])
        if not isinstance(spans_data, list):
            raise ValidationError("partial_trace 'spans' must be a list")
        spans: list[Span] = []
        cursor = 0
        for index, item in enumerate(spans_data):
            if isinstance(item, dict) and _FULL_SPAN_KEYS <= set(item):
                span = parse_span(item, source="<partial_trace>", index=index)
            else:
                span = _span_from_call(ProposedToolCall.from_obj(item), span_id=f"prior-{index}", start_ms=cursor)
            spans.append(span)
            cursor = max(cursor, span.end_ms)
        return TraceRun(
            run_id=str(payload.get("run_id", "partial")),
            case_id=str(payload.get("case_id", "partial")),
            final_output=str(payload.get("final_output", "")),
            expected_output=payload.get("expected_output"),
            spans=sorted(spans, key=lambda span: (span.start_ms, span.end_ms, span.span_id)),
            metadata=payload.get("metadata") or {},
        )
    raise ValidationError(f"cannot read a partial trace from {type(payload).__name__}")


def _span_from_call(call: ProposedToolCall, *, span_id: str, start_ms: int) -> Span:
    """Synthesize a tool span from a (proposed or prior) call so the rule engine can read it."""
    end_ms = start_ms + max(0, call.duration_ms)
    return Span(
        span_id=span_id,
        name=call.tool,
        kind="tool",
        status=call.status,
        start_ms=start_ms,
        end_ms=end_ms,
        tool_name=call.tool,
        input=call.input,
        attributes={
            "gen_ai.usage.input_tokens": call.input_tokens,
            "gen_ai.usage.output_tokens": call.output_tokens,
            "estimated_cost_usd": call.estimated_cost_usd,
        },
    )
