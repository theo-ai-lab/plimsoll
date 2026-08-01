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

Who owns "what has already run"
-------------------------------
Every ordering and budget verdict is a function of the calls that came before. At runtime
that record is kept by the governor itself, in a :class:`GovernorSession` opened through
:meth:`Governor.open_session`: the session appends a call only when the gate ALLOWED it,
and history a caller supplies is never used as the record — at most it is cross-checked
against the governor's own, and a mismatch fails closed. An agent therefore cannot widen
its own permissions by describing a past that did not happen.

The stateless :meth:`Governor.evaluate` remains, but it is an *offline* evaluator: it
answers "what would the gate say given this history?" and is used by the whole-plan
dry-run, by gate replay over a finished trace, and by the cascade telemetry — contexts
where the history comes from a plan or a recorded trace, not from the agent being gated.
It is not the runtime front door.

What the governor's record does and does not prove: it is the list of calls the governor
AUTHORIZED, which is what the gate can know. It cannot observe whether the host actually
executed an authorized call — that is what the post-hoc :meth:`Governor.check_trace` audit
over the real trace is for. The two tiers together are the cascade; neither replaces the
other.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from plimsoll.io import load_policy, parse_span
from plimsoll.models import Finding, JsonObject, Policy, Span, TraceRun, ValidationError, stable_repr
from plimsoll.policy import policy_digest
from plimsoll.rules import (
    check_budgets,
    check_repeated_actions,
    check_tool_order,
    check_tool_policy,
    evaluate_trace,
)

# Gate-integrity findings. These are NOT policy rules — they never come from a policy file
# and never appear in an audit report; they are how the runtime gate refuses a call whose
# session context it cannot trust. Both fail closed.
SESSION_UNKNOWN_RULE = "session_unknown"
SESSION_HISTORY_MISMATCH_RULE = "session_history_mismatch"
SESSION_RULES = frozenset({SESSION_UNKNOWN_RULE, SESSION_HISTORY_MISMATCH_RULE})

# Rules from check_tool_policy that are a pure membership test on the proposed tool and
# therefore decidable at the gate. (required_tool is a *completion* check — a required
# tool is legitimately absent mid-run — so it is not a gate rule.)
_MEMBERSHIP_GATE_RULES = {"forbidden_tool", "tool_allowlist"}

# A human noun for each budget metric, for the gate's per-call copy ("would exceed the
# token budget") — internal metric names like duration_ms never reach users. Keyed by the
# audit's own budget vocabulary (rules.BUDGET_RULES); a test pins the two sets, so a budget
# rule added in rules.py cannot silently keep the audit's finished-trace copy at the gate.
_BUDGET_NOUNS = {
    "max_steps": "step",
    "max_duration_ms": "duration",
    "max_tokens": "token",
    "max_estimated_cost_usd": "estimated-cost",
}

# Keys that mark a fully-specified span (native/OTel shape) vs. a shorthand call dict.
_FULL_SPAN_KEYS = {"span_id", "name", "kind", "status", "start_ms", "end_ms"}


