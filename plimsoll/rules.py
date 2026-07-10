from __future__ import annotations

import re
from collections import Counter
from typing import Any

from plimsoll.models import Finding, Policy, TraceRun

DEFAULT_PII_PATTERNS = [
    r"\b\d{3}-\d{2}-\d{4}\b",
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
]

DEFAULT_SECRET_PATTERNS = [
    r"\b" + "s" + r"k-[A-Za-z0-9_-]{16,}\b",
    r"\b" + "g" + r"hp_[A-Za-z0-9_]{20,}\b",
    r"\b[A-Z0-9]{20,}\b",
]


def evaluate_trace(trace: TraceRun, policy: Policy, baseline: TraceRun | None = None) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_expected_output(trace, policy))
    findings.extend(check_tool_policy(trace, policy))
    findings.extend(check_budgets(trace, policy))
    findings.extend(check_repeated_actions(trace, policy))
    findings.extend(check_retry_drift(trace))
    findings.extend(check_sensitive_data(trace, policy))
    findings.extend(check_baseline_distance(trace, policy, baseline))
    findings.extend(check_trajectory_match(trace, policy, baseline))
    findings.extend(check_tool_order(trace, policy))
    return findings


def check_tool_order(trace: TraceRun, policy: Policy) -> list[Finding]:
    """Enforce required tool ordering (e.g. approvals before a privileged action).

    For each (before, after) pair, any run that performs ``after`` must have performed
    ``before`` at an earlier step. A run that never performs ``after`` (the agent refuses
    or escalates instead) is valid: this checks order, not presence, so it never forces
    the high-risk action to occur. Severity is critical because the canonical violation
    is an approval bypass / privilege escalation.
    """
    sequence = trace.tool_sequence
    findings: list[Finding] = []
    for before, after in policy.must_precede:
        if after not in sequence:
            continue
        first_after = sequence.index(after)
        if before not in sequence[:first_after]:
            findings.append(
                Finding(
                    "tool_order",
                    "critical",
                    trace.case_id,
                    f"'{after}' occurred before the required '{before}'.",
                    {"before": before, "after": after, "fired_at": first_after + 1, "sequence": sequence},
                )
            )
    return findings


def check_expected_output(trace: TraceRun, policy: Policy) -> list[Finding]:
    if trace.expected_output is None:
        return []
    expected = trace.expected_output.strip()
    actual = trace.final_output.strip()
    if policy.expected_output_mode == "exact":
        passed = actual == expected
    else:
        passed = expected.lower() in actual.lower()
    if passed:
        return []
    return [
        Finding(
            "expected_output",
            "high",
            trace.case_id,
            "Final output does not match the expected result.",
            {"expected": expected, "actual": actual, "mode": policy.expected_output_mode},
        )
    ]


