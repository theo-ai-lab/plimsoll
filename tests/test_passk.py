import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from plimsoll.cli import main
from plimsoll.models import CaseReport, ValidationError
from plimsoll.passk import aggregate_pass_k, pass_caret
from plimsoll.report import (
    report_to_dict,
    write_html_report,
    write_junit_report,
    write_sarif_report,
)


def _report(case_id: str, run_id: str, passed: bool) -> CaseReport:
    return CaseReport(
        case_id=case_id,
        run_id=run_id,
        score=100 if passed else 0,
        passed=passed,
        metrics={},
        findings=[],
    )


def _runs(case_id: str, verdicts: list[bool]) -> list[CaseReport]:
    return [_report(case_id, f"{case_id}-r{i}", passed) for i, passed in enumerate(verdicts)]


class PassCaretEstimatorTests(unittest.TestCase):
    def test_endpoints_match_the_definitions(self) -> None:
        # pass^1 = c/n (per-run pass rate)
        self.assertAlmostEqual(pass_caret(2, 3, 1), 2 / 3)
        # pass^n = 1 iff every recorded run passed
        self.assertEqual(pass_caret(3, 3, 3), 1.0)
        self.assertEqual(pass_caret(2, 3, 3), 0.0)

    def test_combinatorial_estimator(self) -> None:
        # C(c,k)/C(n,k): chance that k drawn runs all pass
        self.assertAlmostEqual(pass_caret(2, 3, 2), math.comb(2, 2) / math.comb(3, 2))
        self.assertAlmostEqual(pass_caret(4, 5, 2), math.comb(4, 2) / math.comb(5, 2))

    def test_zero_when_fewer_passed_than_k(self) -> None:
        self.assertEqual(pass_caret(1, 4, 2), 0.0)

    def test_invalid_inputs_raise(self) -> None:
        with self.assertRaises(ValueError):
            pass_caret(1, 0, 1)  # no runs
        with self.assertRaises(ValueError):
            pass_caret(1, 3, 4)  # k > runs
        with self.assertRaises(ValueError):
            pass_caret(5, 3, 2)  # passed > runs