def _call_phrased(finding: Finding, tool: str) -> Finding:
    """Rephrase an audit finding for the single call the gate is deciding.

    ``rules.py`` speaks about a finished trace ("Trace used forbidden tools.") because
    that is what the post-hoc audit sees. The gate is deciding one proposed call before
    it runs, so its copy names that call instead. Only the message changes — the rule id,
    severity, and evidence stay exactly what the rule engine produced. ``tool_order``
    already names the proposed call, so it (and anything unrecognized) passes through.
    """
    if finding.rule_id == "tool_allowlist":
        return replace(finding, message=f"'{tool}' is not in the allowlist.")
    if finding.rule_id == "forbidden_tool":
        return replace(finding, message=f"'{tool}' is forbidden by policy.")
    if finding.rule_id in _BUDGET_NOUNS:
        overrun = f"({finding.evidence['actual']} > {finding.evidence['limit']})"
        return replace(finding, message=f"'{tool}' would exceed the {_BUDGET_NOUNS[finding.rule_id]} budget {overrun}.")
    if finding.rule_id == "repeated_action":
        return replace(finding, message=f"'{tool}' would repeat an identical action more than the policy allows.")
    return finding


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
    caller gets the exact rule, severity and evidence that blocked the call. Their
    messages are phrased for the proposed call ("'deploy' is forbidden by policy.")
    rather than the audit's finished-trace voice.

    ``policy_digest`` is the SHA-256 of the effective policy text (see
    ``policy.policy_digest``), so a decision binds to the exact policy content that
    produced it; ``session_id`` names the governor-owned session it was decided in, or is
    None for an offline evaluation that has no session.
    """

    proposed_tool: str
    blocking_findings: list[Finding] = field(default_factory=list)
    policy_digest: str = ""
    session_id: str | None = None

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
            "policy_digest": self.policy_digest,
            "session_id": self.session_id,
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


@dataclass(frozen=True)
class PlanFeasibility:
    """Stage-1 deterministic feasibility + score for a WHOLE proposed plan (a candidate trajectory).

    This is the *free, exact pruner* of the deterministic-first MPC contract: Plimsoll's
    whole-plan policy DRY-RUN. Every step of the plan is gated against the steps before it,
    WITHOUT executing a single tool or spending a token. Within the gate's decidable rule
    subset it is *exact* (no false negatives: a plan it calls feasible provably violates none
    of those rules), so a planner can prune infeasible candidate trajectories before paying an
    expensive model to score them.

    The result-dependent rules (output match, leakage, retry/trajectory drift) are NOT
    decidable on an un-executed plan and stay deferred to the post-hoc audit — exactly the
    governor/audit cascade boundary.
    """

    plan_length: int
    decisions: list[Decision] = field(default_factory=list)
    blocking_step: int | None = None

    @property
    def feasible(self) -> bool:
        return self.blocking_step is None

    @property
    def blocking_findings(self) -> list[Finding]:
        if self.blocking_step is None:
            return []
        return self.decisions[self.blocking_step].blocking_findings

    @property
    def score(self) -> int:
        """A deterministic 0-100 pruning signal: 100 if the whole plan clears the gate, else
        how far it got before the first block. A cheap ordering heuristic for MPC pruning —
        deliberately NOT a calibrated success probability."""
        if self.plan_length == 0 or self.feasible:
            return 100
        assert self.blocking_step is not None
        return round(100 * self.blocking_step / self.plan_length)

    @property
    def summary(self) -> str:
        if self.feasible:
            return f"feasible: all {self.plan_length} planned step(s) clear the gate (score {self.score})"
        tool = self.decisions[self.blocking_step].proposed_tool
        rules = ", ".join(self.decisions[self.blocking_step].rule_ids)
        return f"infeasible: step {self.blocking_step} ('{tool}') blocked by {rules} (score {self.score})"

    def to_dict(self) -> JsonObject:
        return {
            "feasible": self.feasible,
            "score": self.score,
            "plan_length": self.plan_length,
            "blocking_step": self.blocking_step,
            "summary": self.summary,
            "steps": [{"step": index, **decision.to_dict()} for index, decision in enumerate(self.decisions)],
        }


class Governor:
    """Evaluate a partial trace + a proposed next tool call against a :class:`Policy`.

    For runtime gating, open a :class:`GovernorSession` (:meth:`open_session`) and propose
    calls through it: the governor then owns the record of what it allowed. :meth:`evaluate`
    is the offline evaluator over a history supplied by the caller — correct for plans and
    recorded traces, never for gating the agent that authored the history.
    """

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.policy_digest = policy_digest(policy)
        # Sessions this governor opened, by handle. The governor owns them; nothing a
        # caller sends can add to or reorder a session's record.
        self._sessions: dict[str, GovernorSession] = {}
        self._session_counter = 0
        # Gate decisions mutate session state, so serialize them: two proposals racing on
        # one governor must not both be measured against the same pre-state.
        self._lock = threading.RLock()

    @classmethod
    def from_policy_file(cls, path: str | Path) -> Governor:
        """Build a Governor from a policy JSON file (reuses ``io.load_policy``)."""
        return cls(load_policy(Path(path)))

    def open_session(self, *, run_id: str = "session", case_id: str = "session") -> GovernorSession:
        """Open a gate session whose call record this governor owns.

        Handles are sequential and process-local (``session-1``, ``session-2``, …), which
        keeps a served session reproducible byte-for-byte — Plimsoll is deterministic by
        construction and a random handle would break that. A handle is a NAME, not a bearer
        secret: isolation between agents is the transport's job (the stdio server the
        ``plimsoll-governor`` console script runs is one process per client).
        """
        with self._lock:
            self._session_counter += 1
            session = GovernorSession(
                self, session_id=f"session-{self._session_counter}", run_id=run_id, case_id=case_id
            )
            self._sessions[session.session_id] = session
            return session

    def session(self, session_id: Any) -> GovernorSession | None:
        """The open session with this handle, or None. Anything unrecognized is None."""
        if not isinstance(session_id, str):
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def evaluate(self, partial_trace: TraceRun | list[Any], proposed_call: Any) -> Decision:
        """Decide whether ``proposed_call`` may run, given the partial trace so far.

        OFFLINE evaluator: the history comes from the caller. Use it for whole-plan
        dry-runs, gate replay over a finished trace, and cascade telemetry. To gate a live
        agent, use :meth:`open_session` — a history the gated agent supplies about itself
        is not evidence.

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

        # The audit rules speak about a finished trace; the gate is deciding one call.
        blocking = [_call_phrased(finding, proposed.tool) for finding in blocking]
        return Decision(proposed_tool=proposed.tool, blocking_findings=blocking, policy_digest=self.policy_digest)

    def allows(self, partial_trace: TraceRun | list[Any], proposed_call: Any) -> bool:
        """Convenience boolean wrapper around :meth:`evaluate`."""
        return self.evaluate(partial_trace, proposed_call).allowed

    def check_trace(self, trace: TraceRun, baseline: TraceRun | None = None) -> list[Finding]:
        """Full post-hoc audit of a completed trace (delegates to ``rules.evaluate_trace``)."""
        return evaluate_trace(trace, self.policy, baseline)

    def dry_run_plan(self, plan: list[Any]) -> PlanFeasibility:
        """Dry-run a WHOLE proposed plan against the policy without executing anything.

        ``plan`` is an ordered list of proposed calls (``ProposedToolCall``, tool-name str,
        or JSON object). Each step is gated against the steps before it; the result reports
        every per-step decision, the first blocking step (if any), and a deterministic
        feasibility score. A planner can use this to prune infeasible plans before any tool
        executes or a token is spent; within the gate's decidable rule subset the check is
        exact.
        """
        calls = [ProposedToolCall.from_obj(item) for item in plan]
        decisions: list[Decision] = []
        blocking_step: int | None = None
        for index, call in enumerate(calls):
            decision = self.evaluate(calls[:index], call)
            decisions.append(decision)
            if blocking_step is None and not decision.allowed:
                blocking_step = index
        return PlanFeasibility(plan_length=len(calls), decisions=decisions, blocking_step=blocking_step)

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


