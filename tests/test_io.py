import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.io import load_policy, load_trace
from plimsoll.models import ValidationError

FIXTURES = Path("examples")


class IoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-io-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_trace_orders_spans_and_metrics_ready(self) -> None:
        trace = load_trace(FIXTURES / "traces" / "current_ticket_triage.json")

        self.assertEqual(trace.case_id, "ticket-triage")
        self.assertEqual(trace.tool_sequence, ["read_ticket", "search_docs", "summarize"])
        self.assertEqual(trace.total_duration_ms, 840)

    def test_missing_policy_file_raises_a_validation_error_not_a_traceback(self) -> None:
        missing = self.tmp / "no-such-policy.json"

        with self.assertRaises(ValidationError) as ctx:
            load_policy(missing)

        self.assertIn("no-such-policy.json", str(ctx.exception))

    def test_policy_loads_sets(self) -> None:
        policy = load_policy(FIXTURES / "policies" / "default_policy.json")

        self.assertIn("read_ticket", policy.allowed_tools)
        self.assertIn("deploy", policy.forbidden_tools)
        self.assertEqual(policy.max_steps, 6)

    def test_invalid_trace_rejected(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text('{"run_id":"x"}', encoding="utf-8")

        with self.assertRaises(ValidationError):
            load_trace(bad)

    def test_invalid_policy_rejects_non_string_tool_lists(self) -> None:
        self.assert_policy_error({"allowed_tools": ["read_ticket", 3]}, "allowed_tools")

    def test_invalid_policy_rejects_non_positive_ints(self) -> None:
        for field in ["max_steps", "max_duration_ms", "max_tokens", "max_repeated_action_count"]:
            with self.subTest(field=field):
                self.assert_policy_error({field: 0}, field)
                self.assert_policy_error({field: True}, field)

    def test_invalid_policy_rejects_non_positive_cost(self) -> None:
        self.assert_policy_error({"max_estimated_cost_usd": 0}, "max_estimated_cost_usd")
        self.assert_policy_error({"max_estimated_cost_usd": "0.01"}, "max_estimated_cost_usd")

    def test_invalid_policy_rejects_expected_output_mode(self) -> None:
        self.assert_policy_error({"expected_output_mode": "fuzzy"}, "expected_output_mode")

    def test_invalid_policy_rejects_bad_regex_lists(self) -> None:
        self.assert_policy_error({"pii_patterns": ["["]}, "pii_patterns")
        self.assert_policy_error({"secret_patterns": [123]}, "secret_patterns")

    def test_invalid_policy_rejects_negative_distance(self) -> None:
        self.assert_policy_error({"max_tool_sequence_distance": -1}, "max_tool_sequence_distance")

    def test_invalid_policy_rejects_bad_trajectory_match_mode(self) -> None:
        self.assert_policy_error({"trajectory_match_mode": "fuzzy"}, "trajectory_match_mode")

    def test_invalid_policy_rejects_non_string_match_mode(self) -> None:
        # A non-string (e.g. a list) must raise ValidationError, not crash with TypeError.
        self.assert_policy_error({"trajectory_match_mode": ["subset"]}, "trajectory_match_mode")
        self.assert_policy_error({"expected_output_mode": ["contains"]}, "expected_output_mode")

    def test_invalid_policy_rejects_unknown_field(self) -> None:
        self.assert_policy_error({"max_step": 5}, "unknown policy field")

    def test_invalid_policy_rejects_bad_must_precede(self) -> None:
        self.assert_policy_error({"must_precede": "nope"}, "must_precede")
        self.assert_policy_error({"must_precede": [{"before": "a"}]}, "must_precede")

    def test_invalid_policy_rejects_self_referential_must_precede(self) -> None:
        self.assert_policy_error({"must_precede": [{"before": "x", "after": "x"}]}, "must_precede")

    def test_invalid_policy_rejects_must_precede_tool_outside_allowlist(self) -> None:
        self.assert_policy_error(
            {"allowed_tools": ["a", "b"], "must_precede": [{"before": "a", "after": "typo"}]},
            "must_precede",
        )

    def assert_policy_error(self, payload: dict[str, object], expected_field: str) -> None:
        path = self.tmp / "policy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, expected_field):
            load_policy(path)


if __name__ == "__main__":
    unittest.main()
