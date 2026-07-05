# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions prior to 0.9.0 were developed locally before this repository was published;
their dates record local development milestones, not public releases.

## [Unreleased]

## [1.0.0] - 2026-07-04

### Added

- **Runtime governor** (`plimsoll/governor.py`): the same deterministic rule engine run as a
  *pre-execution* gate. `Governor.evaluate(partial_trace, proposed_call)` decides whether a
  proposed next tool call is safe given the trace so far, enforcing only the subset of rules
  decidable before a call runs (allowlist/forbidden membership, `must_precede` ordering,
  cumulative budgets, repeated-action limits). Rules that need the call's result, the whole
  trajectory, or the finished run remain deferred to the post-hoc `check_trace` audit. No LLM,
  no network, no third-party import — the same zero-dependency engine, evaluated at the gate.
- **`plimsoll governor` CLI subcommand**: a one-shot, deterministic gate over a single proposed
  tool call. Reads the call as JSON (a tool-name string or an object with a `tool` field) from
  `--call PATH` or stdin, takes the calls that already ran via `--partial-trace`, evaluates it
  against `--policy`, and prints the allow/block decision with the rule that fired. Exits `0` to
  allow, `1` to block, `2` on a usage error; `--json` emits the machine-readable `Decision`.
- Optional MCP-style tool surface for the governor (`plimsoll/governor_mcp.py`): the gate
  (`propose_tool_call`) and the full audit (`check_trace`) as plain JSON-in/JSON-out
  callables, with optional `mcp`-SDK server wiring. The `mcp` SDK is an optional extra; the
  core engine never imports it.
- **`plimsoll-governor` console script**: launches the governor as an MCP server (stdio) so an
  MCP host can call `propose_tool_call`/`check_trace`. Requires the new optional `mcp` extra
  (`pip install "plimsoll[mcp]"`); the import stays lazy, so the zero-dependency core install is
  unaffected and the launcher exits with a clear install hint (no silent fallback) when the SDK
  is absent.
- **Armed `pass^k` CI gate** with committed multi-run fixtures (`examples/reliability/`): a
  stable directory (three runs of one `case_id`, all pass → `pass^3 = 1.0`) and a flaky one
  (one run bypasses the required approval → `pass^3 = 0.0`). The repo's own CI runs both as a
  self-test — asserting the stable fixture passes the gate (exit `0`) and the flaky one fails it
  (exit `1`) — and `examples/ci/github-actions.yml` documents the `--passk-threshold` step.
- `examples/governor_loop_demo.py`: the governor firing as a live pre-execution gate over a
  scripted agent loop, cross-checking every decision against a ground-truth label so the
  "blocked N of M unsafe calls" headline is verified, not asserted.
- **Recorded MCP governor session** (`examples/mcp-governor-session/`, `docs/MCP_DEMO.md`): a
  committed JSON-RPC stdio transcript of the real `plimsoll-governor` server refusing tool
  calls pre-execution — one allow, one `tool_order` deny of the task's own goal action, one
  `max_tokens` budget block. Captured by a scripted deterministic client
  (`scripts/build_mcp_governor_session.py`, stdlib-only, double-captures and byte-compares to
  verify determinism; not a live-model session) and pinned to the engine by
  `tests/test_governor_mcp_session.py`, which replays the transcript SDK-free on every run and
  end-to-end against a real server subprocess when the `mcp` extra is installed.
  `docs/MCP_DEMO.md` documents the `.mcp.json` host wiring and the three-outcome walkthrough;
  `demo/mcp-governor.{tape,gif}` record the session run.
- `pass^k` reliability aggregation over repeated recorded runs of the same `case_id`
  (`plimsoll/passk.py`): the tau-Bench reliability view (`pass^k` = fraction of tasks whose
  every recorded run passed), computed deterministically and offline from the per-run verdicts
  Plimsoll already produces. Report-only by default; `--passk-threshold` arms it as a CI gate
  (`--passk` selects K). The `reliability` block threads through the JSON/HTML/JUnit/SARIF/Markdown
  reports.
- **Reliability decay curve with a calibrated confidence band** (`plimsoll/stats.py`,
  `plimsoll/passk.py`): the fixed-`k` `pass^k` point is upgraded to a curve. The pooled per-run
  success probability `p` is estimated with a **Wilson score interval** (calibrated, not a magic
  constant — Acklam's inverse-normal quantile makes any confidence level exact, and Wilson keeps
  near-nominal coverage at the small `n`/extreme `p` where Wald collapses), and `pass^k = p^k` is
  projected as a band `[p_low^k, p_high^k]` (an exact CI via the monotone transform). Surfaces the
  Reliability Decay Curve, `k*` (largest `k` clearing the SLA), the **Meltdown Onset Point**, the
  per-run reliability as the governing invariant (no positive asymptote for `p < 1`; sample-`k`
  decay over a *fixed* gold set, never extrapolated over gold-set size), and optional
  rank-balanced per-task-duration buckets when trace data carries durations.
- **`--reliability-sla` honest worst-case gate**: a CI gate on the *lower* edge of the Wilson
  `pass^k` band (rule `reliability_sla`, distinct from the model-free `reliability_pass_k` floor),
  so a lucky small-`n` run with a wide band cannot certify the SLA. `--reliability-confidence` sets
  the band width (default `0.95`). Both gates fail the build independently and emit their own
  JUnit testcase and SARIF result.
- **Cheap → expensive cascade telemetry** (`plimsoll/cascade.py`, `--cascade`): measures
  Plimsoll's one real deterministic boundary — the pre-execution gate vs. the full post-hoc audit
  — by replay, at zero model spend, emitting the suite-wide contract shape `{alpha,
  disagreementRate, losslessViolations}` per boundary plus a regime/residual-locus label for every
  gate (model-free/provable vs. model-based residual). The gate's rule subset is provably contained
  in the audit's, so `losslessViolations` is 0 and disagreement is always in the safe direction.
- **Whole-plan policy dry-run** (`Governor.dry_run_plan`, `plimsoll governor --plan`): the stage-1
  feasibility / scoreTrace seam — gates an entire proposed plan against the policy without
  executing a tool or spending a token, returning a per-step verdict, the first blocking step, and
  a deterministic feasibility score (exit `0` feasible / `1` infeasible). Exact within the gate's
  decidable rule subset, so a deterministic-first planner can prune infeasible trajectories before
  paying an expensive model to score them.
- A runnable 12-case head-to-head benchmark against promptfoo on deterministic
  trace-regression detection (`examples/benchmark/` + `docs/BENCHMARK_vs_promptfoo.md`).
  Every Plimsoll case is run for real; the scorecard is honest about ties, the one
  budget-gating case promptfoo wins, and the full-trace-scan / ordering / packaging
  cases Plimsoll wins.
- `docs/RELATED_WORK.md` — where Plimsoll sits against promptfoo, agentevals, DeepEval,
  Ragas, Inspect AI, and the AgentAssay paper, and what is genuinely differentiated.

### Changed

- `pyproject.toml` declares an explicit `dependencies = []` so the zero-runtime-dependency
  contract is literal in the metadata.

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

- Terminal demo recording (`demo/demo.gif`), rendered from the committed vhs tape and
  embedded at the top of the README.
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
