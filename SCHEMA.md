# Schema Reference

Plimsoll keeps its contracts small so reports are easy to audit.

## Native Trace

Traces may be supplied as `.json` (a single object matching the shape below) or `.jsonl` (one `{"event": "run", ...}` metadata row followed by `{"event": "span", ...}` rows that map into the spans array).

Required top-level fields:

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | string | Identifier for this run. |
| `case_id` | string | Joins candidate traces to baseline traces. |
| `final_output` | string | Output checked by `expected_output` rules. |
| `spans` | array | Non-empty list of span objects. |

Optional top-level fields:

| Field | Type | Notes |
| --- | --- | --- |
| `expected_output` | string | Used by exact or contains matching. |
| `metadata` | object | Preserved as context, not used by rules today. |

Span fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `span_id` | string | yes | Stable within the trace. |
| `name` | string | yes | Human-readable step name. |
| `kind` | string | yes | Tool, model, retriever, internal, etc. |
| `status` | string | yes | `ok` or `error` are used by retry checks. |
| `start_ms` | integer | yes | Milliseconds relative to run start. |
| `end_ms` | integer | yes | Must be greater than or equal to `start_ms`. |
| `tool_name` | string | no | Included in tool sequence and tool policy checks. |
| `input` | any | no | Used for repeated-action and retry-drift evidence. |
| `output` | any | no | Scanned for sensitive-data patterns. |
| `error` | string | no | Scanned and used for retry context. |
| `attributes` | object | no | Token and cost attributes are read here. |

Token and cost attributes:

| Attribute | Type | Notes |
| --- | --- | --- |
| `gen_ai.usage.input_tokens` | integer | Added to token budget. |
| `gen_ai.usage.output_tokens` | integer | Added to token budget. |
| `estimated_cost_usd` | number | Added to estimated-cost budget. |

## Policy

| Field | Type | Validation |
| --- | --- | --- |
| `allowed_tools` | array of strings | Optional. If present, all used tools must be listed. |
| `forbidden_tools` | array of strings | Optional. Any match is critical. |
| `required_tools` | array of strings | Optional. Missing tools are medium severity. |
| `must_precede` | array of `{before, after}` objects | Optional. Each pair requires `before` to occur before `after`; an out-of-order run emits a critical `tool_order` finding. A run that never performs `after` is valid. |
| `max_steps` | positive integer | Optional. |
| `max_duration_ms` | positive integer | Optional. |
| `max_tokens` | positive integer | Optional. |
| `max_estimated_cost_usd` | positive number | Optional. |
| `max_repeated_action_count` | positive integer | Defaults to `1`. |
| `expected_output_mode` | `contains` or `exact` | Defaults to `contains`. |
| `max_tool_sequence_distance` | nonnegative integer | Optional baseline edit-distance limit. |
| `trajectory_match_mode` | `strict`, `unordered`, `subset`, or `superset` | Optional. Enables the `trajectory_mismatch` check against a baseline. |
| `pii_patterns` | array of valid regex strings | Optional custom patterns. |
| `secret_patterns` | array of valid regex strings | Optional custom patterns. |
| `version` | string | Optional. Policy schema version tag, recorded for forward compatibility. |

`trajectory_match_mode` compares the candidate tool trajectory against the baseline's, treating the baseline as the reference (agentevals semantics). A mismatch is one high-severity `trajectory_mismatch` finding. It is independent of `max_tool_sequence_distance`; you can set either, both, or neither.

| Mode | Passes when |
| --- | --- |
| `strict` | Same tools in the same order. |
| `unordered` | Same multiset of tools, any order. |
| `superset` | No baseline tool call is dropped; extra calls are allowed. |
| `subset` | No tool call beyond the baseline; omissions are allowed. |

Requires a `--baseline` for the case; with no baseline the check is skipped.

## Adapter Field Mapping

| Format | Supported Shape | Key Mapping |
| --- | --- | --- |
| `native` | Plimsoll JSON/JSONL | Direct `TraceRun` and `Span` fields. |
| `otel` | `spans[]` or `resourceSpans[].scopeSpans[].spans[]` | `traceId`, `spanId`, `name`, `kind`, `status`, `startTimeUnixNano`, `endTimeUnixNano`, attributes. |
| `openinference` | OpenInference attributes on OTel-shaped spans | `openinference.span.kind`, `tool.name`, `llm.token_count.prompt`, `llm.token_count.completion`. |
| `langgraph` | Local `events[]` fixture shape | `node`/`tool`, `start_ms`, `end_ms`, `input`, `output`, `attributes`. |
| `openai-agents` | Local `trace_id`, `metadata`, `spans[]` fixture shape | span `data.tool_name`, `data.usage`, ISO timestamps. |

Adapters are intentionally subset mappers for local JSON artifacts. They are not full framework compatibility guarantees.

Fixture source references are recorded in `examples/public_trace_sources.json` and summarized in `PUBLIC_TRACE_VALIDATION.md`. Fixtures are shaped from public documentation and validated against Plimsoll's adapter expectations — except one `otel` fixture, which is a real pydantic-ai OpenTelemetry export captured offline and ingested by the generic `otel` adapter with no framework-specific code.

## Outputs

`report.json` contains:

- `summary`: case counts, pass rate, average score, severity counts.
- `cases[]`: case ID, run ID, score, pass/fail, metrics, findings, trajectory diff.

`report.html` renders the same information for review.

`report.junit.xml` contains one `testcase` per trace case. Failed cases include one `failure` element summarizing findings.

`report.sarif.json` uses SARIF `2.1.0` shape with Plimsoll rules and per-finding results. Each result anchors to the committed policy file at the `region.startLine` of the rule that triggered it (falling back to the input path when no policy file is given), and carries `partialFingerprints`, `automationDetails`, and `problem.severity` so findings render in GitHub code scanning.

`report.md` is a Markdown summary intended for PR comments: the pass/fail verdict, case counts, average score, and a findings table. Inside GitHub Actions the same Markdown is appended to `$GITHUB_STEP_SUMMARY` automatically, with no flag required. Write the file explicitly with `--md`.
