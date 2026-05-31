from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from plimsoll.models import Policy, TraceRun
from plimsoll.rules import trace_metrics


def trajectory_diff(trace: TraceRun, baseline: TraceRun | None, policy: Policy | None = None) -> dict[str, Any]:
    if baseline is None:
        return {}
    baseline_metrics = trace_metrics(baseline)
    current_metrics = trace_metrics(trace)
    return {
        "tool_sequence": {
            "baseline": baseline.tool_sequence,
            "current": trace.tool_sequence,
            "changes": sequence_changes(baseline.tool_sequence, trace.tool_sequence),
        },
        "steps": step_diff(_tool_step_metrics(baseline), _tool_step_metrics(trace)),
        "must_precede": [[before, after] for before, after in policy.must_precede] if policy else [],
        "metrics_delta": {
            "steps": current_metrics["steps"] - baseline_metrics["steps"],
            "duration_ms": current_metrics["duration_ms"] - baseline_metrics["duration_ms"],
            "tokens": current_metrics["tokens"] - baseline_metrics["tokens"],
            "estimated_cost_usd": round(
                current_metrics["estimated_cost_usd"] - baseline_metrics["estimated_cost_usd"], 6
            ),
        },
        "final_output": {
            "baseline": baseline.final_output,
            "current": trace.final_output,
            "changed": baseline.final_output.strip() != trace.final_output.strip(),
        },
    }


def sequence_changes(baseline: list[str], current: list[str]) -> list[dict[str, Any]]:
    matcher = SequenceMatcher(a=baseline, b=current, autojunk=False)
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append(
            {
                "type": tag,
                "baseline_index": i1,
                "current_index": j1,
                "baseline": baseline[i1:i2],
                "current": current[j1:j2],
            }
        )
    return changes


def _tool_step_metrics(trace: TraceRun) -> list[dict[str, Any]]:
    """Per-tool-step duration and token cost, in order — the basis for honest per-step deltas."""
    steps: list[dict[str, Any]] = []
    for span in trace.spans:
        if not span.tool_name:
            continue
        tokens = int(span.attributes.get("gen_ai.usage.input_tokens", 0) or 0) + int(
            span.attributes.get("gen_ai.usage.output_tokens", 0) or 0
        )
        steps.append({"tool": span.tool_name, "duration_ms": span.duration_ms, "tokens": tokens})
    return steps


def step_diff(baseline_steps: list[dict[str, Any]], current_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align baseline vs current tool steps into a unified per-step diff with signed deltas.

    op is one of: equal (in both), delete (dropped from the candidate), insert (new in the
    candidate). Deltas are computed from the real span durations/tokens, so a dropped step's
    delta is the negative of what it cost in the baseline — never a fabricated number.
    """
    matcher = SequenceMatcher(
        a=[s["tool"] for s in baseline_steps], b=[s["tool"] for s in current_steps], autojunk=False
    )
    out: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                base = baseline_steps[i1 + offset]
                cur = current_steps[j1 + offset]
                out.append(
                    {
                        "tool": cur["tool"],
                        "op": "equal",
                        "delta_ms": cur["duration_ms"] - base["duration_ms"],
                        "delta_tokens": cur["tokens"] - base["tokens"],
                    }
                )
            continue
        # replace is decomposed into the dropped baseline steps then the inserted candidate steps.
        if tag in {"delete", "replace"}:
            for index in range(i1, i2):
                base = baseline_steps[index]
                out.append(
                    {
                        "tool": base["tool"],
                        "op": "delete",
                        "delta_ms": -base["duration_ms"],
                        "delta_tokens": -base["tokens"],
                    }
                )
        if tag in {"insert", "replace"}:
            for index in range(j1, j2):
                cur = current_steps[index]
                out.append(
                    {"tool": cur["tool"], "op": "insert", "delta_ms": cur["duration_ms"], "delta_tokens": cur["tokens"]}
                )
    return out
