# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-05-31

### Changed

- HTML report redesigned around a static-first layout that renders fully without
  JavaScript: a verdict band, a finding summary with severity pills and RULE/CODE
  provenance tags, a run-metrics table, a findings table with native `<details>`
  evidence, and a vertical trajectory-diff stepper. JavaScript only enhances the theme
  toggle, unified/split diff, drawers, and copy buttons; the core report is fully visible
  with scripting disabled. Design tokens ship inline, without CDN assets, web fonts, React, or
  runtime dependencies.
- The trajectory `must_precede` explanation now renders one drawer per ordering violation,
  each attributed to its own evidence, and derives the required chain from real transitive
  predecessors — so independent ordering rules are never spliced into one invented chain
  and bypass counts are computed, not assumed.
- OTel adapter no longer treats a bare `gen_ai.operation.name` (e.g. `chat`,
  `invoke_agent`) as a tool name, so a real agent export's per-turn LLM spans no longer
  appear as phantom tool steps. Explicit `gen_ai.tool.name` / `tool.name` / OpenInference
  span kind / `execute_tool` still classify tools. (Found by validating a real export.)
- Token totals exclude the OTel `invoke_agent` aggregate span, which reports usage already
  counted on its child operations — avoiding a ~2x double-count on real agent exports.

### Added

- `scripts/build_golden.py` regenerates the committed golden reports under
  `examples/output/golden/` deterministically from the CLI.
- Validation against a **real** agent trace. `examples/access-request/build_real_otel_trace.py`
  runs pydantic-ai offline (no network, no API key) and exports the spans its built-in
  GenAI OpenTelemetry instrumentation emits to `examples/access-request/traces/real/`.
  Plimsoll's generic OTel adapter ingests the real export with no framework-specific
  code: the clean run passes and a bypass run is caught as a critical `tool_order`
  violation. Registered in `examples/public_trace_sources.json` and gated by
  `tests/test_real_trace.py` and `scripts/validate_public_fixtures.py` (now per-source).

## [0.8.0] - 2026-05-30

### Added

- `must_precede` ordering invariant. Each `{before, after}` pair requires that any run
  performing `after` has performed `before` at an earlier step (rule id `tool_order`,
  critical). A run that never performs `after` (the agent refuses or escalates) is
  valid, so the gate never forces a high-risk action to occur.
- IT access-request reference scenario (`examples/access-request/`): a deterministic,
  offline reference agent that emits real traces for a privileged-access workflow, an
  access-control policy enforcing `manager_review -> security_review -> grant_access`,
  seven adversarial probes, a workflow risk plan, and committed clean/failed/fixed traces
  and reports. `scripts/build_access_request_demo.py` regenerates them deterministically.
- Product artifacts for the scenario: `BEFORE_AFTER.md`, `EVAL_PLAN.md`, `RISK_REGISTER.md`.
- `must_precede` added to the published policy JSON Schema (`docs/policy.schema.json`).

## [0.7.0] - 2026-05-30

### Added

- Tolerant trajectory matching. The policy field `trajectory_match_mode` enables a
  baseline-vs-current tool-trajectory check (rule id `trajectory_mismatch`) with four
  modes that mirror agentevals semantics (baseline = reference): `strict` requires the
  same tools in the same order; `unordered` requires the same multiset of tools in any
  order; `superset` allows extra tool calls but flags dropped baseline calls; `subset`
  allows omissions but flags tool calls beyond the baseline.
- Markdown report (`--md`) for PR comments, plus an automatic GitHub Step Summary that
  writes to `$GITHUB_STEP_SUMMARY` inside Actions with zero configuration.
- CLI flags: `--json` (machine-readable summary to stdout), `-q`/`--quiet`,
  `--color {auto,always,never}` (plus `--no-color`; honors `NO_COLOR`, `FORCE_COLOR`,
  and `TERM=dumb`), `--version`, and `--exit-zero`.
- `python -m plimsoll` module entrypoint, matching the console script.
- Verdict-first HTML report with dark-mode support and an accessible three-signal status
  (glyph, label, and color) so the verdict does not rely on color alone.

### Changed

- Exit-code contract. Failing findings (high or critical) now exit `1` by default; this
  previously happened only with `--fail-on-findings`. `--fail-on-findings` is retained as
  a deprecated no-op alias. `--exit-zero` forces exit `0` (report-only mode). Exit `2`
  still indicates a tool error.
- SARIF results now anchor to the committed policy file at the line of the rule that
  triggered them, carrying `region.startLine`, `partialFingerprints`, `automationDetails`,
  and `problem.severity`, instead of pointing at the trace path. Findings now render
  correctly in GitHub code scanning.
- The human-readable summary banner now prints to stderr so machine output (`--json`,
  report files) on stdout pipes cleanly (clig.dev discipline).

### Fixed

- SARIF previously used the input trace path or a `trace://` URI as the result location,
  which GitHub code scanning renders as "couldn't find this file" (or rejects for non-file
  schemes). Results are now anchored to a committed file.

## [0.6.0] - 2026-05-17

### Added

- Initial public release: a deterministic, dependency-free trace-regression CLI for AI
  agent execution traces.
- Native trace input plus subset adapters for OpenTelemetry, OpenInference, LangGraph,
  and OpenAI Agents shaped local JSON.
- JSON, HTML, JUnit XML, and SARIF report outputs.
- JSON policy gate with baseline tool-sequence edit-distance, severity-weighted scoring,
  and `init-policy` starter-policy inference from observed traces.
