"""Deterministic pass@1..pass^k reliability aggregation over repeated runs.

This is the tau-Bench *reliability* view, computed offline with zero dependencies and no
LLM. You record the SAME task k times and ask: how often does the agent get it right
*every* time, not just once? ``pass^k`` is the fraction of tasks for which **all k runs
pass** — the metric that separates a flaky agent (high pass@1, low pass^k) from a
dependable one.

This module is purely additive. It aggregates the per-run verdicts that ``report.py``
already produces (``CaseReport.passed`` — True when a run has no critical/high finding),
grouping runs by ``case_id``. It never re-evaluates a trace; it only counts existing
results, so the same recorded runs always yield the same curve.

Definitions
-----------
For a task observed with ``n`` runs of which ``c`` passed, the per-task ``pass^j`` is the
probability that ``j`` runs drawn from the ``n`` recorded ones all pass — the unbiased
combinatorial estimator used by tau-Bench::

    pass^j(task) = C(c, j) / C(n, j)        (0 when c < j)

which specialises to the two endpoints people quote most:

* ``pass^1 = c / n`` — the ordinary per-run pass rate (a.k.a. ``pass@1``).
* ``pass^n = 1 if c == n else 0`` — "did every recorded run pass?".

When every task is recorded exactly ``k`` times this reduces to the headline definition:
``pass^k`` is the fraction of tasks where all ``k`` runs pass. The overall ``pass^j`` is
the mean of the per-task values across tasks (macro average, so a task with more recorded
runs does not dominate).

Gate
----
A reliability floor can fail CI: configure a ``threshold`` and the run is gated on
``pass^k >= threshold``. Unlike a single-run gate, this catches *flakiness* — an agent
that usually works but intermittently bypasses a control.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from html import escape
from math import comb
from typing import Any, Protocol

from plimsoll.models import ValidationError
from plimsoll.stats import WilsonInterval, wilson_interval

# The pass^k gate failing is a reliability regression that should block a release, so it
# carries the same weight the CLI's own gate uses for a blocking finding.
PASSK_GATE_SEVERITY = "high"
PASSK_RULE_ID = "reliability_pass_k"
# The SLA band-gate (the honest worst-case gate on the Wilson lower bound) is a distinct
# finding so SARIF/JUnit can tell "the point estimate dipped" apart from "even the
# confidence band cannot certify the reliability SLA".
PASSK_SLA_RULE_ID = "reliability_sla"


class _RunResult(Protocol):
    """The minimal shape this module needs from a recorded run (a ``CaseReport`` fits)."""

    case_id: str
    run_id: str
    passed: bool


def pass_caret(passed: int, runs: int, k: int) -> float:
    """Per-task ``pass^k`` = C(passed, k) / C(runs, k): the chance k of the recorded runs all pass.

    ``passed`` is the number of successful runs (``c``), ``runs`` the number recorded (``n``).
    Returns 0.0 when fewer than ``k`` runs passed. Raises for structurally invalid input.
    """
    if runs <= 0:
        raise ValueError("runs must be a positive integer")
    if not 1 <= k <= runs:
        raise ValueError(f"k={k} must satisfy 1 <= k <= runs ({runs})")
    if not 0 <= passed <= runs:
        raise ValueError(f"passed={passed} must satisfy 0 <= passed <= runs ({runs})")
    if passed < k:
        return 0.0
    return comb(passed, k) / comb(runs, k)


# --------------------------------------------------------------------------------------
# Reliability decay curve (the parametric, MODEL-BASED view).
#
# The combinatorial curve above is *model-free*: it only counts recorded runs and assumes
# nothing about a distribution (its regime is "provable"). The reliability decay curve here
# is the complementary *model-based residual* view: treat each run as an i.i.d. Bernoulli(p)
# draw, estimate the pooled per-run success probability ``p`` with a calibrated Wilson score
# interval, and project ``pass^k = p^k`` as a curve with a confidence BAND ``[p_low^k,
# p_high^k]``. ``pass^k`` itself is the worst-case reliability metric of tau-bench (Yao et
# al., arXiv:2406.12045); reporting it as a decaying curve with uncertainty is the reliability
# framing of "Beyond pass@1: A Reliability Science Framework for Long-Horizon LLM Agents"
# (arXiv:2603.29231).
#
# Because ``x -> x^k`` is monotone on ``[0, 1]``, ``[p_low^k, p_high^k]`` is an *exact*
# 1-alpha confidence interval for ``p^k`` (a monotone transform of a CI is a valid CI). The
# CI gate is armed on the *lower* edge of that band, so a lucky small-n run (``2/2`` gives
# point ``pass^k = 1`` but a Wilson lower bound near 0.34) cannot sneak a flaky agent past.
#
# HONESTY: this is a sample-k decay over a FIXED gold set (the defensible saturation axis),
# NOT an extrapolation of an asymptote over gold-set SIZE. For any per-run ``p < 1`` the
# all-k-pass reliability decays to 0 (there is no positive floor); the governing invariant
# is the per-run reliability ``p`` itself, which is why we surface it with its CI.
# --------------------------------------------------------------------------------------


def _k_star(base: float, sla: float, cap: int) -> int:
    """Largest ``k >= 0`` with ``base**k >= sla``, capped at ``cap``.

    ``base`` is a per-run reliability (point estimate or a Wilson bound). ``0`` means even a
    single attempt misses the SLA at this reliability; ``cap`` means the SLA holds at least
    that deep (reported as a floor, never as "unbounded").
    """
    if sla <= 0.0:
        return cap
    if base <= 0.0:
        return 0
    k = 0
    while k < cap and base ** (k + 1) >= sla - 1e-12:
        k += 1
    return k


@dataclass(frozen=True)
class DurationBucket:
    """Per-run reliability within one task-duration band (the Reliability-Decay-over-duration view).

    Buckets are rank-balanced quantiles of the runs' own ``duration_ms`` (deterministic),
    each with its own Wilson CI. They are purely *descriptive* — ``runs`` is carried so a
    wide CI at small n is visible, and no cross-bucket significance is claimed.
    """

    label: str
    lo_ms: int
    hi_ms: int
    runs: int
    passed: int
    p_hat: float
    ci_low: float
    ci_high: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "duration_ms_range": [self.lo_ms, self.hi_ms],
            "runs": self.runs,
            "passed": self.passed,
            "p_hat": round(self.p_hat, 6),
            "ci": [round(self.ci_low, 6), round(self.ci_high, 6)],
        }


@dataclass(frozen=True)
class ReliabilityCurve:
    """The parametric ``pass^k = p^k`` Reliability Decay Curve with a Wilson confidence band.

    ``wilson`` is the calibrated CI for the pooled per-run success probability. ``band`` is
    the per-k decay with point/lower/upper and a ``projected`` flag (True once ``k`` exceeds
    the depth actually observed, ``observed_k``). ``k_star_*`` is the largest ``k`` that
    still clears the SLA (point vs. honest lower-band); ``meltdown_onset`` is the first ``k``
    where the lower band drops below the SLA (``k_star_lower + 1``).
    """

    wilson: WilsonInterval
    observed_k: int
    horizon: int
    band: list[dict[str, Any]]
    sla: float | None = None
    k_star_point: int | None = None
    k_star_lower: int | None = None
    meltdown_onset: int | None = None
    asymptote: float = 0.0
    duration_buckets: list[DurationBucket] | None = None

    @property
    def p_hat(self) -> float:
        return self.wilson.p_hat

    @property
    def ci_low(self) -> float:
        return self.wilson.low

    @property
    def ci_high(self) -> float:
        return self.wilson.high

    def point_at(self, k: int) -> float:
        return self.p_hat**k

    def lower_at(self, k: int) -> float:
        """The honest worst-case ``pass^k`` (lower edge of the band) the CI gate is armed on."""
        return self.ci_low**k

    def upper_at(self, k: int) -> float:
        return self.ci_high**k

    def to_dict(self) -> dict[str, Any]:
        return {
            # This curve is the MODEL-BASED residual view; the residual it points at is
            # per-run flakiness, located at the whole-run (turn) level.
            "regime": "model-based residual",
            "locus": "turn",
            "per_run_reliability": self.wilson.to_dict(),
            "observed_k": self.observed_k,
            "horizon": self.horizon,
            "band": self.band,
            "asymptote": round(self.asymptote, 6),
            "asymptote_note": (
                "all-k-pass reliability decays to 0 for any per-run p<1 (no positive floor); "
                "the governing invariant is per_run_reliability.p_hat"
            ),
            "sla": self.sla,
            "k_star_point": self.k_star_point,
            "k_star_lower": self.k_star_lower,
            "meltdown_onset_point": self.meltdown_onset,
            "duration_buckets": (
                [bucket.to_dict() for bucket in self.duration_buckets] if self.duration_buckets else None
            ),
            "method": (
                "Wilson score interval on pooled per-run successes; pass^k band = [low^k, high^k] "
                "(exact CI via monotone transform). Sample-k decay over a FIXED gold set; not "
                "extrapolated over gold-set size."
            ),
        }


def _duration_buckets(
    results: list[_RunResult],
    confidence: float,
    n_buckets: int = 4,
    min_runs: int = 6,
) -> list[DurationBucket] | None:
    """Bucket runs into rank-balanced quantiles of ``duration_ms`` and Wilson-CI each.

    Returns ``None`` (not an empty list) when the trace data does not support it: a run is
    missing ``metrics['duration_ms']``, there is no duration variation, or there are too few
    runs to split. Deterministic: runs are sorted by duration, then split by rank.
    """
    durations: list[tuple[int, bool]] = []
    for run in results:
        metrics = getattr(run, "metrics", None)
        if not isinstance(metrics, dict):
            return None
        value = metrics.get("duration_ms")
        if value is None:
            return None
        durations.append((int(value), bool(run.passed)))
    if len(durations) < min_runs or len({d for d, _ in durations}) < 2:
        return None
    ordered = sorted(durations, key=lambda item: item[0])
    total = len(ordered)
    buckets: list[DurationBucket] = []
    index = 1
    for slot in range(n_buckets):
        start = slot * total // n_buckets
        end = (slot + 1) * total // n_buckets
        chunk = ordered[start:end]
        if not chunk:
            continue
        passed = sum(1 for _, ok in chunk if ok)
        interval = wilson_interval(passed, len(chunk), confidence)
        buckets.append(
            DurationBucket(
                label=f"Q{index}",
                lo_ms=chunk[0][0],
                hi_ms=chunk[-1][0],
                runs=len(chunk),
                passed=passed,
                p_hat=interval.p_hat,
                ci_low=interval.low,
                ci_high=interval.high,
            )
        )
        index += 1
    return buckets if len(buckets) >= 2 else None


def compute_reliability_curve(
    n_passed: int,
    n_runs: int,
    observed_k: int,
    *,
    sla: float | None = None,
    confidence: float = 0.95,
    horizon: int | None = None,
    duration_buckets: list[DurationBucket] | None = None,
) -> ReliabilityCurve:
    """Build the parametric reliability decay curve from pooled per-run counts.

    ``n_passed`` / ``n_runs`` are pooled across every recorded run; ``observed_k`` is the
    depth actually recorded (the model-free curve's max k) so the band can flag projection.
    """
    interval = wilson_interval(n_passed, n_runs, confidence)
    p_hat, p_low, p_high = interval.p_hat, interval.low, interval.high
    cap = max(observed_k, 64)
    k_star_point = _k_star(p_hat, sla, cap) if sla is not None else None
    k_star_lower = _k_star(p_low, sla, cap) if sla is not None else None
    meltdown = (k_star_lower + 1) if k_star_lower is not None else None
    # Show enough of the decay to be useful without an unbounded tail: cover the observed
    # range and a reference depth, and extend just past where the *honest* (lower-band) SLA
    # crossing happens. The crossing is driven by k_star_lower, never k_star_point — a
    # perfect point estimate (p_hat = 1) holds forever and would otherwise blow the horizon up.
    band_cap = 24
    if horizon is None:
        horizon = max(observed_k, 8)
        if k_star_lower is not None:
            horizon = max(horizon, min(k_star_lower + 2, band_cap))
    horizon = max(observed_k, min(horizon, band_cap))
    band = [
        {
            "k": k,
            "point": round(p_hat**k, 6),
            "lower": round(p_low**k, 6),
            "upper": round(p_high**k, 6),
            "projected": k > observed_k,
        }
        for k in range(1, horizon + 1)
    ]
    return ReliabilityCurve(
        wilson=interval,
        observed_k=observed_k,
        horizon=horizon,
        band=band,
        sla=sla,
        k_star_point=k_star_point,
        k_star_lower=k_star_lower,
        meltdown_onset=meltdown,
        asymptote=1.0 if p_hat >= 1.0 else 0.0,
        duration_buckets=duration_buckets,
    )


@dataclass(frozen=True)
class TaskReliability:
    """Reliability of a single task (one ``case_id``) across its recorded runs."""

    case_id: str
    runs: int
    passed: int
    run_ids: list[str] = field(default_factory=list)
    pass_caret: dict[int, float] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return self.runs > 0 and self.passed == self.runs

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "runs": self.runs,
            "passed": self.passed,
            "all_passed": self.all_passed,
            "run_ids": list(self.run_ids),
            "pass_caret": {str(j): round(value, 6) for j, value in sorted(self.pass_caret.items())},
        }


@dataclass(frozen=True)
class PassKReport:
    """The pass^1..pass^k curve over a set of tasks, plus an optional reliability gate."""

    k: int
    tasks: int
    total_runs: int
    curve: dict[int, float]
    per_task: list[TaskReliability]
    threshold: float | None = None
    reliability_curve: ReliabilityCurve | None = None
    sla: float | None = None

    @property
    def pass_at_1(self) -> float:
        return self.curve.get(1, 0.0)

    @property
    def pass_hat_k(self) -> float:
        return self.curve.get(self.k, 0.0)

    @property
    def gate_enabled(self) -> bool:
        return self.threshold is not None

    @property
    def gate_failed(self) -> bool:
        """True only when a threshold is set and the (model-free) pass^k point falls below it."""
        return self.threshold is not None and self.pass_hat_k < self.threshold

    @property
    def gate_passed(self) -> bool:
        return not self.gate_failed

    @property
    def gate_state(self) -> str:
        if self.threshold is None:
            return "off"
        return "fail" if self.gate_failed else "pass"

    @property
    def sla_gate_enabled(self) -> bool:
        return self.sla is not None and self.reliability_curve is not None

    @property
    def sla_gate_failed(self) -> bool:
        """True when the SLA is armed and even the lower CI band of pass^k misses it.

        This is the honest worst-case gate: a flaky agent that got lucky on a small sample
        has a wide Wilson interval, so its lower band stays under the SLA and the gate holds.
        """
        if self.sla is None or self.reliability_curve is None:
            return False
        return self.reliability_curve.lower_at(self.k) < self.sla

    @property
    def sla_gate_state(self) -> str:
        if not self.sla_gate_enabled:
            return "off"
        return "fail" if self.sla_gate_failed else "pass"

    @property
    def headline(self) -> str:
        """One-line, human-readable summary suitable for a CLI banner or PR comment."""
        flaky = [task.case_id for task in self.per_task if not task.all_passed]
        base = (
            f"pass@1={self.pass_at_1:.3f} pass^{self.k}={self.pass_hat_k:.3f} "
            f"over {self.tasks} task(s) x up to {self.k} run(s)"
        )
        if self.threshold is not None:
            base += f" [gate >= {self.threshold:.3f}: {self.gate_state.upper()}]"
        curve = self.reliability_curve
        if curve is not None:
            base += (
                f" | p={curve.p_hat:.3f} [{curve.ci_low:.3f},{curve.ci_high:.3f}] "
                f"pass^{self.k} band [{curve.lower_at(self.k):.3f},{curve.upper_at(self.k):.3f}]"
            )
            if self.sla is not None:
                base += (
                    f" SLA {self.sla:.3f}: k*={curve.k_star_lower} MOP={curve.meltdown_onset} "
                    f"[CI gate: {self.sla_gate_state.upper()}]"
                )
        if flaky:
            base += f"; flaky: {', '.join(flaky)}"
        return base

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "k": self.k,
            "tasks": self.tasks,
            "total_runs": self.total_runs,
            "pass_at_1": round(self.pass_at_1, 6),
            "pass_hat_k": round(self.pass_hat_k, 6),
            "threshold": self.threshold,
            "gate": self.gate_state,
            "curve": {str(j): round(value, 6) for j, value in sorted(self.curve.items())},
            "per_task": [task.to_dict() for task in self.per_task],
        }
        if self.reliability_curve is not None:
            payload["reliability_curve"] = self.reliability_curve.to_dict()
            payload["sla"] = self.sla
            payload["sla_gate"] = self.sla_gate_state
        return payload


def aggregate_pass_k(
    results: list[_RunResult],
    k: int | None = None,
    threshold: float | None = None,
    *,
    sla: float | None = None,
    confidence: float = 0.95,
) -> PassKReport:
    """Aggregate per-run verdicts into the pass^1..pass^k reliability curve.

    ``results`` is any list of recorded runs exposing ``case_id``, ``run_id`` and ``passed``
    (``CaseReport`` is the canonical input). Runs are grouped by ``case_id`` in first-seen
    order. ``k`` defaults to the *minimum* runs-per-task so pass^k is well defined for every
    task; an explicit ``k`` larger than that is rejected (you cannot ask "did all 5 runs
    pass?" for a task you only recorded 3 times).

    Two gates can be armed independently:

    * ``threshold`` — the model-free combinatorial gate on the pass^k point estimate.
    * ``sla`` — the model-based CI gate on the *lower* edge of the Wilson ``p^k`` band
      (``confidence`` sets the band width, default 0.95). This is the honest worst-case gate.

    The Wilson reliability decay curve is always attached so report consumers can render the
    band, ``k*`` and the Meltdown Onset Point even when neither gate is armed.
    """
    if not results:
        raise ValidationError("pass^k needs at least one recorded run")

    groups: OrderedDict[str, list[_RunResult]] = OrderedDict()
    for result in results:
        groups.setdefault(result.case_id, []).append(result)

    min_runs = min(len(runs) for runs in groups.values())
    if k is None:
        k = min_runs
    if k < 1:
        raise ValidationError(f"pass^k requires k >= 1 (got {k})")
    if k > min_runs:
        raise ValidationError(
            f"pass^{k} is undefined: the least-recorded task has only {min_runs} run(s). "
            f"Record at least {k} runs of every task, or pass a smaller --passk."
        )
    if threshold is not None and not 0.0 <= threshold <= 1.0:
        raise ValidationError(f"pass^k threshold must be in [0, 1] (got {threshold})")
    if sla is not None and not 0.0 <= sla <= 1.0:
        raise ValidationError(f"reliability SLA must be in [0, 1] (got {sla})")
    if not 0.0 < confidence < 1.0:
        raise ValidationError(f"reliability confidence must be in (0, 1) (got {confidence})")

    per_task: list[TaskReliability] = []
    total_runs = 0
    total_passed = 0
    for case_id, runs in groups.items():
        n = len(runs)
        c = sum(1 for run in runs if run.passed)
        total_runs += n
        total_passed += c
        per_task.append(
            TaskReliability(
                case_id=case_id,
                runs=n,
                passed=c,
                run_ids=[run.run_id for run in runs],
                pass_caret={j: pass_caret(c, n, j) for j in range(1, k + 1)},
            )
        )

    tasks = len(per_task)
    curve = {j: round(sum(task.pass_caret[j] for task in per_task) / tasks, 6) for j in range(1, k + 1)}
    reliability_curve = compute_reliability_curve(
        n_passed=total_passed,
        n_runs=total_runs,
        observed_k=min_runs,
        sla=sla,
        confidence=confidence,
        duration_buckets=_duration_buckets(results, confidence),
    )
    return PassKReport(
        k=k,
        tasks=tasks,
        total_runs=total_runs,
        curve=curve,
        per_task=per_task,
        threshold=threshold,
        reliability_curve=reliability_curve,
        sla=sla,
    )


# --------------------------------------------------------------------------------------
# Reporter render helpers. These return plain strings / JSON-able dicts so report.py can
# splice a reliability section into each format without importing anything back from
# report.py (passk depends only on models + stdlib, preserving the no-cycle, zero-dep core).
# --------------------------------------------------------------------------------------


def gate_message(report: PassKReport) -> str:
    """The blocking message used when the pass^k gate fails (shared by SARIF/JUnit/MD)."""
    flaky = [task.case_id for task in report.per_task if not task.all_passed]
    detail = f" Flaky task(s): {', '.join(flaky)}." if flaky else ""
    return (
        f"pass^{report.k} = {report.pass_hat_k:.3f} is below the required "
        f"reliability floor of {report.threshold:.3f}.{detail}"
    )


def sla_gate_message(report: PassKReport) -> str:
    """The blocking message for a failed reliability-SLA CI gate (lower confidence band)."""
    curve = report.reliability_curve
    assert curve is not None and report.sla is not None  # only called when the gate is armed
    return (
        f"reliability SLA not met: the {curve.wilson.confidence:.0%} lower confidence bound on "
        f"pass^{report.k} is {curve.lower_at(report.k):.3f}, below the SLA of {report.sla:.3f}. "
        f"At the worst-case per-run reliability ({curve.ci_low:.3f}) the SLA holds for at most "
        f"k*={curve.k_star_lower} consecutive run(s); the Meltdown Onset Point is "
        f"k={curve.meltdown_onset}."
    )


def _reliability_curve_markdown(report: PassKReport) -> list[str]:
    """The Reliability Decay Curve block (Wilson band, k*, MOP, duration buckets) for Markdown."""
    curve = report.reliability_curve
    if curve is None:
        return []
    lines = [
        "",
        f"**Reliability Decay Curve** &middot; per-run p **{curve.p_hat:.3f}** "
        f"[{curve.ci_low:.3f}, {curve.ci_high:.3f}] at {curve.wilson.confidence:.0%} "
        f"(Wilson, n={curve.wilson.n}) &middot; _model-based residual; pass^k = p^k_",
        "",
        "| k | pass^k (point) | lower band | upper band | |",
        "| --- | --- | --- | --- | --- |",
    ]
    for point in curve.band:
        tag = "projected" if point["projected"] else ""
        lines.append(f"| {point['k']} | {point['point']:.3f} | {point['lower']:.3f} | {point['upper']:.3f} | {tag} |")
    if report.sla is not None:
        lines += [
            "",
            f"SLA **{report.sla:.3f}** &middot; k\\* (lower band) **{curve.k_star_lower}** "
            f"&middot; Meltdown Onset Point **k={curve.meltdown_onset}** "
            f"&middot; CI gate **{report.sla_gate_state.upper()}**",
        ]
        if report.sla_gate_failed:
            lines += ["", f"> {sla_gate_message(report)}"]
    if curve.duration_buckets:
        lines += [
            "",
            "_Reliability across task-duration buckets (descriptive; rank-balanced, own Wilson CI):_",
            "",
            "| Bucket | duration ms | runs | passed | p | CI |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for bucket in curve.duration_buckets:
            lines.append(
                f"| {bucket.label} | {bucket.lo_ms}–{bucket.hi_ms} | {bucket.runs} | {bucket.passed} | "
                f"{bucket.p_hat:.3f} | [{bucket.ci_low:.3f}, {bucket.ci_high:.3f}] |"
            )
    return lines


def markdown_section(report: PassKReport) -> str:
    """A GitHub-flavored Markdown block for the PR comment / step summary."""
    verdict = {"fail": "❌ FAIL", "pass": "✅ PASS", "off": "—"}[report.gate_state]
    lines = [
        "",
        f"### Reliability — pass^{report.k} {verdict}",
        "",
        (
            f"**pass@1 {report.pass_at_1:.3f}** &middot; **pass^{report.k} {report.pass_hat_k:.3f}** "
            f"&middot; {report.tasks} task(s) &middot; {report.total_runs} run(s)"
            + (f" &middot; floor **{report.threshold:.3f}**" if report.threshold is not None else "")
        ),
        "",
        "| Task | Runs | Passed | pass^" + str(report.k) + " |",
        "| --- | --- | --- | --- |",
    ]
    for task in report.per_task:
        cell_case = str(task.case_id).replace("|", "\\|")
        lines.append(f"| {cell_case} | {task.runs} | {task.passed} | {task.pass_caret.get(report.k, 0.0):.3f} |")
    if report.gate_failed:
        lines += ["", f"> {gate_message(report)}"]
    lines += _reliability_curve_markdown(report)
    lines.append("")
    return "\n".join(lines)


def html_section(report: PassKReport) -> str:
    """An HTML block reusing the report's existing CSS classes for a consistent look."""
    state = report.gate_state
    cards = [
        ("pass@1", f"{report.pass_at_1:.3f}", ""),
        (f"pass^{report.k}", f"{report.pass_hat_k:.3f}", " fail" if report.gate_failed else ""),
        ("Tasks", str(report.tasks), ""),
        ("Runs", str(report.total_runs), ""),
        ("Floor", "—" if report.threshold is None else f"{report.threshold:.3f}", ""),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{escape(label)}</div><div class="v{cls}">{escape(value)}</div></div>'
        for label, value, cls in cards
    )
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(task.case_id))}</td>"
        f"<td>{task.passed}/{task.runs}</td>"
        f"<td>{task.pass_caret.get(report.k, 0.0):.3f}</td>"
        f"<td>{'all passed' if task.all_passed else '<span class=&quot;viol&quot;>flaky</span>'}</td>"
        "</tr>"
        for task in report.per_task
    )
    gate_banner = ""
    if report.gate_failed:
        gate_banner = f'<div class="why"><b>Reliability gate failed</b><div class="line">{escape(gate_message(report))}</div></div>'
    head = "<tr><th>Task</th><th>Passed</th><th>pass^" + str(report.k) + "</th><th>Status</th></tr>"
    return (
        f'<div class="seclabel">Reliability &middot; pass^{report.k} ({escape(state)})</div>'
        f"{gate_banner}"
        f'<div class="cards">{card_html}</div>'
        f'<table class="kv"><thead>{head}</thead><tbody>{rows}</tbody></table>'
        f"{_reliability_curve_html(report)}"
    )


