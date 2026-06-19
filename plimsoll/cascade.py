"""Cascade telemetry for Plimsoll's one real cheap->expensive boundary, plus the gate
regime / residual-locus registry.

Plimsoll *is* the cheap deterministic substrate, so there is no LLM tier to disagree with —
and we never invent one. The genuine, measurable boundary inside the repo is between two
deterministic tiers of different cost and scope:

* **Cheap tier — the pre-execution gate.** ``governor.replay_through_gate`` decides each tool
  call from only the *decidable-before-it-runs* rule subset (allowlist/forbidden membership,
  ``must_precede`` ordering, cumulative budgets, repeated-action limits), incrementally.
* **Expensive tier — the full post-hoc audit.** ``rules.evaluate_trace`` runs *every* rule
  over the *complete* trace, including the result-dependent ones the gate cannot see before a
  call runs (expected-output match, PII/secret leakage, retry drift, trajectory drift/match).

Both tiers are deterministic, so the cascade is measured purely by replay — **zero extra
model spend**. We compare them at the same severity bar the CI gate uses (a "fail" is a
blocking critical/high finding) so the comparison is apples-to-apples and losslessness is
meaningful.

Telemetry slice (the suite-wide contract shape):

* ``alpha`` — fraction of traces the cheap gate *resolves on its own*: it decisively blocks a
  critical/high violation before the call runs, so no escalation to the full audit is needed
  to know the trace is unsafe. (A clean-at-gate trace is not resolved — result-dependent
  checks still have to run, so it escalates.)
* ``disagreementRate`` — fraction of traces where the cheap and expensive verdicts differ
  (always in the safe direction: the audit sees strictly more rules).
* ``losslessViolations`` — count of traces where the cheap fast path *failed* something the
  authoritative audit would *pass*. The gate's gate-subset findings are a strict subset of the
  audit's, so this is expected to be 0; we still measure it rather than assume it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plimsoll.governor import replay_through_gate
from plimsoll.models import JsonObject, Policy, TraceRun
from plimsoll.rules import evaluate_trace

# A "fail" at either tier is a blocking critical/high finding — the same bar the CLI gate and
# CaseReport.passed use. Matching bars is what makes alpha / disagreement / lossless honest.
BLOCKING_SEVERITIES = frozenset({"critical", "high"})


@dataclass(frozen=True)
class CascadeSlice:
    """One cheap->expensive boundary's telemetry, in the suite-wide shape."""

    boundary: str
    cheap_tier: str
    expensive_tier: str
    n: int
    cheap_resolved: int
    escalated: int
    disagreements: int
    lossless_violations: int

    @property
    def alpha(self) -> float:
        return round(self.cheap_resolved / self.n, 6) if self.n else 0.0

    @property
    def disagreement_rate(self) -> float:
        return round(self.disagreements / self.n, 6) if self.n else 0.0

    def to_dict(self) -> JsonObject:
        return {
            "boundary": self.boundary,
            "cheap_tier": self.cheap_tier,
            "expensive_tier": self.expensive_tier,
            "n": self.n,
            # The suite-wide contract keys (camelCase), measured zero-model-spend.
            "alpha": self.alpha,
            "disagreementRate": self.disagreement_rate,
            "losslessViolations": self.lossless_violations,
            # Supporting counts so the fractions are auditable.
            "cheap_resolved": self.cheap_resolved,
            "escalated": self.escalated,
            "disagreements": self.disagreements,
            "measurement": "deterministic gate replay vs full audit; no LLM, no model spend",
        }

    def headline(self) -> str:
        """The one recruiter-legible measured sentence."""
        return (
            f"the deterministic fast path resolves {self.alpha:.0%} of traces losslessly "
            f"({self.lossless_violations} lossless violation(s)); the expensive audit tier touches the "
            f"remaining {1 - self.alpha:.0%}, at {self.disagreement_rate:.0%} measured disagreement"
        )


def _cheap_fails(trace: TraceRun, policy: Policy) -> bool:
    """True when the cheap gate decisively blocks a critical/high call in the replay."""
    for decision in replay_through_gate(trace, policy):
        if any(finding.severity in BLOCKING_SEVERITIES for finding in decision.blocking_findings):
            return True
    return False


