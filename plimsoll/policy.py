from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import fields
from typing import Any

from plimsoll.models import Policy, TraceRun
from plimsoll.rules import trace_metrics

DIGEST_PREFIX = "sha256:"


def canonical_policy_text(policy: Policy) -> str:
    """The one canonical text form of an *effective* policy, for hashing.

    Two policies that constrain identically must produce the same text regardless of how
    they were written: key order, whitespace, `$schema`, and the ordering of the tool sets
    are all normalized away. Every field of :class:`~plimsoll.models.Policy` is emitted by
    walking the dataclass, so a policy field added later is covered by the digest without
    anyone remembering to update this function.
    """
    document: dict[str, Any] = {}
    for policy_field in fields(Policy):
        value = getattr(policy, policy_field.name)
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        elif isinstance(value, list):
            value = [list(item) if isinstance(item, tuple) else item for item in value]
        document[policy_field.name] = value
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def policy_digest(policy: Policy) -> str:
    """``sha256:<hex>`` over :func:`canonical_policy_text`.

    Every gate decision carries this, so a recorded decision provably binds to the exact
    policy content that produced it: change any constraint and the digest changes, making
    a decision replayed under a different policy detectable rather than plausible.
    """
    return DIGEST_PREFIX + hashlib.sha256(canonical_policy_text(policy).encode("utf-8")).hexdigest()


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