def _reliability_curve_html(report: PassKReport) -> str:
    """The Reliability Decay Curve HTML block (Wilson band, k*, MOP, duration buckets)."""
    curve = report.reliability_curve
    if curve is None:
        return ""
    cards = [
        ("per-run p", f"{curve.p_hat:.3f}", ""),
        (f"{curve.wilson.confidence:.0%} CI", f"{curve.ci_low:.3f}–{curve.ci_high:.3f}", ""),
        (f"pass^{report.k} lower", f"{curve.lower_at(report.k):.3f}", " fail" if report.sla_gate_failed else ""),
        ("k* (lower band)", "—" if curve.k_star_lower is None else str(curve.k_star_lower), ""),
        ("Meltdown Onset", "—" if curve.meltdown_onset is None else f"k={curve.meltdown_onset}", ""),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{escape(label)}</div><div class="v{cls}">{escape(value)}</div></div>'
        for label, value, cls in cards
    )
    band_rows = "".join(
        "<tr>"
        f"<td>{point['k']}</td>"
        f"<td>{point['point']:.3f}</td>"
        f"<td>{point['lower']:.3f}</td>"
        f"<td>{point['upper']:.3f}</td>"
        f"<td>{'projected' if point['projected'] else 'observed'}</td>"
        "</tr>"
        for point in curve.band
    )
    band_head = "<tr><th>k</th><th>pass^k</th><th>lower</th><th>upper</th><th>range</th></tr>"
    banner = ""
    if report.sla_gate_failed:
        banner = (
            f'<div class="why"><b>Reliability SLA gate failed</b>'
            f'<div class="line">{escape(sla_gate_message(report))}</div></div>'
        )
    buckets_html = ""
    if curve.duration_buckets:
        bucket_rows = "".join(
            "<tr>"
            f"<td>{escape(bucket.label)}</td>"
            f"<td>{bucket.lo_ms}–{bucket.hi_ms} ms</td>"
            f"<td>{bucket.passed}/{bucket.runs}</td>"
            f"<td>{bucket.p_hat:.3f}</td>"
            f"<td>{bucket.ci_low:.3f}–{bucket.ci_high:.3f}</td>"
            "</tr>"
            for bucket in curve.duration_buckets
        )
        bucket_head = "<tr><th>Bucket</th><th>duration</th><th>passed</th><th>p</th><th>CI</th></tr>"
        buckets_html = (
            '<div class="seclabel">Reliability by task-duration bucket</div>'
            f'<table class="kv"><thead>{bucket_head}</thead><tbody>{bucket_rows}</tbody></table>'
        )
    return (
        '<div class="seclabel">Reliability Decay Curve &middot; model-based (Wilson p^k band)</div>'
        f"{banner}"
        f'<div class="cards">{card_html}</div>'
        f'<table class="kv"><thead>{band_head}</thead><tbody>{band_rows}</tbody></table>'
        f"{buckets_html}"
    )


