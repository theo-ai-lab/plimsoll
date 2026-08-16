"""`plimsoll corpus-score`: the external-corpus measurement as a real, runnable command.

A number nobody can re-run is a claim, not evidence. This locks the productized surface —
adapt an external corpus, replay the gate over it, print the confusion cells and BOTH
denominators — and pins the two behaviours that would let the command flatter itself:

  * unmappable records are surfaced in the output, never quietly excluded;
  * the exit code is 0 for a completed measurement, whatever the measurement says. The
    command reports a miss rate; it does not gate a build on one.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.cli import EXIT_ERROR, EXIT_OK, main

FIXTURE_CORPUS = Path(__file__).resolve().parents[1] / "examples" / "external-corpus" / "fixture-corpus"


class CorpusScoreCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-corpus-cli-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["corpus-score", *args])
        return code, out.getvalue(), err.getvalue()

    def test_scoring_the_bundled_fixture_corpus_prints_both_denominators(self) -> None:
        code, out, _ = self._run("--corpus", str(FIXTURE_CORPUS), "--json")

        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["denominators"]["total_records"], 8)
        self.assertEqual(payload["denominators"]["scored_records"], 6)
        self.assertEqual(payload["denominators"]["unmapped_records"], 2)
        self.assertEqual(payload["denominators"]["unsafe_total"], 4)
        self.assertEqual(payload["confusion"], {"caught": 2, "missed": 1, "false_reject": 1, "clean_allow": 2})
        self.assertAlmostEqual(payload["rates"]["miss_rate"], 1 / 3)
        self.assertAlmostEqual(payload["rates"]["worst_case_miss_rate"], 0.5)

    def test_the_human_summary_names_the_unmappable_records_it_could_not_see(self) -> None:
        _, out, _ = self._run("--corpus", str(FIXTURE_CORPUS))

        self.assertIn("unmappable", out)
        self.assertIn("worst-case miss rate", out)
        self.assertIn("no_tool_calls", out)

    def test_an_explicit_policy_file_overrides_the_derived_deny_list(self) -> None:
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"forbidden_tools": ["GmailSendEmail"]}), encoding="utf-8")

        _, out, _ = self._run("--corpus", str(FIXTURE_CORPUS), "--policy", str(policy), "--json")

        payload = json.loads(out)
        # Only the Gmail record is blocked now, so the Binance withdrawal becomes a miss and
        # the BankManagerSearchPayee over-block disappears.
        self.assertEqual(payload["confusion"], {"caught": 1, "missed": 2, "false_reject": 0, "clean_allow": 3})

    def test_a_measurement_that_looks_bad_still_exits_zero(self) -> None:
        """The command reports; it does not gate. A 100% miss rate is a result, not an error."""
        policy = self.tmp / "empty.json"
        policy.write_text("{}", encoding="utf-8")

        code, out, _ = self._run("--corpus", str(FIXTURE_CORPUS), "--policy", str(policy), "--json")

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["rates"]["miss_rate"], 1.0)

    def test_a_missing_corpus_is_a_usage_error_exit_2(self) -> None:
        code, _, err = self._run("--corpus", str(self.tmp / "nope"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("error:", err)

    def test_the_ledger_can_be_written_out_for_auditing(self) -> None:
        target = self.tmp / "score.json"
        code, _, _ = self._run("--corpus", str(FIXTURE_CORPUS), "--out", str(target), "-q")

        self.assertEqual(code, EXIT_OK)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["ledger"]), 6)
        self.assertEqual(
            sorted(row["cell"] for row in payload["ledger"]),
            ["caught", "caught", "clean_allow", "clean_allow", "false_reject", "missed"],
        )


if __name__ == "__main__":
    unittest.main()
