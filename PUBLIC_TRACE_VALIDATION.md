# Public Trace Fixture Validation

Plimsoll includes a small fixture corpus — shaped from public tracing documentation and semantic-convention references, plus one real offline export from the pydantic-ai SDK. These fixtures are validated against Plimsoll's adapter expectations without requiring API keys, model calls, collectors, or hosted dashboards.

## What This Validates

| Format | Fixture | Public reference | What is validated |
| --- | --- | --- | --- |
| `otel` | `examples/traces/otel_ticket_triage.json` | OpenTelemetry GenAI and trace conventions | OTLP-style span nesting, span IDs, timestamps, status, GenAI operation/tool/token attributes. |
| `openinference` | `examples/traces/openinference_ticket_triage.json` | OpenInference semantic conventions | `openinference.span.kind`, tool naming, prompt/completion token attributes on OTel-shaped spans. |
| `openai-agents` | `examples/traces/openai_agents_ticket_triage.json` | OpenAI Agents SDK tracing docs | Trace/span concepts, timestamped spans, span data, usage payloads, sensitive-data posture. |
| `langgraph` | `examples/traces/langgraph_ticket_triage.json` | LangGraph workflow/event concepts | LangGraph-inspired local event fixture mapped into Plimsoll spans. |
| `otel` (real) | `examples/access-request/traces/real/clean.otel.json` | **Real pydantic-ai OpenTelemetry export** | Spans emitted by pydantic-ai's GenAI instrumentation during an offline run; ingested by the generic OTel adapter with no framework-specific code. |

Run the validation:

```bash
python3 scripts/validate_public_fixtures.py
```

The script checks that every documented fixture exists, can be loaded through its adapter, maps to the expected tool sequence, preserves token metrics where relevant, and passes the default policy against the native baseline.

## Sanitized Real-Shaped Fixture

`examples/traces/real_openinference_like_ticket_triage.json` mirrors a fuller OpenInference export shape than the minimal `openinference_ticket_triage.json` fixture. It includes:

- `resourceSpans[].resource.attributes` with `service.name`, `service.version`, and `telemetry.sdk.*`.
- `scopeSpans[].scope` identifying the instrumentation package and version.
- A parent `AGENT` span wrapping three child spans (`TOOL`, `TOOL`, `LLM`) linked by `parentSpanId`.
- LLM attributes: `llm.system`, `llm.model_name`, `llm.invocation_parameters`, and the `llm.token_count.prompt`/`completion`/`total` triple.

It is **sanitized and structurally modeled** after public OpenInference documentation. It is **not** exported from Phoenix, LangSmith, OpenAI, or any other third-party deployment, and contains no real user data. It is validated by the same `scripts/validate_public_fixtures.py` gate as every other fixture in this table.

## Real SDK Export (pydantic-ai, offline)

`examples/access-request/traces/real/{clean,bypass}.otel.json` are different in kind from every fixture above: they are **emitted by the actual pydantic-ai library**, not hand-authored. `examples/access-request/build_real_otel_trace.py` builds a small access-request agent, drives a deterministic tool sequence with a no-network `FunctionModel` (no API key, no model call, no user data), and captures the spans pydantic-ai's built-in GenAI OpenTelemetry instrumentation produces via an in-memory span exporter.

- The span tree, span kinds, `gen_ai.*` attribute names, tool calls, and token usage are **verbatim from the instrumentation**. Only span IDs and timestamps are normalized, so the committed JSON is byte-stable across regenerations.
- Plimsoll's generic `otel` adapter ingests it with **no framework-specific code**. The `clean` run passes the access-control policy; the `bypass` run (which grants access without the security review) is caught as a **critical `tool_order`** violation.
- Capturing this real export surfaced — and fixed — two adapter gaps the hand-authored fixtures had not: bare `gen_ai.operation.name` values like `chat`/`invoke_agent` were treated as tool steps, and the `invoke_agent` aggregate span double-counted tokens. See `CHANGELOG.md`.

Regenerate with `pip install -e '.[realtraces]'` then `python examples/access-request/build_real_otel_trace.py`.

## What This Does Not Claim

- It does not claim full SDK compatibility.
- It runs one real SDK (pydantic-ai) **offline** to capture a genuine export, but does not exercise SDKs against live model providers, hosted dashboards, or networked deployments.
- It does not require network access, credentials, or paid APIs to validate (regenerating the real export needs the SDK installed locally; validating it does not).

The point is narrower: fixtures are shaped from public documentation — plus one real offline SDK export — and validated against Plimsoll's adapter expectations while keeping the project local, deterministic, and zero-runtime-dependency.
