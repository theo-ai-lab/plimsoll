"""Whole-plan policy dry-run: gate an entire proposed plan before anything executes.

``Governor.dry_run_plan`` gates an entire proposed plan against the policy WITHOUT executing
a tool or spending a token. Within the gate's decidable rule subset it is exact (no false
negatives), so a planner can prune infeasible candidate trajectories before paying an
expensive model to score them. These tests pin the per-step decisions, the first-blocking-step
semantics, the deterministic score, and the `plimsoll governor --plan` CLI exit codes.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plimsoll.cli import main
from plimsoll.governor import Governor, ProposedToolCall
from plimsoll.models import Policy


def _policy() -> Policy:
    return Policy(
        allowed_tools={"search", "manager_review", "grant_access"},
        forbidden_tools={"delete_database"},
        must_precede=[("manager_review", "grant_access")],
        max_steps=4,
    )


class DryRunPlanTests(unittest.TestCase):
    def test_feasible_plan_scores_100_and_records_every_step(self) -> None:
        plan = ["search", "manager_review", "grant_access"]
        result = Governor(_policy()).dry_run_plan(plan)
        self.assertTrue(result.feasible)
        self.assertIsNone(result.blocking_step)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.plan_length, 3)
        self.assertEqual(len(result.decisions), 3)
        self.assertTrue(all(d.allowed for d in result.decisions))

    def test_ordering_violation_is_caught_before_anything_runs(self) -> None:
        # grant_access before its required manager_review: infeasible at step 1.
        plan = ["search", "grant_access", "manager_review"]
        result = Governor(_policy()).dry_run_plan(plan)
        self.assertFalse(result.feasible)
        self.assertEqual(result.blocking_step, 1)
        self.assertIn("tool_order", {f.rule_id for f in result.blocking_findings})
        # The plan got one of three steps in before blocking -> score 33.
        self.assertEqual(result.score, round(100 * 1 / 3))

    def test_first_blocking_step_wins_even_with_later_violations(self) -> None:
        plan = ["delete_database", "grant_access"]  # forbidden at step 0, ordering at step 1
        result = Governor(_policy()).dry_run_plan(plan)
        self.assertEqual(result.blocking_step, 0)
        self.assertEqual(result.score, 0)
        self.assertIn("forbidden_tool", {f.rule_id for f in result.blocking_findings})

    def test_budget_is_accumulated_across_the_plan(self) -> None:
        # max_steps = 4; a 5-step plan is infeasible the moment the budget is exceeded. Each
        # call carries a distinct input so the repeated-action rule does not fire first.
        plan = [{"tool": "search", "input": {"q": i}} for i in range(5)]
        result = Governor(_policy()).dry_run_plan(plan)
        self.assertFalse(result.feasible)
        self.assertEqual(result.blocking_step, 4)
        self.assertIn("max_steps", {f.rule_id for f in result.blocking_findings})

    def test_empty_plan_is_vacuously_feasible(self) -> None:
        result = Governor(_policy()).dry_run_plan([])
        self.assertTrue(result.feasible)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.plan_length, 0)

    def test_accepts_proposed_tool_call_objects_and_dicts(self) -> None:
        plan = [ProposedToolCall(tool="search"), {"tool": "manager_review"}, "grant_access"]
        result = Governor(_policy()).dry_run_plan(plan)
        self.assertTrue(result.feasible)

    def test_to_dict_is_serialisable_and_indexed(self) -> None:
        result = Governor(_policy()).dry_run_plan(["search", "grant_access"])
        payload = result.to_dict()
        json.dumps(payload)  # must be JSON-serialisable
        self.assertEqual([step["step"] for step in payload["steps"]], [0, 1])
        self.assertFalse(payload["feasible"])
        self.assertIn("infeasible", payload["summary"])

    def test_deterministic(self) -> None:
        plan = ["search", "grant_access", "manager_review"]
        self.assertEqual(
            Governor(_policy()).dry_run_plan(plan).to_dict(),
            Governor(_policy()).dry_run_plan(plan).to_dict(),
        )


class GovernorPlanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-plan-cli-"))
        self.policy = self.tmp / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "allowed_tools": ["search", "manager_review", "grant_access"],
                    "must_precede": [{"before": "manager_review", "after": "grant_access"}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_plan(self, name: str, plan: list) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["governor", *args])
        return code, out.getvalue(), err.getvalue()

    def test_feasible_plan_exits_zero(self) -> None:
        plan = self._write_plan("ok.json", ["search", "manager_review", "grant_access"])
        code, _, err = self._run("--policy", str(self.policy), "--plan", str(plan))
        self.assertEqual(code, 0)
        self.assertIn("feasible", err)

    def test_infeasible_plan_exits_one(self) -> None:
        plan = self._write_plan("bad.json", ["grant_access"])
        code, _, err = self._run("--policy", str(self.policy), "--plan", str(plan))
        self.assertEqual(code, 1)
        self.assertIn("infeasible", err)
        self.assertIn("tool_order", err)

    def test_json_output_is_machine_readable(self) -> None:
        plan = self._write_plan("bad.json", ["search", "grant_access"])
        code, out, _ = self._run("--policy", str(self.policy), "--plan", str(plan), "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["feasible"])
        self.assertEqual(payload["blocking_step"], 1)
        self.assertEqual(payload["plan_length"], 2)

    def test_plan_and_call_are_mutually_exclusive(self) -> None:
        plan = self._write_plan("ok.json", ["search"])
        call = self._write_plan("call.json", {"tool": "search"})
        code, _, err = self._run("--policy", str(self.policy), "--plan", str(plan), "--call", str(call))
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_plan_must_be_a_json_list(self) -> None:
        bad = self._write_plan("obj.json", {"tool": "search"})
        code, _, err = self._run("--policy", str(self.policy), "--plan", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("list", err)

    def test_quiet_suppresses_human_line(self) -> None:
        plan = self._write_plan("ok.json", ["search"])
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self._run("--policy", str(self.policy), "--plan", str(plan), "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
