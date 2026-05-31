import json
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from plimsoll.cli import main


class OutputFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-outputs-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_junit_output_for_passing_case(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/current_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--junit",
            ]
        )

        self.assertEqual(code, 0)
        root = ElementTree.parse(self.tmp / "report.junit.xml").getroot()
        self.assertEqual(root.attrib["tests"], "1")
        self.assertEqual(root.attrib["failures"], "0")
        self.assertEqual(root.find("testcase/failure"), None)

    def test_junit_output_for_failing_case(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/regressed_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--junit",
                "--fail-on-findings",
            ]
        )

        self.assertEqual(code, 1)
        root = ElementTree.parse(self.tmp / "report.junit.xml").getroot()
        failure = root.find("testcase/failure")
        self.assertEqual(root.attrib["tests"], "1")
        self.assertEqual(root.attrib["failures"], "1")
        self.assertIsNotNone(failure)
        self.assertIn("expected_output", failure.attrib["message"])
        self.assertIn("forbidden_tool", failure.text or "")

    def test_sarif_output_for_failing_case(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/regressed_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--sarif",
                "--fail-on-findings",
            ]
        )

        self.assertEqual(code, 1)
        payload = json.loads((self.tmp / "report.sarif.json").read_text(encoding="utf-8"))
        run = payload["runs"][0]
        rule_ids = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
        levels = {result["level"] for result in run["results"]}
        self.assertEqual(payload["version"], "2.1.0")
        self.assertIn("forbidden_tool", rule_ids)
        self.assertEqual(len(run["results"]), 9)
        self.assertIn("error", levels)
        self.assertIn("warning", levels)

    def test_sarif_anchors_to_committed_policy_with_line_regions(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/regressed_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--sarif",
                "--exit-zero",
            ]
        )

        self.assertEqual(code, 0)
        payload = json.loads((self.tmp / "report.sarif.json").read_text(encoding="utf-8"))
        results = payload["runs"][0]["results"]
        for result in results:
            location = result["locations"][0]["physicalLocation"]
            uri = location["artifactLocation"]["uri"]
            # Must anchor to a committed file (the policy), never a trace:// scheme.
            self.assertFalse(uri.startswith("trace://"))
            self.assertIn("default_policy.json", uri)
            self.assertGreaterEqual(location["region"]["startLine"], 1)
            self.assertIn("plimsoll/v1", result["partialFingerprints"])
        # forbidden_tools is on line 3 of the fixture policy, so its finding must not anchor to line 1.
        forbidden = next(result for result in results if result["ruleId"] == "forbidden_tool")
        self.assertGreater(forbidden["locations"][0]["physicalLocation"]["region"]["startLine"], 1)

    def test_report_json_includes_trajectory_diff(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/regressed_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--exit-zero",
            ]
        )

        self.assertEqual(code, 0)
        payload = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        diff = payload["cases"][0]["trajectory_diff"]
        self.assertEqual(diff["metrics_delta"]["steps"], 2)
        self.assertTrue(diff["tool_sequence"]["changes"])

    def test_html_report_includes_review_affordances(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/regressed_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--exit-zero",
            ]
        )

        self.assertEqual(code, 0)
        html = (self.tmp / "report.html").read_text(encoding="utf-8")
        self.assertIn("Finding Summary", html)
        self.assertIn("Why this needs review", html)
        self.assertIn("Trajectory Diff", html)
        self.assertIn('class="pill critical"', html)

    def test_markdown_report_written(self) -> None:
        code = main(
            [
                "run",
                "--input",
                "examples/traces/regressed_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.tmp),
                "--md",
                "--exit-zero",
            ]
        )

        self.assertEqual(code, 0)
        markdown = (self.tmp / "report.md").read_text(encoding="utf-8")
        self.assertIn("Plimsoll", markdown)
        self.assertIn("FAIL", markdown)
        self.assertIn("forbidden_tool", markdown)

    def test_github_step_summary_is_appended(self) -> None:
        import os

        summary_file = self.tmp / "step_summary.md"
        prior_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary_file)
        try:
            code = main(
                [
                    "run",
                    "--input",
                    "examples/traces/regressed_ticket_triage.json",
                    "--baseline",
                    "examples/traces/baseline_ticket_triage.json",
                    "--policy",
                    "examples/policies/default_policy.json",
                    "--out",
                    str(self.tmp),
                    "--exit-zero",
                ]
            )
        finally:
            if prior_summary is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = prior_summary

        self.assertEqual(code, 0)
        self.assertIn("Plimsoll", summary_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
