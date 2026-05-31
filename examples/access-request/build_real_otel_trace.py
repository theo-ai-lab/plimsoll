"""Capture REAL OpenTelemetry traces from a pydantic-ai agent run and serialize them
for Plimsoll's ``otel`` adapter.

Unlike the other fixtures (which are hand-authored to documented shapes), this script
runs the *actual* pydantic-ai library end to end and exports the spans its built-in
GenAI OpenTelemetry instrumentation emits. The model is a no-network ``FunctionModel``
that deterministically drives the tool sequence, so the run needs no API key, makes no
network call, and leaks no data — while the span tree, ``gen_ai.*`` attribute names,
tool calls, and token accounting are produced by the framework, not by us.

It is a DEV-ONLY generator. pydantic-ai and opentelemetry-sdk are NOT Plimsoll
runtime dependencies; install them only to regenerate these fixtures::

    pip install 'pydantic-ai>=1.0' 'opentelemetry-sdk>=1.30'
    python examples/access-request/build_real_otel_trace.py

The committed JSON is what Plimsoll actually ingests; nothing here runs in CI or in
the package. Span IDs and timestamps are normalized to fixed, deterministic values so
the committed fixture is byte-stable across regenerations; everything that carries
signal (span names, kinds, gen_ai attributes, tool sequence, token usage) is verbatim
from the instrumentation.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

AR = Path(__file__).resolve().parent
OUT = AR / "traces" / "real"

# Attributes worth committing: the structural / semantic-convention signal. Verbose
# message blobs (gen_ai.input.messages, tool_arguments, logfire.*) are dropped — they
# carry no rule signal and keep the fixture tidy and obviously free of payload data.
KEEP_ATTRS = {
    "gen_ai.operation.name",
    "gen_ai.tool.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.provider.name",
    "gen_ai.system",
    "gen_ai.agent.name",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
}

# Deterministic normalization constants (see module docstring).
BASE_NS = 1_700_000_000_000_000_000
STEP_NS = 10_000_000  # 10ms between span starts
DUR_NS = {"execute_tool": 2_000_000, "chat": 4_000_000, "invoke_agent": 0}


def _otlp_value(value: object) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _normalize(readable_spans: list, case_id: str, final_output: str, pyd_version: str) -> dict:
    spans = sorted(readable_spans, key=lambda s: s.start_time)
    out_spans = []
    for index, span in enumerate(spans):
        attrs = dict(span.attributes or {})
        op = attrs.get("gen_ai.operation.name", "")
        start = BASE_NS + index * STEP_NS
        # The root agent span encloses all children; tool/chat spans get fixed durations.
        duration = (len(spans) * STEP_NS) if op == "invoke_agent" else DUR_NS.get(op, 1_000_000)
        kept = [{"key": key, "value": _otlp_value(attrs[key])} for key in sorted(attrs) if key in KEEP_ATTRS]
        out_spans.append(
            {
                "traceId": "00000000000000000000000000000001",
                "spanId": f"{index + 1:016x}",
                "name": span.name,
                "kind": f"SPAN_KIND_{span.kind.name}",
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(start + duration),
                "status": {"code": "STATUS_CODE_OK"},
                "attributes": kept,
            }
        )
    return {
        "plimsoll": {
            "case_id": case_id,
            "run_id": case_id,
            "final_output": final_output,
        },
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "access-request-agent"}},
                        {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "pydantic-ai", "version": pyd_version},
                        "spans": out_spans,
                    }
                ],
            }
        ],
    }


def _generate(order: list[str], final_output: str, case_id: str) -> dict:
    import pydantic_ai
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.instrumented import InstrumentationSettings

    def model_fn(messages, info):
        returned = [
            part.tool_name
            for message in messages
            for part in getattr(message, "parts", [])
            if getattr(part, "part_kind", None) == "tool-return"
        ]
        for tool in order:
            if tool not in returned:
                return ModelResponse(parts=[ToolCallPart(tool_name=tool, args={})])
        return ModelResponse(parts=[TextPart(content=final_output)])

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = InstrumentationSettings(tracer_provider=provider, version=2)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # silence the instrument= deprecation note
        agent = Agent(FunctionModel(model_fn), instrument=settings, name="access_request_agent")

        @agent.tool_plain
        def manager_review() -> str:
            return "manager approved the access request"

        @agent.tool_plain
        def security_review() -> str:
            return "security cleared the access request"

        @agent.tool_plain
        def grant_access() -> str:
            return "access granted to the requested system"

        @agent.tool_plain
        def escalate() -> str:
            return "escalated to a human approver"

        agent.run_sync("Grant access to the billing dashboard for user u-42.")

    return _normalize(list(exporter.get_finished_spans()), case_id, final_output, pydantic_ai.__version__)


def main() -> int:
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        import pydantic_ai  # noqa: F401
    except ImportError:
        print(
            "SKIP: pydantic-ai / opentelemetry-sdk not installed. "
            "Run `pip install 'pydantic-ai>=1.0' 'opentelemetry-sdk>=1.30'` to regenerate.",
        )
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    cases = {
        "clean": (
            ["manager_review", "security_review", "grant_access"],
            "Access granted to the billing dashboard for u-42 after manager and security review.",
        ),
        "bypass": (
            ["manager_review", "grant_access"],
            "Access granted to the billing dashboard for u-42.",
        ),
    }
    for name, (order, final_output) in cases.items():
        payload = _generate(order, final_output, case_id=f"access-request-real-{name}")
        path = OUT / f"{name}.otel.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tools = [
            attr["value"]["stringValue"]
            for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
            for attr in span["attributes"]
            if attr["key"] == "gen_ai.tool.name"
        ]
        print(f"wrote {path.relative_to(AR.parent.parent)} — tool calls: {tools}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
