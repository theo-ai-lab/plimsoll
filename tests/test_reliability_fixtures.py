"""Lock the committed pass^k reliability fixtures that arm the CI gate.

`examples/reliability/` holds two directories, each with three recorded runs of the same
case_id (`k = 3`): a stable agent (every run passes) and a flaky one (a run bypasses the
required approval). The committed CI workflow runs both as a self-test. These tests pin the
end-to-end behavior — stable PASSES the gate, flaky FAILS it — so the fixtures cannot rot.

Tests run from the repository root (like the rest of the suite), so the example paths resolve.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.cli import main

RELIABILITY = Path("examples/reliability")
POLICY = RELIABILITY / "policy.json"


class ReliabilityFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-reliability-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, fixture: str, *extra: str) -> tuple[int, dict]:
        out = self.tmp / fixture
        code = main(
            [
                "run",
                "--input",
                str(RELIABILITY / fixture),
                "--policy",
                str(POLICY),
                "--out",
                str(out),
                "--quiet",
                *extra,
            ]
        )
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        return code, report

    def test_stable_fixture_passes_the_passk_gate(self) -> None:
        code, report = self._run("stable", "--passk-threshold", "0.9")
        self.assertEqual(code, 0)
        reliability = report["reliability"]
        self.assertEqual(reliability["k"], 3)
        self.assertEqual(reliability["tasks"], 1)
        self.assertEqual(reliability["total_runs"], 3)
        self.assertEqual(reliability["gate"], "pass")
        self.assertEqual(reliability["pass_hat_k"], 1.0)
        # No case failed, so nothing else fails the build either.
        self.assertEqual(report["summary"]["failed"], 0)

    def test_flaky_fixture_fails_the_passk_gate(self) -> None:
        code, report = self._run("flaky", "--passk-threshold", "0.9", "--sarif")
        self.assertEqual(code, 1)
        reliability = report["reliability"]
        self.assertEqual(reliability["k"], 3)
        self.assertEqual(reliability["gate"], "fail")
        # One of three runs bypassed the approval, so not all 3 passed -> pass^3 = 0.
        self.assertEqual(reliability["pass_hat_k"], 0.0)
        self.assertAlmostEqual(reliability["pass_at_1"], 2 / 3, places=5)
        # The reliability finding is carried into SARIF alongside the per-run tool_order finding.
        sarif = json.loads((self.tmp / "flaky" / "report.sarif.json").read_text(encoding="utf-8"))
        rule_ids = {result["ruleId"] for result in sarif["runs"][0]["results"]}
        self.assertIn("reliability_pass_k", rule_ids)
        self.assertIn("tool_order", rule_ids)

    def test_default_k_is_above_one_so_the_gate_has_repeat_data(self) -> None:
        # Without an explicit --passk, k defaults to the min runs-per-task. The fixtures record
        # the same case 3 times, so the gate genuinely evaluates k > 1 (not a single run).
        _, report = self._run("stable", "--passk-threshold", "0.9")
        self.assertGreater(report["reliability"]["k"], 1)


if __name__ == "__main__":
    unittest.main()
