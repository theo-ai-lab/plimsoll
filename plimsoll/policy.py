from __future__ import annotations

from collections import Counter
from typing import Any

from plimsoll.models import TraceRun
from plimsoll.rules import trace_metrics


def infer_policy(traces: list[TraceRun]) -> dict[str, Any]:
    tools = sorted({tool for trace in traces for tool in trace.tool_sequence})
    metrics = [trace_metrics(trace) for trace in traces]
    repeated_counts = []
    for trace in traces:
        counts = Counter(span.action_signature for span in trace.spans if span.tool_name)
        repeated_counts.append(max(counts.values(), default=1))
    return {
        "allowed_tools": tools,
        "forbidden_tools": [],
        "required_tools": tools,
        "max_steps": _budget(max(metric["steps"] for metric in metrics)),
        "max_duration_ms": _budget(max(metric["duration_ms"] for metric in metrics)),
        "max_tokens": _budget(max(metric["tokens"] for metric in metrics)),
        "max_estimated_cost_usd": round(max(metric["estimated_cost_usd"] for metric in metrics) * 1.2, 6) or 0.001,
        "max_repeated_action_count": max(repeated_counts, default=1),
        "expected_output_mode": "contains",
        "max_tool_sequence_distance": 1,
        "pii_patterns": [],
        "secret_patterns": [],
    }


def _budget(value: int) -> int:
    return max(1, int(value * 1.2) + 1)
