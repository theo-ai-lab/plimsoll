import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-cli-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_writes_reports(self) -> None:
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
            ]
        )

        self.assertEqual(code, 0)
        report = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertTrue((self.tmp / "report.html").exists())

    def test_cli_can_fail_on_findings(self) -> None:
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
                "--fail-on-findings",
            ]
        )

        self.assertEqual(code, 1)
        report = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["failed"], 1)

    def test_run_exits_nonzero_on_findings_by_default(self) -> None:
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
            ]
        )

        self.assertEqual(code, 1)

    def test_exit_zero_forces_success_exit(self) -> None:
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
        report = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["failed"], 1)

    def test_human_summary_formats_finding_counts_in_plain_english(self) -> None:
        import contextlib
        import io as string_io

        buffer = string_io.StringIO()
        with contextlib.redirect_stderr(buffer):
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
                    "--color",
                    "never",
                ]
            )

        self.assertEqual(code, 1)
        summary_line = buffer.getvalue().splitlines()[0]
        self.assertIn("findings: 3 critical, 3 high, 3 medium", summary_line)
        self.assertNotIn("{", summary_line, "severity counts must not print as a Python dict repr")

    def test_human_summary_says_none_when_clean(self) -> None:
        import contextlib
        import io as string_io

        buffer = string_io.StringIO()
        with contextlib.redirect_stderr(buffer):
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
                    "--color",
                    "never",
                ]
            )

        self.assertEqual(code, 0)
        summary_line = buffer.getvalue().splitlines()[0]
        self.assertIn("findings: none", summary_line)

    def test_missing_policy_file_is_a_clean_usage_error(self) -> None:
        import contextlib
        import io as string_io

        buffer = string_io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(
                [
                    "run",
                    "--input",
                    "examples/traces/current_ticket_triage.json",
                    "--policy",
                    str(self.tmp / "no-such-policy.json"),
                    "--out",
                    str(self.tmp),
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("error:", buffer.getvalue())
        self.assertIn("no-such-policy.json", buffer.getvalue())

    def test_json_summary_is_machine_readable(self) -> None:
        import contextlib
        import io as string_io

        buffer = string_io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(
                [
                    "run",
                    "--input",
                    "examples/traces/current_ticket_triage.json",
                    "--policy",
                    "examples/policies/default_policy.json",
                    "--out",
                    str(self.tmp),
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["summary"]["passed"], 1)


if __name__ == "__main__":
    unittest.main()
