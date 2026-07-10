from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plimsoll.io import iter_dir, load_json
from plimsoll.models import Span, TraceRun, ValidationError


def load_otel_trace(path: Path) -> TraceRun:
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: OpenTelemetry input must be a JSON object")
    spans = _extract_spans(data, source=str(path))
    if not spans:
        raise ValidationError(f"{path}: OpenTelemetry input must contain at least one span")

    normalized_attrs = [_attributes(span.get("attributes", {}), source=str(path)) for span in spans]
    merged = _merged_plimsoll_attrs(data, normalized_attrs)
    case_id = (
        _string_attr(merged, "plimsoll.case_id") or _string_attr(merged, "case.id") or _trace_id(spans[0], str(path))
    )
    run_id = _string_attr(merged, "plimsoll.run_id") or _trace_id(spans[0], str(path))
    final_output = _string_attr(merged, "plimsoll.final_output")
    if final_output is None:
        raise ValidationError(f"{path}: OpenTelemetry input missing 'plimsoll.final_output'")

    starts = [_timestamp_ms(span, "startTimeUnixNano", "start_time_unix_nano", str(path)) for span in spans]
    base_ms = min(starts)
    parsed_spans = [
        _parse_span(span, attrs, base_ms, source=str(path), index=index)
        for index, (span, attrs) in enumerate(zip(spans, normalized_attrs))
    ]
    return TraceRun(
        run_id=run_id,
        case_id=case_id,
        final_output=final_output,
        expected_output=_string_attr(merged, "plimsoll.expected_output"),
        spans=sorted(parsed_spans, key=lambda span: (span.start_ms, span.end_ms, span.span_id)),
        metadata={"format": "otel", "trace_id": run_id},
    )


def load_otel_traces(path: Path) -> list[TraceRun]:
    if path.is_file():
        return [load_otel_trace(path)]
    if not path.is_dir():
        raise ValidationError(f"{path}: expected an OpenTelemetry JSON file or directory")
    traces = [load_otel_trace(item) for item in iter_dir(path) if item.suffix == ".json"]
    if not traces:
        raise ValidationError(f"{path}: no .json OpenTelemetry traces found")
    return traces


def _extract_spans(data: dict[str, Any], source: str) -> list[dict[str, Any]]:
    if isinstance(data.get("spans"), list):
        return _span_objects(data["spans"], source)
    spans: list[dict[str, Any]] = []
    for resource_span in data.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            spans.extend(_span_objects(scope_span.get("spans", []), source))
    if spans:
        return spans
    for resource_span in data.get("resource_spans", []):
        for scope_span in resource_span.get("scope_spans", []):
            spans.extend(_span_objects(scope_span.get("spans", []), source))
    return spans