class AggregatePassKTests(unittest.TestCase):
    def test_headline_definition_when_n_equals_k(self) -> None:
        # 3 tasks, each run 3 times; pass^3 = fraction of tasks where ALL 3 runs pass.
        reports = (
            _runs("t1", [True, True, True]) + _runs("t2", [True, False, True]) + _runs("t3", [False, False, False])
        )
        report = aggregate_pass_k(reports)
        self.assertEqual(report.k, 3)
        self.assertEqual(report.tasks, 3)
        self.assertEqual(report.total_runs, 9)
        # Only t1 passes all 3 -> pass^3 = 1/3.
        self.assertAlmostEqual(report.pass_hat_k, 1 / 3, places=6)
        # pass^1 = mean(3/3, 2/3, 0/3).
        self.assertAlmostEqual(report.pass_at_1, (1 + 2 / 3 + 0) / 3, places=6)
        # The curve is monotonically non-increasing in k (more runs is harder).
        values = [report.curve[j] for j in range(1, report.k + 1)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_k_defaults_to_minimum_runs_per_task(self) -> None:
        reports = _runs("t1", [True, True, True]) + _runs("t2", [True, True])
        report = aggregate_pass_k(reports)
        self.assertEqual(report.k, 2)  # t2 only has 2 runs

    def test_explicit_k_larger_than_min_runs_is_rejected(self) -> None:
        reports = _runs("t1", [True, True, True]) + _runs("t2", [True, True])
        with self.assertRaises(ValidationError):
            aggregate_pass_k(reports, k=3)

    def test_empty_results_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            aggregate_pass_k([])

    def test_threshold_out_of_range_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            aggregate_pass_k(_runs("t1", [True]), threshold=1.5)

    def test_per_task_carries_run_ids(self) -> None:
        report = aggregate_pass_k(_runs("t1", [True, False]))
        task = report.per_task[0]
        self.assertEqual(task.run_ids, ["t1-r0", "t1-r1"])
        self.assertFalse(task.all_passed)

    def test_deterministic(self) -> None:
        reports = _runs("t1", [True, False, True]) + _runs("t2", [True, True, True])
        self.assertEqual(aggregate_pass_k(reports).to_dict(), aggregate_pass_k(reports).to_dict())


class PassKGateTests(unittest.TestCase):
    def test_gate_off_without_threshold(self) -> None:
        report = aggregate_pass_k(_runs("t1", [True, False]))
        self.assertFalse(report.gate_enabled)
        self.assertFalse(report.gate_failed)
        self.assertEqual(report.gate_state, "off")

    def test_gate_fails_below_floor(self) -> None:
        # pass^2 for 1/2 passing = C(1,2)/C(2,2) = 0 -> below 0.5 floor.
        report = aggregate_pass_k(_runs("t1", [True, False]), threshold=0.5)
        self.assertTrue(report.gate_failed)
        self.assertEqual(report.gate_state, "fail")

    def test_gate_passes_at_or_above_floor(self) -> None:
        report = aggregate_pass_k(_runs("t1", [True, True]), threshold=1.0)
        self.assertTrue(report.gate_passed)
        self.assertEqual(report.gate_state, "pass")


class ReporterWiringTests(unittest.TestCase):
    """The pass^k report must thread through every reporter, and be absent by default."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-passk-report-"))
        self.case_reports = [CaseReport("t1", "t1-r0", 100, True, {}, [])]
        self.failing = aggregate_pass_k(_runs("t1", [True, False]), threshold=0.9)
        self.passing = aggregate_pass_k(_runs("t1", [True, True]), threshold=0.5)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_report_dict_omits_reliability_by_default(self) -> None:
        self.assertNotIn("reliability", report_to_dict(self.case_reports))

    def test_report_dict_includes_reliability_when_provided(self) -> None:
        payload = report_to_dict(self.case_reports, passk=self.failing)
        self.assertEqual(payload["reliability"]["gate"], "fail")
        self.assertEqual(payload["reliability"]["k"], 2)

    def test_junit_adds_failing_reliability_testcase(self) -> None:
        out = self.tmp / "j.xml"
        write_junit_report(out, self.case_reports, passk=self.failing)
        root = ElementTree.parse(out).getroot()
        names = [tc.attrib["name"] for tc in root.findall("testcase")]
        self.assertIn("reliability.pass_caret_2", names)
        # one normal case (passing) + the failing reliability testcase
        self.assertEqual(root.attrib["tests"], "2")
        self.assertEqual(root.attrib["failures"], "1")
        reliability_case = next(tc for tc in root.findall("testcase") if tc.attrib["name"].startswith("reliability"))
        self.assertIsNotNone(reliability_case.find("failure"))

    def test_junit_reliability_testcase_passes_when_gate_ok(self) -> None:
        out = self.tmp / "j2.xml"
        write_junit_report(out, self.case_reports, passk=self.passing)
        root = ElementTree.parse(out).getroot()
        self.assertEqual(root.attrib["failures"], "0")
        reliability_case = next(tc for tc in root.findall("testcase") if tc.attrib["name"].startswith("reliability"))
        self.assertIsNone(reliability_case.find("failure"))

    def test_sarif_adds_reliability_result_only_on_failure(self) -> None:
        failing_out = self.tmp / "fail.sarif.json"
        write_sarif_report(failing_out, self.case_reports, passk=self.failing)
        payload = json.loads(failing_out.read_text(encoding="utf-8"))
        run = payload["runs"][0]
        self.assertIn("reliability_pass_k", {rule["id"] for rule in run["tool"]["driver"]["rules"]})
        result = next(r for r in run["results"] if r["ruleId"] == "reliability_pass_k")
        self.assertEqual(result["level"], "error")
        self.assertIsInstance(result["ruleIndex"], int)
        # ruleIndex must point at the matching rule in the driver's rules list.
        self.assertEqual(run["tool"]["driver"]["rules"][result["ruleIndex"]]["id"], "reliability_pass_k")

        passing_out = self.tmp / "pass.sarif.json"
        write_sarif_report(passing_out, self.case_reports, passk=self.passing)
        payload2 = json.loads(passing_out.read_text(encoding="utf-8"))
        self.assertNotIn("reliability_pass_k", {r["ruleId"] for r in payload2["runs"][0]["results"]})

    def test_html_includes_reliability_section(self) -> None:
        out = self.tmp / "r.html"
        write_html_report(out, self.case_reports, passk=self.failing)
        html = out.read_text(encoding="utf-8")
        self.assertIn("Reliability", html)
        self.assertIn("pass^2", html)
        self.assertIn("Reliability gate failed", html)

    def test_html_unchanged_without_passk(self) -> None:
        out = self.tmp / "plain.html"
        write_html_report(out, self.case_reports)
        self.assertNotIn("Reliability", out.read_text(encoding="utf-8"))


class CliPassKIntegrationTests(unittest.TestCase):
    """End-to-end: repeated recorded runs of the same case_id gated through the CLI."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-passk-cli-"))
        self.indir = self.tmp / "traces"
        self.indir.mkdir()
        self.policy = self.tmp / "policy.json"
        self.policy.write_text(
            json.dumps({"must_precede": [{"before": "manager_review", "after": "grant_access"}]}),
            encoding="utf-8",
        )
        # One case ("access") recorded 3 times: two safe runs + one ordering bypass (fails).
        self._write("r1", ["manager_review", "grant_access"])
        self._write("r2", ["manager_review", "grant_access"])
        self._write("r3", ["grant_access"])  # bypass -> critical tool_order -> run fails

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

    def test_passk_threshold_fails_ci_and_records_reliability(self) -> None:
        code = self._run("--passk-threshold", "0.9", "--sarif", "--junit")
        self.assertEqual(code, 1)
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertIn("reliability", payload)
        reliability = payload["reliability"]
        self.assertEqual(reliability["k"], 3)
        self.assertEqual(reliability["gate"], "fail")
        # pass^3 = 0 (the flaky bypass run means not all 3 passed).
        self.assertEqual(reliability["pass_hat_k"], 0.0)
        # SARIF + JUnit carry the reliability finding.
        sarif = json.loads((self.tmp / "out" / "report.sarif.json").read_text(encoding="utf-8"))
        self.assertIn("reliability_pass_k", {r["ruleId"] for r in sarif["runs"][0]["results"]})

    def test_passk_report_only_without_threshold(self) -> None:
        # --passk alone aggregates and reports, but does not by itself fail CI.
        # (All three runs share a case; the only failure source is the bypass run's findings,
        # so we assert the reliability block exists and records the flakiness.)
        code = self._run("--passk", "3", "--exit-zero")
        self.assertEqual(code, 0)
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["reliability"]["gate"], "off")
        self.assertEqual(payload["reliability"]["pass_hat_k"], 0.0)

    def test_no_passk_flag_means_no_reliability_block(self) -> None:
        code = self._run("--exit-zero")
        self.assertEqual(code, 0)
        payload = json.loads((self.tmp / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertNotIn("reliability", payload)

    def test_passk_k_too_large_is_a_clean_tool_error(self) -> None:
        # The least-recorded task has 3 runs; asking for pass^4 is a usage error (exit 2).
        code = self._run("--passk", "4")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
