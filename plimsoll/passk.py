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

# The pass^k gate failing is a reliability regression that should block a release, so it
# carries the same weight the CLI's own gate uses for a blocking finding.
PASSK_GATE_SEVERITY = "high"
PASSK_RULE_ID = "reliability_pass_k"


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
        """True only when a threshold is set and pass^k falls below it."""
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
    def headline(self) -> str:
        """One-line, human-readable summary suitable for a CLI banner or PR comment."""
        flaky = [task.case_id for task in self.per_task if not task.all_passed]
        base = (
            f"pass@1={self.pass_at_1:.3f} pass^{self.k}={self.pass_hat_k:.3f} "
            f"over {self.tasks} task(s) x up to {self.k} run(s)"
        )
        if self.threshold is not None:
            base += f" [gate >= {self.threshold:.3f}: {self.gate_state.upper()}]"
        if flaky:
            base += f"; flaky: {', '.join(flaky)}"
        return base

    def to_dict(self) -> dict[str, Any]:
        return {
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


def aggregate_pass_k(
    results: list[_RunResult],
    k: int | None = None,
    threshold: float | None = None,
) -> PassKReport:
    """Aggregate per-run verdicts into the pass^1..pass^k reliability curve.

    ``results`` is any list of recorded runs exposing ``case_id``, ``run_id`` and ``passed``
    (``CaseReport`` is the canonical input). Runs are grouped by ``case_id`` in first-seen
    order. ``k`` defaults to the *minimum* runs-per-task so pass^k is well defined for every
    task; an explicit ``k`` larger than that is rejected (you cannot ask "did all 5 runs
    pass?" for a task you only recorded 3 times). ``threshold`` (optional) arms the gate.
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

    per_task: list[TaskReliability] = []
    total_runs = 0
    for case_id, runs in groups.items():
        n = len(runs)
        c = sum(1 for run in runs if run.passed)
        total_runs += n
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
    return PassKReport(
        k=k,
        tasks=tasks,
        total_runs=total_runs,
        curve=curve,
        per_task=per_task,
        threshold=threshold,
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