def _span_objects(value: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValidationError(f"{source}: OpenTelemetry spans must be a list")
    if not all(isinstance(span, dict) for span in value):
        raise ValidationError(f"{source}: each OpenTelemetry span must be an object")
    return value


def _parse_span(span: dict[str, Any], attrs: dict[str, Any], base_ms: int, source: str, index: int) -> Span:
    span_id = _required_string(span, ["spanId", "span_id"], source, index)
    name = _required_string(span, ["name"], source, index)
    start_ms = _timestamp_ms(span, "startTimeUnixNano", "start_time_unix_nano", source) - base_ms
    end_ms = _timestamp_ms(span, "endTimeUnixNano", "end_time_unix_nano", source) - base_ms
    if end_ms < start_ms:
        raise ValidationError(f"{source}: OpenTelemetry span {index} has end before start")
    return Span(
        span_id=span_id,
        name=name,
        kind=str(span.get("kind") or attrs.get("span.kind") or "internal").lower(),
        status=_status(span),
        start_ms=start_ms,
        end_ms=end_ms,
        tool_name=_tool_name(name, attrs),
        input=attrs.get("plimsoll.input") or attrs.get("gen_ai.input.messages"),
        output=attrs.get("plimsoll.output") or attrs.get("gen_ai.output.messages"),
        error=_error(span, attrs),
        attributes=attrs,
    )


def _attributes(value: Any, source: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        raise ValidationError(f"{source}: OpenTelemetry span attributes must be an object or key/value list")
    attrs: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ValidationError(f"{source}: each OpenTelemetry attribute must have a string key")
        attrs[item["key"]] = _attribute_value(item.get("value"))
    _normalize_openinference_attrs(attrs)
    return attrs


def _normalize_openinference_attrs(attrs: dict[str, Any]) -> None:
    if "gen_ai.usage.input_tokens" not in attrs and "llm.token_count.prompt" in attrs:
        attrs["gen_ai.usage.input_tokens"] = attrs["llm.token_count.prompt"]
    if "gen_ai.usage.output_tokens" not in attrs and "llm.token_count.completion" in attrs:
        attrs["gen_ai.usage.output_tokens"] = attrs["llm.token_count.completion"]
    if "tool.name" not in attrs and "openinference.tool.name" in attrs:
        attrs["tool.name"] = attrs["openinference.tool.name"]


def _attribute_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ["stringValue", "intValue", "doubleValue", "boolValue", "arrayValue", "kvlistValue"]:
            if key in value:
                return value[key]
        for key in ["string_value", "int_value", "double_value", "bool_value", "array_value", "kvlist_value"]:
            if key in value:
                return value[key]
    return value


def _merged_plimsoll_attrs(data: dict[str, Any], span_attrs: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    top = data.get("plimsoll") or {}
    if isinstance(top, dict):
        for key, value in top.items():
            merged[f"plimsoll.{key}"] = value
    for attrs in span_attrs:
        for key, value in attrs.items():
            if key.startswith("plimsoll.") or key == "case.id":
                merged.setdefault(key, value)
    return merged


def _tool_name(name: str, attrs: dict[str, Any]) -> str | None:
    # A span is a tool/step when it explicitly names a tool, is an OpenInference
    # tool/LLM/retriever span, or is an OTel `execute_tool` operation. Generic gen_ai
    # operations like `chat` or `invoke_agent` are model/agent activity, not tools, so
    # `gen_ai.operation.name` is NOT treated as a tool name on its own — otherwise a
    # real agent export (which emits a `chat` span per turn) tags every LLM turn as a
    # tool and floods the tool sequence with phantom `chat`/`invoke_agent` steps.
    for key in ["gen_ai.tool.name", "tool.name", "plimsoll.tool_name"]:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    if attrs.get("openinference.span.kind") in {"TOOL", "LLM", "RETRIEVER"}:
        return str(attrs.get("tool.name") or attrs.get("gen_ai.operation.name") or name)
    if str(attrs.get("gen_ai.operation.name")) == "execute_tool":
        return name
    return None


def _status(span: dict[str, Any]) -> str:
    status = span.get("status") or {}
    if isinstance(status, dict):
        code = str(status.get("code") or "").lower()
        if code in {"error", "status_code_error", "2"}:
            return "error"
    return "ok"


def _error(span: dict[str, Any], attrs: dict[str, Any]) -> str | None:
    status = span.get("status") or {}
    if isinstance(status, dict) and status.get("message"):
        return str(status["message"])
    error_type = attrs.get("error.type")
    return str(error_type) if error_type else None


def _timestamp_ms(span: dict[str, Any], camel_key: str, snake_key: str, source: str) -> int:
    value = span.get(camel_key, span.get(snake_key))
    if value is None:
        raise ValidationError(f"{source}: OpenTelemetry span missing '{camel_key}'")
    if isinstance(value, int):
        return value // 1_000_000
    if isinstance(value, str) and value.isdigit():
        return int(value) // 1_000_000
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(text).astimezone(UTC).timestamp() * 1000)
        except ValueError as exc:
            raise ValidationError(f"{source}: invalid OpenTelemetry timestamp '{value}'") from exc
    raise ValidationError(f"{source}: OpenTelemetry timestamp must be nanoseconds or ISO-8601 string")


def _trace_id(span: dict[str, Any], source: str) -> str:
    return _required_string(span, ["traceId", "trace_id"], source, 0)


def _required_string(span: dict[str, Any], keys: list[str], source: str, index: int) -> str:
    for key in keys:
        value = span.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValidationError(f"{source}: OpenTelemetry span {index} missing '{keys[0]}'")


def _string_attr(attrs: dict[str, Any], key: str) -> str | None:
    value = attrs.get(key)
    return value if isinstance(value, str) and value else None