def check_tool_policy(trace: TraceRun, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    tools = [tool for tool in trace.tool_sequence if tool]
    used = set(tools)
    if policy.allowed_tools:
        disallowed = sorted(tool for tool in used if tool not in policy.allowed_tools)
        if disallowed:
            findings.append(
                Finding(
                    "tool_allowlist",
                    "critical",
                    trace.case_id,
                    "Trace used tools outside the allowlist.",
                    {"disallowed_tools": disallowed},
                )
            )
    forbidden = sorted(used & policy.forbidden_tools)
    if forbidden:
        findings.append(
            Finding(
                "forbidden_tool",
                "critical",
                trace.case_id,
                "Trace used forbidden tools.",
                {"forbidden_tools": forbidden},
            )
        )
    missing = sorted(policy.required_tools - used)
    if missing:
        findings.append(
            Finding(
                "required_tool",
                "medium",
                trace.case_id,
                "Trace skipped required tools.",
                {"missing_tools": missing},
            )
        )
    return findings


# The budget-rule vocabulary: rule id -> the trace metric it caps. The Policy attribute
# holding each limit shares the rule id's name. ``plimsoll.governor`` keys its gate copy
# off this mapping, so a budget rule added here cannot silently drift out of the gate.
BUDGET_RULES: dict[str, str] = {
    "max_steps": "steps",
    "max_duration_ms": "duration_ms",
    "max_tokens": "tokens",
    "max_estimated_cost_usd": "estimated_cost_usd",
}


def check_budgets(trace: TraceRun, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    metrics = trace_metrics(trace)
    for rule_id, metric_name in BUDGET_RULES.items():
        limit = getattr(policy, rule_id)
        if limit is not None and metrics[metric_name] > limit:
            findings.append(
                Finding(
                    rule_id,
                    "medium",
                    trace.case_id,
                    f"Trace exceeded {metric_name} budget.",
                    {"actual": metrics[metric_name], "limit": limit},
                )
            )
    return findings


def check_repeated_actions(trace: TraceRun, policy: Policy) -> list[Finding]:
    signatures = [span.action_signature for span in trace.spans if span.tool_name]
    counts = Counter(signatures)
    offenders = {signature: count for signature, count in counts.items() if count > policy.max_repeated_action_count}
    if not offenders:
        return []
    return [
        Finding(
            "repeated_action",
            "medium",
            trace.case_id,
            "Trace repeated identical tool actions more than the policy allows.",
            {"repeated_actions": offenders, "limit": policy.max_repeated_action_count},
        )
    ]


def check_retry_drift(trace: TraceRun) -> list[Finding]:
    findings: list[Finding] = []
    spans = [span for span in trace.spans if span.tool_name]
    for previous, current in zip(spans, spans[1:]):
        if previous.status == "error" and previous.tool_name == current.tool_name:
            if previous.action_signature != current.action_signature:
                findings.append(
                    Finding(
                        "retry_drift",
                        "medium",
                        trace.case_id,
                        "Retry changed the tool input after an error, which may hide nondeterministic behavior.",
                        {
                            "failed_span": previous.span_id,
                            "retry_span": current.span_id,
                            "before": previous.action_signature,
                            "after": current.action_signature,
                        },
                    )
                )
    return findings


def check_sensitive_data(trace: TraceRun, policy: Policy) -> list[Finding]:
    text = collect_text(trace)
    findings: list[Finding] = []
    for label, severity, patterns in [
        ("pii_leak", "high", DEFAULT_PII_PATTERNS + policy.pii_patterns),
        ("secret_leak", "critical", DEFAULT_SECRET_PATTERNS + policy.secret_patterns),
    ]:
        matches = sorted({match.group(0) for pattern in patterns for match in re.finditer(pattern, text, flags=re.I)})
        if matches:
            findings.append(
                Finding(
                    label,
                    severity,
                    trace.case_id,
                    "Trace contains data matching sensitive-data patterns.",
                    {"match_count": len(matches), "examples": [redact(match) for match in matches[:5]]},
                )
            )
    return findings


def check_baseline_distance(trace: TraceRun, policy: Policy, baseline: TraceRun | None) -> list[Finding]:
    if baseline is None or policy.max_tool_sequence_distance is None:
        return []
    distance = edit_distance(baseline.tool_sequence, trace.tool_sequence)
    if distance <= policy.max_tool_sequence_distance:
        return []
    return [
        Finding(
            "trajectory_drift",
            "high",
            trace.case_id,
            "Tool sequence drifted beyond the accepted distance from baseline.",
            {
                "distance": distance,
                "limit": policy.max_tool_sequence_distance,
                "baseline": baseline.tool_sequence,
                "actual": trace.tool_sequence,
            },
        )
    ]


TRAJECTORY_MATCH_MODES = {"strict", "unordered", "subset", "superset"}


def check_trajectory_match(trace: TraceRun, policy: Policy, baseline: TraceRun | None) -> list[Finding]:
    """Compare the tool trajectory against a baseline using a tolerant match mode.

    Modes mirror the agentevals conventions (baseline = reference, trace = actual):
      - strict:    identical tools in identical order
      - unordered: same multiset of tools, any order
      - superset:  trace contains at least every baseline tool call (extras allowed) -> catches dropped steps
      - subset:    trace introduces no tool calls beyond the baseline (omissions allowed) -> catches new steps
    """
    mode = policy.trajectory_match_mode
    if mode is None or baseline is None:
        return []
    reference = baseline.tool_sequence
    actual = trace.tool_sequence
    matched, detail = _trajectory_matches(mode, reference, actual)
    if matched:
        return []
    return [
        Finding(
            "trajectory_mismatch",
            "high",
            trace.case_id,
            f"Tool trajectory does not satisfy the '{mode}' match against the baseline.",
            {"mode": mode, "baseline": reference, "actual": actual, **detail},
        )
    ]


def _trajectory_matches(mode: str, reference: list[str], actual: list[str]) -> tuple[bool, dict[str, Any]]:
    if mode == "strict":
        if actual == reference:
            return True, {}
        return False, {"reason": "tool sequence differs in order or content"}
    reference_counts = Counter(reference)
    actual_counts = Counter(actual)
    missing = sorted((reference_counts - actual_counts).elements())
    unexpected = sorted((actual_counts - reference_counts).elements())
    if mode == "unordered":
        if not missing and not unexpected:
            return True, {}
        return False, {"missing": missing, "unexpected": unexpected}
    if mode == "superset":
        return (not missing), ({} if not missing else {"missing": missing})
    if mode == "subset":
        return (not unexpected), ({} if not unexpected else {"unexpected": unexpected})
    return True, {}


def trace_metrics(trace: TraceRun) -> dict[str, Any]:
    tokens = 0
    cost = 0.0
    for span in trace.spans:
        # The OTel `invoke_agent` span reports usage aggregated across its child
        # operations, so counting it as well as the children double-counts tokens on a
        # real agent export. Skip the agent-level aggregate; the children carry the
        # real per-call usage.
        if span.attributes.get("gen_ai.operation.name") == "invoke_agent":
            continue
        tokens += int(span.attributes.get("gen_ai.usage.input_tokens", 0) or 0)
        tokens += int(span.attributes.get("gen_ai.usage.output_tokens", 0) or 0)
        cost += float(span.attributes.get("estimated_cost_usd", 0.0) or 0.0)
    return {
        "steps": len(trace.spans),
        "tool_steps": len(trace.tool_sequence),
        "duration_ms": trace.total_duration_ms,
        "tokens": tokens,
        "estimated_cost_usd": round(cost, 6),
        "tool_sequence": trace.tool_sequence,
    }


def collect_text(trace: TraceRun) -> str:
    parts: list[str] = [trace.final_output]
    for span in trace.spans:
        parts.extend([str(span.input), str(span.output), str(span.error), str(span.attributes)])
    return "\n".join(parts)


def edit_distance(left: list[str], right: list[str]) -> int:
    dp = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i in range(len(left) + 1):
        dp[i][0] = i
    for j in range(len(right) + 1):
        dp[0][j] = j
    for i, left_item in enumerate(left, start=1):
        for j, right_item in enumerate(right, start=1):
            cost = 0 if left_item == right_item else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return dp[-1][-1]


def redact(value: str) -> str:
    if len(value) <= 8:
        return "[redacted]"
    return f"{value[:3]}...{value[-3:]}"
