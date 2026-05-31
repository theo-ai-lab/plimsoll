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