def sarif_rule_and_result(report: PassKReport, anchor_uri: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The SARIF rule + result for a failed pass^k gate, or None when the gate did not fail.

    Anchors to the same committed file the other findings use (line 1, since the floor is a
    CI argument, not a policy-file key) so GitHub code scanning renders it.
    """
    if not report.gate_failed:
        return None
    rule = {
        "id": PASSK_RULE_ID,
        "name": "ReliabilityPassK",
        "shortDescription": {"text": f"pass^{report.k} reliability below floor"},
        "fullDescription": {
            "text": "The pass^k reliability metric (fraction of tasks whose every recorded run passed) "
            "fell below the configured floor — the agent is flaky across repeated runs."
        },
        "help": {"text": gate_message(report)},
        "defaultConfiguration": {"level": "error"},
        "properties": {"tags": ["agent-trace", "reliability", "pass-k"], "problem": {"severity": "error"}},
    }
    result = {
        "ruleId": PASSK_RULE_ID,
        "level": "error",
        "kind": "fail",
        "message": {"text": f"{PASSK_GATE_SEVERITY}: {gate_message(report)}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": anchor_uri},
                    "region": {"startLine": 1},
                }
            }
        ],
        "properties": {
            "severity": PASSK_GATE_SEVERITY,
            "k": report.k,
            "pass_at_1": report.pass_at_1,
            "pass_hat_k": report.pass_hat_k,
            "threshold": report.threshold,
        },
    }
    return rule, result


def sla_sarif_rule_and_result(report: PassKReport, anchor_uri: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The SARIF rule + result for a failed reliability-SLA CI gate, or None when it passed.

    Distinct from :func:`sarif_rule_and_result`: this gate fires on the *lower* edge of the
    Wilson band (the honest worst-case), so it can block even when the point estimate clears.
    """
    if not report.sla_gate_failed:
        return None
    curve = report.reliability_curve
    assert curve is not None and report.sla is not None
    rule = {
        "id": PASSK_SLA_RULE_ID,
        "name": "ReliabilitySla",
        "shortDescription": {"text": f"pass^{report.k} lower confidence band below SLA"},
        "fullDescription": {
            "text": "The lower Wilson confidence bound on pass^k fell below the reliability SLA — even "
            "in the worst case consistent with the observed runs, the agent cannot be certified to "
            "meet the SLA at this k (it may be a lucky small sample)."
        },
        "help": {"text": sla_gate_message(report)},
        "defaultConfiguration": {"level": "error"},
        "properties": {
            "tags": ["agent-trace", "reliability", "pass-k", "confidence-band"],
            "problem": {"severity": "error"},
        },
    }
    result = {
        "ruleId": PASSK_SLA_RULE_ID,
        "level": "error",
        "kind": "fail",
        "message": {"text": f"{PASSK_GATE_SEVERITY}: {sla_gate_message(report)}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": anchor_uri},
                    "region": {"startLine": 1},
                }
            }
        ],
        "properties": {
            "severity": PASSK_GATE_SEVERITY,
            "k": report.k,
            "sla": report.sla,
            "confidence": curve.wilson.confidence,
            "pass_hat_k_lower": round(curve.lower_at(report.k), 6),
            "k_star_lower": curve.k_star_lower,
            "meltdown_onset_point": curve.meltdown_onset,
        },
    }
    return rule, result
