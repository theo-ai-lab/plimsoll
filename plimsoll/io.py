from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from plimsoll.models import Policy, Span, TraceRun, ValidationError


def load_json(path: Path) -> Any:
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc


def _read_text(path: Path) -> str:
    """Read a file, turning OS-level failures into the CLI's clean usage-error contract."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"{path}: cannot read file: {exc.strerror or exc}") from exc


def iter_dir(path: Path) -> list[Path]:
    """List a directory sorted, with the same clean usage-error contract as ``_read_text``.

    Every directory-of-traces loader (native, OTel, adapters) goes through this, so an
    unlistable directory reports as a usage error instead of an unhandled traceback.
    """
    try:
        return sorted(path.iterdir())
    except OSError as exc:
        raise ValidationError(f"{path}: cannot read directory: {exc.strerror or exc}") from exc


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{path}:{line_no}: invalid JSONL: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValidationError(f"{path}:{line_no}: each JSONL row must be an object")
        rows.append(row)
    return rows


def load_trace(path: Path) -> TraceRun:
    data = load_json(path) if path.suffix == ".json" else _single_jsonl_run(path)
    return parse_trace(data, source=str(path))


def load_traces(path: Path) -> list[TraceRun]:
    if path.is_file():
        return [load_trace(path)]
    if not path.is_dir():
        raise ValidationError(f"{path}: expected a trace file or directory")
    traces: list[TraceRun] = []
    for item in iter_dir(path):
        if item.suffix in {".json", ".jsonl"}:
            traces.append(load_trace(item))
    if not traces:
        raise ValidationError(f"{path}: no .json or .jsonl traces found")
    return traces


def _single_jsonl_run(path: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    if not rows:
        raise ValidationError(f"{path}: empty JSONL trace")
    meta = rows[0] if rows[0].get("event") == "run" else {}
    spans = [row for row in rows if row.get("event") == "span"]
    return {
        "run_id": meta.get("run_id", path.stem),
        "case_id": meta.get("case_id", path.stem),
        "final_output": meta.get("final_output", ""),
        "expected_output": meta.get("expected_output"),
        "metadata": {key: value for key, value in meta.items() if key not in {"event", "spans"}},
        "spans": spans,
    }


def parse_trace(data: dict[str, Any], source: str = "<memory>") -> TraceRun:
    required = ["run_id", "case_id", "final_output", "spans"]
    for key in required:
        if key not in data:
            raise ValidationError(f"{source}: missing required trace field '{key}'")
    if not isinstance(data["spans"], list):
        raise ValidationError(f"{source}: spans must be a list")
    spans = [parse_span(span, source=source, index=index) for index, span in enumerate(data["spans"])]
    if not spans:
        raise ValidationError(f"{source}: trace must contain at least one span")
    return TraceRun(
        run_id=_string(data["run_id"], "run_id", source),
        case_id=_string(data["case_id"], "case_id", source),
        final_output=_string(data["final_output"], "final_output", source),
        expected_output=data.get("expected_output"),
        spans=sorted(spans, key=lambda span: (span.start_ms, span.end_ms, span.span_id)),
        metadata=data.get("metadata") or {},
    )


def parse_span(data: dict[str, Any], source: str, index: int) -> Span:
    if not isinstance(data, dict):
        raise ValidationError(f"{source}: span {index} must be an object")
    for key in ["span_id", "name", "kind", "status", "start_ms", "end_ms"]:
        if key not in data:
            raise ValidationError(f"{source}: span {index} missing '{key}'")
    start_ms = _int(data["start_ms"], "start_ms", source)
    end_ms = _int(data["end_ms"], "end_ms", source)
    if end_ms < start_ms:
        raise ValidationError(f"{source}: span {index} has end_ms before start_ms")
    attributes = data.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise ValidationError(f"{source}: span {index} attributes must be an object")
    return Span(
        span_id=_string(data["span_id"], "span_id", source),
        name=_string(data["name"], "name", source),
        kind=_string(data["kind"], "kind", source),
        status=_string(data["status"], "status", source),
        start_ms=start_ms,
        end_ms=end_ms,
        tool_name=data.get("tool_name"),
        input=data.get("input"),
        output=data.get("output"),
        error=data.get("error"),
        attributes=attributes,
    )


_KNOWN_POLICY_KEYS = {
    "$schema",
    "version",
    "allowed_tools",
    "forbidden_tools",
    "required_tools",
    "max_steps",
    "max_duration_ms",
    "max_tokens",
    "max_estimated_cost_usd",
    "max_repeated_action_count",
    "pii_patterns",
    "secret_patterns",
    "expected_output_mode",
    "max_tool_sequence_distance",
    "trajectory_match_mode",
    "must_precede",
}


def load_policy(path: Path | None) -> Policy:
    if path is None:
        return Policy()
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: policy must be a JSON object")
    source = str(path)
    unknown = sorted(key for key in data if key not in _KNOWN_POLICY_KEYS)
    if unknown:
        raise ValidationError(f"{source}: unknown policy field(s): {', '.join(unknown)}")
    allowed_tools = set(_optional_string_list(data, "allowed_tools", source))
    must_precede = _optional_tool_order(data, source)
    if allowed_tools:
        referenced = {tool for pair in must_precede for tool in pair}
        missing = sorted(referenced - allowed_tools)
        if missing:
            raise ValidationError(
                f"{source}: 'must_precede' references tools not in allowed_tools: {', '.join(missing)}"
            )
    return Policy(
        allowed_tools=allowed_tools,
        forbidden_tools=set(_optional_string_list(data, "forbidden_tools", source)),
        required_tools=set(_optional_string_list(data, "required_tools", source)),
        max_steps=_optional_positive_int(data, "max_steps", source),
        max_duration_ms=_optional_positive_int(data, "max_duration_ms", source),
        max_tokens=_optional_positive_int(data, "max_tokens", source),
        max_estimated_cost_usd=_optional_positive_number(data, "max_estimated_cost_usd", source),
        max_repeated_action_count=_optional_positive_int(data, "max_repeated_action_count", source, default=1),
        pii_patterns=_optional_regex_list(data, "pii_patterns", source),
        secret_patterns=_optional_regex_list(data, "secret_patterns", source),
        expected_output_mode=_expected_output_mode(data, source),
        max_tool_sequence_distance=_optional_nonnegative_int(data, "max_tool_sequence_distance", source),
        trajectory_match_mode=_optional_trajectory_match_mode(data, source),
        must_precede=must_precede,
    )


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _string(value: Any, field: str, source: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{source}: '{field}' must be a string")
    return value


def _int(value: Any, field: str, source: str) -> int:
    if not isinstance(value, int):
        raise ValidationError(f"{source}: '{field}' must be an integer")
    return value


def _optional_string_list(data: dict[str, Any], field: str, source: str) -> list[str]:
    if field not in data:
        return []
    value = data[field]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{source}: '{field}' must be a list of strings")
    return value


def _optional_regex_list(data: dict[str, Any], field: str, source: str) -> list[str]:
    values = _optional_string_list(data, field, source)
    for index, pattern in enumerate(values):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValidationError(f"{source}: '{field}' item {index} is not a valid regex: {exc}") from exc
    return values


def _optional_positive_int(data: dict[str, Any], field: str, source: str, default: int | None = None) -> int | None:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{source}: '{field}' must be a positive integer")
    return value


def _optional_nonnegative_int(data: dict[str, Any], field: str, source: str) -> int | None:
    if field not in data:
        return None
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{source}: '{field}' must be a nonnegative integer")
    return value


def _optional_positive_number(data: dict[str, Any], field: str, source: str) -> int | float | None:
    if field not in data:
        return None
    value = data[field]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{source}: '{field}' must be a positive number")
    return value


def _expected_output_mode(data: dict[str, Any], source: str) -> str:
    value = data.get("expected_output_mode", "contains")
    if not isinstance(value, str) or value not in {"contains", "exact"}:
        raise ValidationError(f"{source}: 'expected_output_mode' must be 'contains' or 'exact'")
    return value


def _optional_trajectory_match_mode(data: dict[str, Any], source: str) -> str | None:
    if "trajectory_match_mode" not in data:
        return None
    value = data["trajectory_match_mode"]
    valid = {"strict", "unordered", "subset", "superset"}
    if not isinstance(value, str) or value not in valid:
        raise ValidationError(f"{source}: 'trajectory_match_mode' must be one of {sorted(valid)}")
    return value


def _optional_tool_order(data: dict[str, Any], source: str) -> list[tuple[str, str]]:
    if "must_precede" not in data:
        return []
    value = data["must_precede"]
    if not isinstance(value, list):
        raise ValidationError(f"{source}: 'must_precede' must be a list of {{'before','after'}} objects")
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or set(item) != {"before", "after"}
            or not isinstance(item.get("before"), str)
            or not isinstance(item.get("after"), str)
        ):
            raise ValidationError(
                f"{source}: 'must_precede' item {index} must be an object with string 'before' and 'after'"
            )
        if item["before"] == item["after"]:
            raise ValidationError(f"{source}: 'must_precede' item {index} has identical 'before' and 'after'")
        pairs.append((item["before"], item["after"]))
    return pairs