class GovernorSession:
    """A live gate session. The governor — not the caller — owns what has already run.

    Open one with :meth:`Governor.open_session`, then gate every proposed call through
    :meth:`propose`. A call is appended to the session's record only when the gate ALLOWED
    it, so the ordering, budget and repetition rules are decided against authorizations
    this governor issued, not against a history the gated agent narrated.

    A caller MAY still send its own view of the history (``client_history``) — hosts often
    keep one — but it is never used as the record. It is compared against the governor's,
    and any disagreement fails closed with a ``session_history_mismatch`` block rather than
    being reconciled: a gate that resolves a conflict in the caller's favour is the hole
    this class exists to close.

    Scope of the guarantee: the record is the list of calls the governor AUTHORIZED. A gate
    cannot observe whether the host really executed one; the post-hoc
    :meth:`Governor.check_trace` audit over the real trace is the tier that can.
    """

    def __init__(self, governor: Governor, *, session_id: str, run_id: str = "session", case_id: str = "session"):
        self.governor = governor
        self.session_id = session_id
        self.run_id = run_id
        self.case_id = case_id
        self._authorized: list[ProposedToolCall] = []

    @property
    def authorized_calls(self) -> tuple[ProposedToolCall, ...]:
        """The calls this governor allowed, in order. Read-only by construction."""
        return tuple(self._authorized)

    @property
    def tool_sequence(self) -> list[str]:
        return [call.tool for call in self._authorized]

    def trace(self) -> TraceRun:
        """The governor's own partial trace for this session."""
        return Governor.build_partial_trace(list(self._authorized), run_id=self.run_id, case_id=self.case_id)

    def propose(self, proposed_call: Any, *, client_history: Any = None) -> Decision:
        """Gate one call against this session's governor-owned record.

        Records the call only when the decision is allow. When ``client_history`` is not
        None it must agree with the record — same tools, same inputs, same order — or the
        call is blocked without ever reaching the policy rules.
        """
        proposed = ProposedToolCall.from_obj(proposed_call)
        with self.governor._lock:
            if client_history is not None:
                mismatch = self._history_mismatch(proposed, client_history)
                if mismatch is not None:
                    return self._decision(proposed.tool, [mismatch])
            decision = self.governor.evaluate(self.trace(), proposed)
            if decision.allowed:
                self._authorized.append(proposed)
            return replace(decision, session_id=self.session_id)

    def _decision(self, tool: str, findings: list[Finding]) -> Decision:
        return Decision(
            proposed_tool=tool,
            blocking_findings=findings,
            policy_digest=self.governor.policy_digest,
            session_id=self.session_id,
        )

    def _history_mismatch(self, proposed: ProposedToolCall, client_history: Any) -> Finding | None:
        """A ``session_history_mismatch`` finding when the supplied history is not ours."""
        try:
            supplied = [ProposedToolCall.from_obj(item) for item in _history_items(client_history)]
        except (ValidationError, TypeError) as exc:
            return self._mismatch_finding(proposed, f"the supplied history could not be read ({exc})", [])
        supplied_signatures = [_call_signature(call) for call in supplied]
        own_signatures = [_call_signature(call) for call in self._authorized]
        if supplied_signatures == own_signatures:
            return None
        return self._mismatch_finding(proposed, "the supplied history is not what this session authorized", supplied)

    def _mismatch_finding(self, proposed: ProposedToolCall, reason: str, supplied: list[ProposedToolCall]) -> Finding:
        return Finding(
            rule_id=SESSION_HISTORY_MISMATCH_RULE,
            severity="critical",
            case_id=self.case_id,
            message=f"'{proposed.tool}' is blocked: {reason}.",
            evidence={
                "session_id": self.session_id,
                "authorized_tools": self.tool_sequence,
                "supplied_tools": [call.tool for call in supplied],
                "reason": reason,
            },
        )