def _expensive_fails(trace: TraceRun, policy: Policy) -> bool:
    """True when the full audit produces a blocking critical/high finding (the authority)."""
    return any(finding.severity in BLOCKING_SEVERITIES for finding in evaluate_trace(trace, policy))


def cascade_telemetry(traces: list[TraceRun], policy: Policy) -> CascadeSlice:
    """Measure the gate->audit cascade over a corpus of completed traces (deterministic only)."""
    n = len(traces)
    cheap_resolved = 0
    disagreements = 0
    lossless_violations = 0
    for trace in traces:
        cheap_fail = _cheap_fails(trace, policy)
        expensive_fail = _expensive_fails(trace, policy)
        if cheap_fail:
            cheap_resolved += 1
        if cheap_fail != expensive_fail:
            disagreements += 1
        if cheap_fail and not expensive_fail:
            lossless_violations += 1
    return CascadeSlice(
        boundary="pre-execution gate -> full post-hoc audit",
        cheap_tier="governor gate (decidable-before-call rule subset, incremental)",
        expensive_tier="full audit (every rule over the complete trace)",
        n=n,
        cheap_resolved=cheap_resolved,
        escalated=n - cheap_resolved,
        disagreements=disagreements,
        lossless_violations=lossless_violations,
    )


# --------------------------------------------------------------------------------------
# Gate regime + residual-locus registry.
#
# Each gate is labelled by its REGIME — deliberately model-free/provable (a deterministic
# count or membership test with no distributional assumption) vs. model-based residual (a
# statistical model whose residual is what it flags) — and the residual LOCUS it points at,
# drawn from {turn, claim, action, step, chunk}.
# --------------------------------------------------------------------------------------

_PROVABLE = "model-free / provable"
_MODEL_BASED = "model-based residual"

_GATE_REGIMES: tuple[dict[str, str], ...] = (
    {
        "gate": "tool_allowlist / forbidden_tool",
        "regime": _PROVABLE,
        "locus": "action",
        "note": "set membership on a tool call; no model",
    },
    {
        "gate": "required_tool",
        "regime": _PROVABLE,
        "locus": "step",
        "note": "completion check over the step sequence",
    },
    {
        "gate": "tool_order (must_precede)",
        "regime": _PROVABLE,
        "locus": "step",
        "note": "ordering predicate over step positions",
    },
    {
        "gate": "budgets (steps/duration/tokens/cost)",
        "regime": _PROVABLE,
        "locus": "step",
        "note": "arithmetic threshold over accumulated steps",
    },
    {
        "gate": "repeated_action",
        "regime": _PROVABLE,
        "locus": "action",
        "note": "exact-duplicate count on a tool call",
    },
    {
        "gate": "retry_drift",
        "regime": _PROVABLE,
        "locus": "action",
        "note": "input-equality check on a retried call",
    },
    {
        "gate": "pii_leak / secret_leak",
        "regime": _PROVABLE,
        "locus": "chunk",
        "note": "regex match over text chunks (provable given the patterns)",
    },
    {
        "gate": "expected_output",
        "regime": _PROVABLE,
        "locus": "claim",
        "note": "exact/contains match on the final answer claim",
    },
    {
        "gate": "trajectory_drift / trajectory_match",
        "regime": _PROVABLE,
        "locus": "step",
        "note": "edit distance / set relation over the step sequence",
    },
    {
        "gate": "reliability_pass_k (combinatorial)",
        "regime": _PROVABLE,
        "locus": "turn",
        "note": "unbiased count-based pass^k; assumes no distribution",
    },
    {
        "gate": "reliability_curve / reliability_sla (Wilson p^k)",
        "regime": _MODEL_BASED,
        "locus": "turn",
        "note": "i.i.d. Bernoulli(p) per-run model; residual = per-run flakiness",
    },
)


def gate_regimes() -> list[dict[str, str]]:
    """Return the regime + residual-locus label for every Plimsoll gate (copy, not the source)."""
    return [dict(entry) for entry in _GATE_REGIMES]


def cascade_to_dict(slice_: CascadeSlice) -> dict[str, Any]:
    """The full ``cascade`` block for report.json: the slice, its headline, and the regimes."""
    return {
        "boundaries": [slice_.to_dict()],
        "measured_sentence": slice_.headline(),
        "gate_regimes": gate_regimes(),
    }
