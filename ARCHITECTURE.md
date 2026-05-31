# Architecture

Plimsoll is a dependency-free Python CLI. The base workflow is:

1. Load one trace or a directory of traces.
2. Normalize native, OpenTelemetry-style, OpenInference-style, LangGraph-inspired local event, or OpenAI Agents-style input into the same internal model.
3. Load a JSON policy.
4. Optionally load baseline traces by `case_id`.
5. Run deterministic checks.
6. Write `report.json`, `report.html`, and optional CI artifacts (JUnit XML, SARIF JSON, Markdown).

## Modules

- `plimsoll.cli`: CLI parsing and command orchestration.
- `plimsoll.io`: JSON and JSONL loading, trace validation, policy parsing, report JSON writes.
- `plimsoll.models`: dataclasses for traces, spans, policies, findings, and case reports.
- `plimsoll.adapters`: small local-file adapters for sanitized framework-shaped fixtures.
- `plimsoll.otel`: small OpenTelemetry-style JSON adapter that normalizes span exports into `TraceRun`.
- `plimsoll.diff`: baseline-vs-current trajectory comparison for report evidence.
- `plimsoll.policy`: starter-policy inference from observed traces.
- `plimsoll.rules`: deterministic evaluators for output, tools, budgets, privacy patterns, retries, repeated actions, baseline drift, and tolerant trajectory matching against a baseline.
- `plimsoll.report`: score calculation plus JSON, static HTML, JUnit XML, SARIF JSON, and Markdown rendering.

## Design Choices

- No runtime dependencies. A reviewer can run the demo with Python 3.11+.
- No database. Reports are files because the primary artifact is local evidence.
- No web server. The HTML report is static to avoid unnecessary runtime surface area.
- Deterministic rules first. Optional model-based evaluation would be an extension, not a requirement.
- Trace schema stays small. It maps cleanly to common span concepts without requiring a specific observability vendor.
- Adapter and output code stay separate from rules. Imported formats become `TraceRun`; report formats consume `CaseReport`.
- Policy inference is a bootstrap helper, not a substitute for human review of the policy gate.

## Data Flow

```text
native or adapter-supported JSON files
  -> validation and normalization
  -> policy checks
  -> optional baseline comparison (edit distance + tolerant trajectory match)
  -> optional trajectory diff
  -> case reports
  -> report.json + report.html + optional JUnit/SARIF/Markdown
```

SARIF results anchor to the committed policy file at the line of the rule that triggered
them, not to the trace path, so findings render in GitHub code scanning. When no policy
file is available, results fall back to the input path. The Markdown report renders the
same summary for PR comments; inside GitHub Actions it is also appended to
`$GITHUB_STEP_SUMMARY` automatically.

## Scoring

Each case starts at 100. Findings subtract:

- critical: 45
- high: 30
- medium: 15
- low: 5

A case passes only when it has no critical or high findings.

## Extension Points

- Add new deterministic rules in `plimsoll.rules.evaluate_trace`.
- Add output formats in `plimsoll.report`.
- Add import adapters that normalize external traces into `TraceRun`.
- Add CI output without changing the core rule engine.

## Contract Documentation

`SCHEMA.md` is the source of truth for public trace, policy, adapter, and output shapes. Code changes that alter accepted input fields or emitted report fields should update that file and the smoke script together.

The supported public interface is the CLI and documented file contracts. Internal Python modules are organized for maintainability but are not stable API yet.

## Known Seams (Defer Until They Bite)

- **`plimsoll.report` split.** `report.py` houses scoring plus HTML, JUnit, SARIF, and Markdown rendering in one module that is the largest in the codebase (~890 lines). Splitting it by output target into a `plimsoll/report/` package (`scoring.py`, `html.py`, `junit.py`, `sarif.py`, `markdown.py`) is the next step, with `__init__.py` re-exporting the existing `write_*_report`, `render_markdown`, `build_case_report`, `summarize`, and `report_to_dict` names so callers do not break.
- **Static type checking.** The codebase is fully type-annotated but no type checker runs as part of `scripts/smoke.py`. Astral's `ty` is the fast option but was still beta as of mid-2026; `mypy` and `pyright` are the stable alternatives. Adding a type-check gate is deferred until one of these can be added without producing false-positive churn that would force unrelated edits.
