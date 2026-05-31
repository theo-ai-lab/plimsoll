from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plimsoll.io import load_json
from plimsoll.models import Span, TraceRun, ValidationError
from plimsoll.otel import load_otel_trace


def load_adapter_traces(path: Path, trace_format: str) -> list[TraceRun]:
    if path.is_file():
        return [load_adapter_trace(path, trace_format)]
    if not path.is_dir():
        raise ValidationError(f"{path}: expected a trace file or directory")
    traces = [load_adapter_trace(item, trace_format) for item in sorted(path.iterdir()) if item.suffix == ".json"]
    if not traces:
        raise ValidationError(f"{path}: no .json traces found")
    return traces


def load_adapter_trace(path: Path, trace_format: str) -> TraceRun:
    if trace_format == "openinference":
        return load_otel_trace(path)
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: {trace_format} input must be a JSON object")
    if trace_format == "langgraph":
        return _load_langgraph(data, str(path))
    if trace_format == "openai-agents":
        return _load_openai_agents(data, str(path))
    raise ValidationError(f"unsupported adapter format: {trace_format}")


def _load_langgraph(data: dict[str, Any], source: str) -> TraceRun:
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise ValidationError(f"{source}: LangGraph-style input must contain a non-empty events list")
    spans = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValidationError(f"{source}: LangGraph event {index} must be an object")
        start = _int_field(event, ["start_ms", "started_at_ms"], source, index)
        end = _int_field(event, ["end_ms", "ended_at_ms"], source, index)
        spans.append(
            Span(
                span_id=str(event.get("span_id") or event.get("id") or f"event-{index + 1}"),
                name=_string_field(event, ["name", "node"], source, index),
                kind=str(event.get("kind") or event.get("type") or "internal"),
                status=str(event.get("status") or "ok"),
                start_ms=start,
                end_ms=end,
                tool_name=event.get("tool") or event.get("tool_name") or event.get("node"),
                input=event.get("input"),
                output=event.get("output"),
                error=event.get("error"),
                attributes=event.get("attributes") or {},
            )
        )
    return TraceRun(
        run_id=_top_string(data, "run_id", source),
        case_id=_top_string(data, "case_id", source),
        expected_output=data.get("expected_output"),
        final_output=_top_string(data, "final_output", source),
        spans=spans,
        metadata={"format": "langgraph"},
    )


def _load_openai_agents(data: dict[str, Any], source: str) -> TraceRun:
    spans_data = data.get("spans")
    metadata = data.get("metadata") or {}
    if not isinstance(spans_data, list) or not spans_data:
        raise ValidationError(f"{source}: OpenAI Agents-style input must contain a non-empty spans list")
    if not isinstance(metadata, dict):
        raise ValidationError(f"{source}: metadata must be an object")
    starts = [_time_ms(span, ["started_at", "start_time"], source, index) for index, span in enumerate(spans_data)]
    base = min(starts)
    spans = []
    for index, span in enumerate(spans_data):
        if not isinstance(span, dict):
            raise ValidationError(f"{source}: OpenAI Agents span {index} must be an object")
        data_field = span.get("data") or span.get("span_data") or {}
        if not isinstance(data_field, dict):
            raise ValidationError(f"{source}: OpenAI Agents span {index} data must be an object")
        attrs = dict(data_field)
        usage = data_field.get("usage") or {}
        if isinstance(usage, dict):
            attrs["gen_ai.usage.input_tokens"] = usage.get("input_tokens", usage.get("prompt_tokens", 0))
            attrs["gen_ai.usage.output_tokens"] = usage.get("output_tokens", usage.get("completion_tokens", 0))
        spans.append(
            Span(
                span_id=_string_field(span, ["span_id", "id"], source, index),
                name=_string_field(span, ["name"], source, index),
                kind=str(span.get("span_type") or span.get("type") or "internal"),
                status=str(span.get("status") or "ok"),
                start_ms=_time_ms(span, ["started_at", "start_time"], source, index) - base,
                end_ms=_time_ms(span, ["ended_at", "end_time"], source, index) - base,
                tool_name=data_field.get("tool_name") or data_field.get("name"),
                input=data_field.get("input"),
                output=data_field.get("output"),
                error=data_field.get("error"),
                attributes=attrs,
            )
        )
    return TraceRun(
        run_id=str(data.get("trace_id") or metadata.get("run_id") or source),
        case_id=str(metadata.get("case_id") or data.get("case_id") or data.get("trace_id") or source),
        expected_output=metadata.get("expected_output") or data.get("expected_output"),
        final_output=str(metadata.get("final_output") or data.get("final_output") or ""),
        spans=spans,
        metadata={"format": "openai-agents"},
    )


def _top_string(data: dict[str, Any], field: str, source: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{source}: missing string field '{field}'")
    return value


def _string_field(data: dict[str, Any], fields: list[str], source: str, index: int) -> str:
    for field in fields:
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    raise ValidationError(f"{source}: item {index} missing string field '{fields[0]}'")


def _int_field(data: dict[str, Any], fields: list[str], source: str, index: int) -> int:
    for field in fields:
        value = data.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise ValidationError(f"{source}: item {index} missing integer field '{fields[0]}'")


def _time_ms(data: dict[str, Any], fields: list[str], source: str, index: int) -> int:
    for field in fields:
        value = data.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        if isinstance(value, str):
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp() * 1000)
            except ValueError as exc:
                raise ValidationError(f"{source}: item {index} has invalid time '{value}'") from exc
    raise ValidationError(f"{source}: item {index} missing time field '{fields[0]}'")