def _history_items(client_history: Any) -> list[Any]:
    """The ordered prior calls of a supplied history, in list or trace-shaped dict form."""
    if isinstance(client_history, list):
        return client_history
    if isinstance(client_history, TraceRun):
        return [_proposed_from_span(span) for span in client_history.spans if span.tool_name]
    if isinstance(client_history, dict):
        spans = client_history.get("spans")
        if spans is None:
            return []
        if not isinstance(spans, list):
            raise ValidationError("supplied history 'spans' must be a list")
        return [
            _proposed_from_span(parse_span(item, source="<client_history>", index=index))
            if isinstance(item, dict) and _FULL_SPAN_KEYS <= set(item)
            else item
            for index, item in enumerate(spans)
        ]
    raise ValidationError(f"cannot read a supplied history from {type(client_history).__name__}")


def _call_signature(call: ProposedToolCall) -> str:
    """What must match between the governor's record and a supplied history.

    Tool and input — the two things the ordering and repetition rules read. Cost hints are
    deliberately excluded: a host reporting a call's *measured* usage where the gate
    accounted an *estimate* is a normal disagreement, not an attempt to rewrite history,
    and the budgets are counted from the governor's own numbers either way.
    """
    return f"{call.tool}:{stable_repr(call.input)}"


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


def replay_through_gate(trace: TraceRun, policy: Policy) -> list[Decision]:
    """Replay a *completed* trace's tool calls through the pre-execution gate, in order.

    For each tool span, the gate is asked whether that call would have been allowed given the
    spans before it. This is how the cheap (gate) tier is measured against the expensive
    (full-audit) tier for the cascade telemetry — purely deterministic, zero model spend. Only
    tool spans are gated; non-tool spans still count toward cumulative budgets via the prefix.
    """
    governor = Governor(policy)
    spans_sorted = sorted(trace.spans, key=lambda span: (span.start_ms, span.end_ms, span.span_id))
    decisions: list[Decision] = []
    for index, span in enumerate(spans_sorted):
        if not span.tool_name:
            continue
        prefix = TraceRun(
            run_id=trace.run_id,
            case_id=trace.case_id,
            final_output="",
            expected_output=None,
            spans=spans_sorted[:index],
        )
        decisions.append(governor.evaluate(prefix, _proposed_from_span(span)))
    return decisions


def _proposed_from_span(span: Span) -> ProposedToolCall:
    """Build a :class:`ProposedToolCall` from a recorded tool span (for gate replay)."""
    attributes = span.attributes or {}
    return ProposedToolCall(
        tool=span.tool_name or span.name,
        input=span.input,
        input_tokens=int(attributes.get("gen_ai.usage.input_tokens", 0) or 0),
        output_tokens=int(attributes.get("gen_ai.usage.output_tokens", 0) or 0),
        estimated_cost_usd=float(attributes.get("estimated_cost_usd", 0.0) or 0.0),
        duration_ms=span.duration_ms,
        status=span.status,
    )


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
