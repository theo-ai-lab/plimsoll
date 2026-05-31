import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.cli import main
from plimsoll.io import load_policy, load_trace
from plimsoll.models import ValidationError
from plimsoll.otel import load_otel_trace
from plimsoll.rules import evaluate_trace, trace_metrics

FIXTURES = Path("examples")


class OpenTelemetryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-otel-"))
        self.cli_out = self.tmp / "otel-cli"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loads_otel_fixture(self) -> None:
        trace = load_otel_trace(FIXTURES / "traces" / "otel_ticket_triage.json")

        self.assertEqual(trace.case_id, "ticket-triage")
        self.assertEqual(trace.run_id, "otel-candidate-2026-05-17")
        self.assertEqual(trace.tool_sequence, ["read_ticket", "search_docs", "summarize"])
        self.assertEqual(trace.total_duration_ms, 840)

    def test_otel_token_metrics_map_to_existing_rules(self) -> None:
        trace = load_otel_trace(FIXTURES / "traces" / "otel_ticket_triage.json")
        metrics = trace_metrics(trace)

        self.assertEqual(metrics["tokens"], 355)
        self.assertEqual(metrics["estimated_cost_usd"], 0.0037)

    def test_policy_evaluation_works_on_otel_trace(self) -> None:
        policy = load_policy(FIXTURES / "policies" / "default_policy.json")
        baseline = load_trace(FIXTURES / "traces" / "baseline_ticket_triage.json")
        trace = load_otel_trace(FIXTURES / "traces" / "otel_ticket_triage.json")

        self.assertEqual(evaluate_trace(trace, policy, baseline), [])

    def test_cli_runs_otel_input_with_native_baseline(self) -> None:
        code = main(
            [
                "run",
                "--format",
                "otel",
                "--input",
                "examples/traces/otel_ticket_triage.json",
                "--baseline",
                "examples/traces/baseline_ticket_triage.json",
                "--baseline-format",
                "native",
                "--policy",
                "examples/policies/default_policy.json",
                "--out",
                str(self.cli_out),
            ]
        )

        self.assertEqual(code, 0)
        self.assertTrue((self.cli_out / "report.json").exists())

    def test_realish_adapter_corpus_passes_policy(self) -> None:
        cases = [
            ("langgraph", "examples/traces/langgraph_ticket_triage.json"),
            ("openai-agents", "examples/traces/openai_agents_ticket_triage.json"),
            ("openinference", "examples/traces/openinference_ticket_triage.json"),
        ]
        for trace_format, fixture in cases:
            with self.subTest(trace_format=trace_format):
                code = main(
                    [
                        "run",
                        "--format",
                        trace_format,
                        "--input",
                        fixture,
                        "--baseline",
                        "examples/traces/baseline_ticket_triage.json",
                        "--baseline-format",
                        "native",
                        "--policy",
                        "examples/policies/default_policy.json",
                        "--out",
                        str(self.cli_out),
                    ]
                )
                self.assertEqual(code, 0)

    def test_missing_required_otel_field_fails_clearly(self) -> None:
        path = self.tmp / "bad_otel.json"
        path.write_text(
            '{"spans":[{"traceId":"t1","spanId":"s1","name":"x","attributes":{"plimsoll.final_output":"done"}}]}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValidationError, "startTimeUnixNano"):
            load_otel_trace(path)


if __name__ == "__main__":
    unittest.main()
