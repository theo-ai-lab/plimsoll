"""Unit tests for the cheap->expensive cascade telemetry and the gate-regime registry.

Plimsoll is the cheap deterministic substrate, so its one honest cascade boundary is
internal: the pre-execution gate (decidable-before-a-call rule subset, incremental) vs. the
full post-hoc audit (every rule over the complete trace). Both tiers are deterministic, so
the telemetry is measured purely by replay — zero model spend. These tests pin the contract
shape ({alpha, disagreementRate, losslessViolations}) and the two defining invariants:

* the gate's findings are a strict subset of the audit's, so ``losslessViolations`` is 0 and
  every disagreement is in the safe direction (the audit catches strictly more);
* a result-dependent failure (e.g. an expected-output mismatch) is invisible to the gate and
  only the audit blocks it — that is an escalation, not a lossless fast-path verdict.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from plimsoll.cascade import (
    BLOCKING_SEVERITIES,
    cascade_telemetry,
    cascade_to_dict,
    gate_regimes,
)
from plimsoll.io import load_policy, load_traces
from plimsoll.models import Policy, Span, TraceRun

ALLOWED_LOCI = {"turn", "claim", "action", "step", "chunk"}
ALLOWED_REGIMES = {"model-free / provable", "model-based residual"}


def _tool_span(span_id: str, tool: str, start_ms: int) -> Span:
    return Span(
        span_id=span_id,
        name=tool,
        kind="tool",
        status="ok",
        start_ms=start_ms,
        end_ms=start_ms + 5,
        tool_name=tool,
    )


def _trace(run_id: str, tools: list[str], *, final: str = "done", expected: str | None = None) -> TraceRun:
    spans = [_tool_span(f"{run_id}-s{i}", tool, i * 10) for i, tool in enumerate(tools)]
    return TraceRun(
        run_id=run_id,
        case_id="case",
        final_output=final,
        expected_output=expected,
        spans=spans,
    )


class CascadeTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        # A policy with one gate-decidable blocker (forbidden tool, critical) and one
        # result-dependent blocker the gate cannot see (expected-output mismatch, high).
        self.policy = Policy(
            forbidden_tools={"delete_database"},
            must_precede=[("manager_review", "grant_access")],
            expected_output_mode="contains",
        )

    def test_clean_corpus_resolves_nothing_and_disagrees_nowhere(self) -> None:
        traces = [_trace("r1", ["search"]), _trace("r2", ["search", "summarize"])]
        slice_ = cascade_telemetry(traces, self.policy)
        self.assertEqual(slice_.n, 2)
        self.assertEqual(slice_.cheap_resolved, 0)
        self.assertEqual(slice_.escalated, 2)
        self.assertEqual(slice_.alpha, 0.0)
        self.assertEqual(slice_.disagreement_rate, 0.0)
        self.assertEqual(slice_.lossless_violations, 0)

    def test_gate_decidable_block_is_resolved_by_the_cheap_tier(self) -> None:
        # A forbidden-tool call is critical AND gate-decidable: the cheap tier blocks it, so
        # the trace is resolved without escalating, and the audit agrees (no disagreement).
        traces = [_trace("bad", ["delete_database"]), _trace("ok", ["search"])]
        slice_ = cascade_telemetry(traces, self.policy)
        self.assertEqual(slice_.cheap_resolved, 1)
        self.assertEqual(slice_.escalated, 1)
        self.assertEqual(slice_.alpha, 0.5)
        self.assertEqual(slice_.disagreements, 0)
        self.assertEqual(slice_.lossless_violations, 0)

    def test_result_dependent_failure_escalates_and_disagrees_safely(self) -> None:
        # An expected-output mismatch is high severity but NOT decidable before the call runs,
        # so the gate passes it and only the audit blocks: a safe-direction disagreement with
        # zero lossless violations (the cheap tier never produced a verdict the audit reverses).
        traces = [_trace("miss", ["search"], final="wrong", expected="the right answer")]
        slice_ = cascade_telemetry(traces, self.policy)
        self.assertEqual(slice_.cheap_resolved, 0)
        self.assertEqual(slice_.disagreements, 1)
        self.assertEqual(slice_.disagreement_rate, 1.0)
        self.assertEqual(slice_.lossless_violations, 0)

    def test_lossless_invariant_holds_on_a_mixed_corpus(self) -> None:
        traces = [
            _trace("clean", ["search"]),
            _trace("forbidden", ["delete_database"]),
            _trace("order_bypass", ["grant_access"]),  # gate-decidable tool_order, critical
            _trace("miss", ["search"], final="nope", expected="expected text"),
        ]
        slice_ = cascade_telemetry(traces, self.policy)
        # The gate subset is a strict subset of the audit, so the cheap tier can never fail
        # something the authoritative audit would pass.
        self.assertEqual(slice_.lossless_violations, 0)
        # Two gate-decidable criticals are resolved cheaply; the mismatch escalates.
        self.assertEqual(slice_.cheap_resolved, 2)
        self.assertEqual(slice_.escalated, 2)

    def test_to_dict_carries_the_suite_contract_keys(self) -> None:
        slice_ = cascade_telemetry([_trace("r1", ["search"])], self.policy)
        payload = slice_.to_dict()
        for key in ("alpha", "disagreementRate", "losslessViolations"):
            self.assertIn(key, payload)
        # Supporting counts so the camelCase fractions are auditable.
        for key in ("n", "cheap_resolved", "escalated", "disagreements", "boundary"):
            self.assertIn(key, payload)
        self.assertIn("no LLM", payload["measurement"])

    def test_headline_is_one_measured_sentence(self) -> None:
        slice_ = cascade_telemetry(
            [_trace("bad", ["delete_database"]), _trace("ok", ["search"])],
            self.policy,
        )
        headline = slice_.headline()
        self.assertIn("%", headline)
        self.assertIn("lossless", headline)
        self.assertIn("disagreement", headline)

    def test_deterministic(self) -> None:
        traces = [_trace("bad", ["delete_database"]), _trace("ok", ["search"])]
        self.assertEqual(
            cascade_telemetry(traces, self.policy).to_dict(),
            cascade_telemetry(traces, self.policy).to_dict(),
        )

    def test_empty_corpus_is_well_defined(self) -> None:
        slice_ = cascade_telemetry([], self.policy)
        self.assertEqual(slice_.n, 0)
        self.assertEqual(slice_.alpha, 0.0)
        self.assertEqual(slice_.disagreement_rate, 0.0)


class CommittedCorpusMeasurementTests(unittest.TestCase):
    """Lock the README's MEASURED cascade sentence to the committed access-request corpus.

    The README claims the deterministic gate tier resolves 33% (1/3) of these traces with 0
    lossless violations and 0% disagreement. Pinning it here means the published number cannot
    silently rot: if the corpus or the gate/audit subset relation changes, this fails.
    """

    def test_access_request_corpus_matches_the_readme_number(self) -> None:
        root = Path("examples/access-request")
        policy = load_policy(root / "policies" / "access-control-policy.json")
        traces = load_traces(root / "traces")
        slice_ = cascade_telemetry(traces, policy)
        self.assertEqual(slice_.n, 3)
        self.assertEqual(slice_.cheap_resolved, 1)
        self.assertEqual(slice_.alpha, round(1 / 3, 6))
        self.assertEqual(slice_.disagreement_rate, 0.0)
        self.assertEqual(slice_.lossless_violations, 0)


class GateRegimeRegistryTests(unittest.TestCase):
    def test_blocking_bar_matches_the_ci_gate(self) -> None:
        self.assertEqual(BLOCKING_SEVERITIES, frozenset({"critical", "high"}))

    def test_every_gate_is_labelled_with_a_valid_regime_and_locus(self) -> None:
        regimes = gate_regimes()
        self.assertGreater(len(regimes), 0)
        for entry in regimes:
            self.assertEqual(set(entry), {"gate", "regime", "locus", "note"})
            self.assertIn(entry["regime"], ALLOWED_REGIMES)
            self.assertIn(entry["locus"], ALLOWED_LOCI)

    def test_only_the_wilson_curve_is_model_based(self) -> None:
        # The deterministic gates are deliberately model-free/provable; only the parametric
        # Wilson p^k reliability curve is a model-based residual. This labels the regime split
        # honestly rather than letting a provable gate masquerade as statistical (or vice versa).
        model_based = [entry["gate"] for entry in gate_regimes() if entry["regime"] == "model-based residual"]
        self.assertEqual(len(model_based), 1)
        self.assertIn("Wilson", model_based[0])

    def test_returns_a_defensive_copy(self) -> None:
        first = gate_regimes()
        first[0]["regime"] = "tampered"
        self.assertNotEqual(gate_regimes()[0]["regime"], "tampered")

    def test_cascade_to_dict_bundles_slice_sentence_and_regimes(self) -> None:
        slice_ = cascade_telemetry([_trace("r1", ["search"])], Policy())
        block = cascade_to_dict(slice_)
        self.assertEqual(set(block), {"boundaries", "measured_sentence", "gate_regimes"})
        self.assertEqual(len(block["boundaries"]), 1)
        self.assertEqual(block["measured_sentence"], slice_.headline())
        self.assertEqual(block["gate_regimes"], gate_regimes())


if __name__ == "__main__":
    unittest.main()
