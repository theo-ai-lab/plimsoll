import unittest
from pathlib import Path

from plimsoll.diff import step_diff, trajectory_diff
from plimsoll.io import load_trace

ROOT = Path(__file__).resolve().parent.parent
AR = ROOT / "examples" / "access-request"


class StepDiffTests(unittest.TestCase):
    def test_deltas_are_signed_and_honest(self) -> None:
        baseline = [
            {"tool": "a", "duration_ms": 100, "tokens": 50},
            {"tool": "b", "duration_ms": 120, "tokens": 90},
        ]
        current = [{"tool": "a", "duration_ms": 110, "tokens": 50}]
        steps = step_diff(baseline, current)
        # 'a' present in both with a real +10ms delta; 'b' dropped, so its delta is negative-its-cost.
        self.assertEqual(steps[0], {"tool": "a", "op": "equal", "delta_ms": 10, "delta_tokens": 0})
        self.assertEqual(steps[1], {"tool": "b", "op": "delete", "delta_ms": -120, "delta_tokens": -90})

    def test_inserted_step_has_positive_delta(self) -> None:
        steps = step_diff([], [{"tool": "x", "duration_ms": 80, "tokens": 40}])
        self.assertEqual(steps, [{"tool": "x", "op": "insert", "delta_ms": 80, "delta_tokens": 40}])

    def test_access_request_failed_drops_the_required_reviews(self) -> None:
        baseline = load_trace(AR / "traces" / "clean.trace.json")
        failed = load_trace(AR / "traces" / "failed.trace.json")
        steps = trajectory_diff(failed, baseline)["steps"]
        dropped = {s["tool"] for s in steps if s["op"] == "delete"}
        self.assertEqual(dropped, {"manager_review", "security_review"})
        for step in steps:
            if step["op"] == "delete":
                self.assertLess(step["delta_ms"], 0)
                self.assertLess(step["delta_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
