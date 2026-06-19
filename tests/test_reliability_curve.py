"""The reliability decay curve (Wilson p^k band), its k*/Meltdown-Onset-Point, and the
honest worst-case SLA gate that fires on the band's LOWER edge.

The upgrade from a fixed-k pass^k point to a reliability curve is the headline: a lucky
small-n run still has a point estimate of 1.0, but its Wilson band is wide, so gating on the
*lower* edge keeps a flaky agent from sneaking past. These tests pin the curve math, the k*
/ MOP semantics, the asymptote honesty (sample-k decay over a FIXED gold set, never an
extrapolation of an asymptote over gold-set size), and the gate's behaviour through every
reporter and the CLI.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from plimsoll.cli import main
from plimsoll.models import CaseReport
from plimsoll.passk import (
    aggregate_pass_k,
    compute_reliability_curve,
    markdown_section,
)
from plimsoll.report import (
    write_html_report,
    write_junit_report,
    write_sarif_report,
)


def _report(case_id: str, run_id: str, passed: bool, duration_ms: int | None = None) -> CaseReport:
    metrics = {} if duration_ms is None else {"duration_ms": duration_ms}
    return CaseReport(case_id, run_id, 100 if passed else 0, passed, metrics, [])


def _runs(case_id: str, verdicts: list[bool]) -> list[CaseReport]:
    return [_report(case_id, f"{case_id}-r{i}", passed) for i, passed in enumerate(verdicts)]


class ReliabilityCurveMathTests(unittest.TestCase):
    def test_band_is_point_with_a_wilson_envelope(self) -> None:
        curve = compute_reliability_curve(8, 10, observed_k=3, sla=0.5, confidence=0.95)
        self.assertAlmostEqual(curve.p_hat, 0.8, places=6)
        self.assertEqual(len(curve.band), curve.horizon)
        for point in curve.band:
            k = point["k"]
            self.assertAlmostEqual(point["point"], round(curve.p_hat**k, 6), places=6)
            self.assertAlmostEqual(point["lower"], round(curve.ci_low**k, 6), places=6)
            self.assertAlmostEqual(point["upper"], round(curve.ci_high**k, 6), places=6)
            # The point estimate sits inside its own band, which is what the gate trusts.
            self.assertLessEqual(point["lower"], point["point"] + 1e-9)
            self.assertLessEqual(point["point"], point["upper"] + 1e-9)

    def test_curve_decays_monotonically_in_k(self) -> None:
        curve = compute_reliability_curve(8, 10, observed_k=3, sla=0.5)
        for series in ("point", "lower", "upper"):
            values = [p[series] for p in curve.band]
            self.assertEqual(values, sorted(values, reverse=True))

    def test_projection_flag_marks_k_beyond_observed_depth(self) -> None:
        curve = compute_reliability_curve(8, 10, observed_k=3)
        observed = [p["k"] for p in curve.band if not p["projected"]]
        projected = [p["k"] for p in curve.band if p["projected"]]
        self.assertEqual(observed, [1, 2, 3])
        self.assertTrue(all(k > 3 for k in projected))

    def test_k_star_lower_is_never_above_the_point_estimate(self) -> None:
        # The honest worst-case k* must be <= the optimistic point k*: the wide band on a small
        # sample collapses k*, which is precisely the protection the band buys.
        curve = compute_reliability_curve(8, 10, observed_k=3, sla=0.5)
        self.assertEqual(curve.k_star_point, 3)  # 0.8^3 = 0.512 >= 0.5, 0.8^4 < 0.5
        self.assertEqual(curve.k_star_lower, 0)  # lower bound ~0.49 misses the SLA even at k=1
        self.assertEqual(curve.meltdown_onset, curve.k_star_lower + 1)

    def test_no_positive_asymptote_for_imperfect_runs(self) -> None:
        curve = compute_reliability_curve(8, 10, observed_k=3)
        self.assertEqual(curve.asymptote, 0.0)
        payload = curve.to_dict()
        self.assertIn("decays to 0", payload["asymptote_note"])
        self.assertEqual(payload["regime"], "model-based residual")
        self.assertEqual(payload["locus"], "turn")
        self.assertIn("Wilson", payload["method"])
        self.assertIn("FIXED gold set", payload["method"])

    def test_duration_buckets_present_only_when_trace_data_supports(self) -> None:
        # No metrics -> None (the curve still builds, just without the duration view).
        plain = aggregate_pass_k(_runs("c", [True, False, True, True, False, True]))
        self.assertIsNone(plain.reliability_curve.duration_buckets)
        # With per-run durations and enough runs, rank-balanced quantile buckets appear.
        timed = [
            _report("c", f"c-r{i}", passed, duration_ms=ms)
            for i, (ms, passed) in enumerate(
                [(10, True), (20, True), (30, True), (40, True), (50, False), (60, False), (70, True), (80, True)]
            )
        ]
        curve = aggregate_pass_k(timed).reliability_curve
        self.assertIsNotNone(curve.duration_buckets)
        buckets = curve.duration_buckets
        self.assertGreaterEqual(len(buckets), 2)
        # Buckets are ordered by duration and partition all the runs.
        self.assertEqual(sum(b.runs for b in buckets), len(timed))
        self.assertEqual([b.label for b in buckets], sorted(b.label for b in buckets))


class SlaGateTests(unittest.TestCase):
    def test_sla_gate_off_when_not_armed(self) -> None:
        report = aggregate_pass_k(_runs("t", [True, True, False]))
        self.assertFalse(report.sla_gate_enabled)
        self.assertFalse(report.sla_gate_failed)
        self.assertEqual(report.sla_gate_state, "off")

    def test_lucky_sample_cannot_sneak_past_the_band_gate(self) -> None:
        # 8/10 at k=2: the model-free pass^2 point (0.62) clears a 0.5 floor, but the lower
        # Wilson band on pass^2 (~0.24) does NOT — the honest gate blocks where the point lets
        # through. This is the headline behaviour the curve exists to deliver.
        report = aggregate_pass_k(_runs("t", [True] * 8 + [False] * 2), k=2, threshold=0.5, sla=0.5)
        self.assertGreaterEqual(report.pass_hat_k, 0.5)
        self.assertEqual(report.gate_state, "pass")  # model-free point gate clears
        self.assertTrue(report.sla_gate_failed)  # honest band gate blocks
        self.assertEqual(report.sla_gate_state, "fail")

    def test_band_gate_passes_when_lower_bound_clears_the_sla(self) -> None:
        report = aggregate_pass_k(_runs("t", [True] * 8 + [False] * 2), k=2, sla=0.2)
        self.assertTrue(report.sla_gate_enabled)
        self.assertFalse(report.sla_gate_failed)
        self.assertEqual(report.sla_gate_state, "pass")

    def test_sla_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            aggregate_pass_k(_runs("t", [True, True]), sla=1.5)

    def test_confidence_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            aggregate_pass_k(_runs("t", [True, True]), sla=0.5, confidence=1.0)

    def test_to_dict_carries_curve_and_gate_state(self) -> None:
        payload = aggregate_pass_k(_runs("t", [True] * 8 + [False] * 2), k=2, sla=0.5).to_dict()
        self.assertEqual(payload["sla"], 0.5)
        self.assertEqual(payload["sla_gate"], "fail")
        self.assertIn("reliability_curve", payload)
        self.assertEqual(payload["reliability_curve"]["regime"], "model-based residual")

    def test_headline_surfaces_band_k_star_and_mop(self) -> None:
        report = aggregate_pass_k(_runs("t", [True] * 8 + [False] * 2), k=2, sla=0.5)
        headline = report.headline
        self.assertIn("k*", headline)
        self.assertIn("MOP", headline)
        self.assertIn("CI gate", headline)


class SlaReporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-sla-report-"))
        self.case_reports = [CaseReport("t", "t-r0", 100, True, {}, [])]
        self.failing = aggregate_pass_k(_runs("t", [True] * 8 + [False] * 2), k=2, sla=0.5)
        self.passing = aggregate_pass_k(_runs("t", [True] * 8 + [False] * 2), k=2, sla=0.2)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_junit_adds_sla_testcase_and_fails_it(self) -> None:
        out = self.tmp / "j.xml"
        write_junit_report(out, self.case_reports, passk=self.failing)
        root = ElementTree.parse(out).getroot()
        names = [tc.attrib["name"] for tc in root.findall("testcase")]
        self.assertIn("reliability.sla_pass_caret_2", names)
        sla_case = next(tc for tc in root.findall("testcase") if tc.attrib["name"].startswith("reliability.sla"))
        self.assertIsNotNone(sla_case.find("failure"))

    def test_junit_sla_testcase_passes_when_band_clears(self) -> None:
        out = self.tmp / "j2.xml"
        write_junit_report(out, self.case_reports, passk=self.passing)
        root = ElementTree.parse(out).getroot()
        sla_case = next(tc for tc in root.findall("testcase") if tc.attrib["name"].startswith("reliability.sla"))
        self.assertIsNone(sla_case.find("failure"))

    def test_sarif_adds_sla_result_only_on_failure(self) -> None:
        fail_out = self.tmp / "fail.sarif.json"
        write_sarif_report(fail_out, self.case_reports, passk=self.failing)
        run = json.loads(fail_out.read_text(encoding="utf-8"))["runs"][0]
        self.assertIn("reliability_sla", {r["ruleId"] for r in run["results"]})
        result = next(r for r in run["results"] if r["ruleId"] == "reliability_sla")
        self.assertEqual(run["tool"]["driver"]["rules"][result["ruleIndex"]]["id"], "reliability_sla")

        pass_out = self.tmp / "pass.sarif.json"
        write_sarif_report(pass_out, self.case_reports, passk=self.passing)
        run2 = json.loads(pass_out.read_text(encoding="utf-8"))["runs"][0]
        self.assertNotIn("reliability_sla", {r["ruleId"] for r in run2["results"]})

    def test_html_renders_the_decay_curve_and_mop(self) -> None:
        out = self.tmp / "r.html"
        write_html_report(out, self.case_reports, passk=self.failing)
        html = out.read_text(encoding="utf-8")
        self.assertIn("Reliability Decay Curve", html)
        self.assertIn("Meltdown Onset", html)
        self.assertIn("Wilson", html)

    def test_markdown_renders_the_band_and_gate(self) -> None:
        md = markdown_section(self.failing)
        self.assertIn("Reliability Decay Curve", md)
        self.assertIn("Meltdown Onset Point", md)
        self.assertIn("CI gate", md)


class CliSlaIntegrationTests(unittest.TestCase):
    """End-to-end: the SLA band gate fails CI on its own, and --cascade emits telemetry."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-sla-cli-"))
        self.indir = self.tmp / "traces"
        self.indir.mkdir()
        self.policy = self.tmp / "policy.json"
        self.policy.write_text(
            json.dumps({"must_precede": [{"before": "manager_review", "after": "grant_access"}]}),
            encoding="utf-8",
        )
        # One case recorded 3 times: two clean, one ordering bypass (fails) -> pooled 2/3.
        self._write("r1", ["manager_review", "grant_access"])
        self._write("r2", ["manager_review", "grant_access"])
        self._write("r3", ["grant_access"])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, run_id: str, tools: list[str]) -> None:
        spans = [
            {
                "span_id": f"s{i}",
                "name": tool,
                "kind": "tool",
                "status": "ok",
                "start_ms": i * 10,
                "end_ms": i * 10 + 5,
                "tool_name": tool,
            }
            for i, tool in enumerate(tools)
        ]
        (self.indir / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "case_id": "access", "final_output": "done", "spans": spans}),
            encoding="utf-8",
        )

    def _run(self, *extra: str) -> int:
        return main(
            [
                "run",
                "--input",
                str(self.indir),
                "--policy",
                str(self.policy),
                "--out",
                str(self.tmp / "out"),
                "--quiet",
                *extra,
            ]
        )

    def test_reliability_sla_fails_ci_and_records_curve(self) -> None:
        code = self._run("--reliability-sla", "0.9", "--sarif")
        self.assertEqual(code, 1)
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        reliability = payload["reliability"]
        self.assertEqual(reliability["sla_gate"], "fail")
        curve = reliability["reliability_curve"]
        self.assertEqual(curve["regime"], "model-based residual")
        self.assertIn("per_run_reliability", curve)
        sarif = json.loads((self.tmp / "out" / "report.sarif.json").read_text(encoding="utf-8"))
        self.assertIn("reliability_sla", {r["ruleId"] for r in sarif["runs"][0]["results"]})

    def test_custom_confidence_widens_the_band(self) -> None:
        # A higher confidence widens the Wilson band, lowering the certified floor.
        self._run("--reliability-sla", "0.5", "--reliability-confidence", "0.99")
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["reliability"]["reliability_curve"]["per_run_reliability"]["confidence"], 0.99)

    def test_cascade_flag_emits_telemetry_block(self) -> None:
        code = self._run("--cascade", "--exit-zero")
        self.assertEqual(code, 0)
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertIn("cascade", payload)
        boundary = payload["cascade"]["boundaries"][0]
        for key in ("alpha", "disagreementRate", "losslessViolations"):
            self.assertIn(key, boundary)
        self.assertIn("measured_sentence", payload["cascade"])

    def test_no_reliability_flag_means_no_cascade_or_reliability(self) -> None:
        code = self._run("--exit-zero")
        self.assertEqual(code, 0)
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertNotIn("cascade", payload)
        self.assertNotIn("reliability", payload)


if __name__ == "__main__":
    unittest.main()
