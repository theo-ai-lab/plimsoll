from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JsonObject = dict[str, Any]


class ValidationError(ValueError):
    """Raised when an input trace or policy is structurally invalid."""


@dataclass(frozen=True)
class Span:
    span_id: str
    name: str
    kind: str
    status: str
    start_ms: int
    end_ms: int
    tool_name: str | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    attributes: JsonObject = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    @property
    def action_signature(self) -> str:
        return f"{self.tool_name or self.kind}:{stable_repr(self.input)}"


@dataclass(frozen=True)
class TraceRun:
    run_id: str
    case_id: str
    final_output: str
    expected_output: str | None
    spans: list[Span]
    metadata: JsonObject = field(default_factory=dict)

    @property
    def tool_sequence(self) -> list[str]:
        return [span.tool_name for span in self.spans if span.tool_name]

    @property
    def total_duration_ms(self) -> int:
        if not self.spans:
            return 0
        return max(span.end_ms for span in self.spans) - min(span.start_ms for span in self.spans)


@dataclass(frozen=True)
class Policy:
    allowed_tools: set[str] = field(default_factory=set)
    forbidden_tools: set[str] = field(default_factory=set)
    required_tools: set[str] = field(default_factory=set)
    max_steps: int | None = None
    max_duration_ms: int | None = None
    max_tokens: int | None = None
    max_estimated_cost_usd: float | None = None
    max_repeated_action_count: int = 1
    pii_patterns: list[str] = field(default_factory=list)
    secret_patterns: list[str] = field(default_factory=list)
    expected_output_mode: str = "contains"
    max_tool_sequence_distance: int | None = None
    trajectory_match_mode: str | None = None
    must_precede: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    case_id: str
    message: str
    evidence: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class CaseReport:
    case_id: str
    run_id: str
    score: int
    passed: bool
    metrics: JsonObject
    findings: list[Finding]
    trajectory_diff: JsonObject = field(default_factory=dict)


def stable_repr(value: Any) -> str:
    if isinstance(value, dict):
        parts = [f"{key}:{stable_repr(value[key])}" for key in sorted(value)]
        return "{" + ",".join(parts) + "}"
    if isinstance(value, list):
        return "[" + ",".join(stable_repr(item) for item in value) + "]"
    return repr(value)
