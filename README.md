# Plimsoll

**A deterministic, zero-dependency CLI that gates recorded AI-agent traces in CI: no re-runs, no LLM judge, no account.**

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/theo-ai-lab/plimsoll/actions/workflows/ci.yml/badge.svg)
![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20deps-0-blue)
![Output: JSON · HTML · JUnit · SARIF · Markdown](https://img.shields.io/badge/output-JSON%20%C2%B7%20HTML%20%C2%B7%20JUnit%20%C2%B7%20SARIF%20%C2%B7%20MD-informational)

![Terminal demo: a clean trace passes the gate, then a regressed trace fails with findings broken down by severity](demo/demo.gif)

![Plimsoll HTML report showing a caught regression](docs/assets/report.png)

![How Plimsoll works: a recorded agent trace flows through an adapter, is checked against a policy and baseline by deterministic rules, and emits JSON/HTML/JUnit/SARIF/Markdown reports plus a CI pass/fail exit code, fully offline with zero runtime dependencies](docs/assets/architecture.png)

You changed a prompt, a model, or some tool wiring. Your unit tests still pass, yet the agent quietly started calling the wrong tool, dropped a step, took three extra turns, or leaked a secret into a log, and you found out from a user. **Plimsoll is the deterministic floor under that problem:** point it at a trace you already recorded, check it against a declarative JSON policy and a known-good baseline, and fail the build on a regression, reproducibly and offline, with no tokens spent. It emits every format CI actually consumes (JSON, HTML, JUnit, SARIF, Markdown) and exits non-zero when a gate fails. It is *not* an LLM judge and does not score semantic quality; it is the cheap, reproducible layer you run on every PR.

**One deterministic engine, three surfaces, zero runtime dependencies** — same pure-stdlib rules, same input, same finding, no model call:

- **CI gate** — `plimsoll run` checks a recorded trace against a policy and baseline and fails the build on a regression (the quickstart below).
- **Runtime governor** — `plimsoll governor`, and the `plimsoll-governor` MCP server, gate a proposed tool call *before* it executes ([Runtime governor](#runtime-governor-gate-a-tool-call-before-it-runs)).
- **Reliability gate** — `--passk-threshold` fails the build when an agent is flaky across repeated runs of the same task, and `--reliability-sla` adds an honest worst-case gate on the *lower* edge of a calibrated confidence band so a lucky small-n run cannot sneak past ([reliability curve](#reliability-passk-over-repeated-runs)).

## Quickstart

Requires Python 3.11+. No runtime dependencies. The quickstart reads example traces committed in this repo, so start from a clone:

```bash
git clone https://github.com/theo-ai-lab/plimsoll && cd plimsoll
python -m pip install -e .
plimsoll --version
```

Inside a clone no install is needed at all — `python -m plimsoll` behaves identically to the `plimsoll` script. To install without cloning, `python -m pip install git+https://github.com/theo-ai-lab/plimsoll` works today; `pip install plimsoll` is pending the PyPI publish.

Check a clean run against a baseline (exits `0`):

```bash
plimsoll run \
  --input examples/traces/current_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out runs/sample
# Plimsoll: 1/1 passed, avg score 100.0, findings: none
```

Now check a regressed run. Plimsoll catches nine issues and exits `1`:

```bash
plimsoll run \
  --input examples/traces/regressed_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out runs/regression
# Plimsoll: 0/1 passed, avg score 0.0, findings: 3 critical, 3 high, 3 medium  -> exit 1
```

Reports land in `runs/<name>/report.json` and `report.html`.

## What it checks

Every check is deterministic: same trace in, same findings out, no model call.

- **Expected output:** exact or `contains` matching against `expected_output`.
- **Tool policy:** allowlist, forbidden tools, and required tools.
- **Budgets:** step count, duration, tokens, and estimated cost ceilings.
- **Repeated actions:** identical tool calls beyond a configured limit.
- **Retry drift:** a retry that silently changed its input after an error.
- **Sensitive data:** conservative PII and secret-like/high-entropy patterns across inputs, outputs, errors, attributes, and the final answer.
- **Baseline regression:** tool-sequence edit distance from a known-good trace, plus tolerant **trajectory matching** (see below).
- **Ordering invariants:** `must_precede` requires one tool to run before another (e.g. approvals before a privileged action). It checks order, not presence, so a refusal/escalation path stays valid.

A minimal policy is just JSON:

```json
{
  "allowed_tools": ["read_ticket", "search_docs", "summarize"],
  "forbidden_tools": ["delete_file", "deploy"],
  "max_steps": 6,
  "max_estimated_cost_usd": 0.02,
  "trajectory_match_mode": "superset"
}
```

Findings are severity-weighted into a 0–100 score; a case **fails** if it has any critical or high finding. A JSON Schema for the full policy lives at [`docs/policy.schema.json`](docs/policy.schema.json) — reference it from your policy's `$schema` for editor autocomplete.

### Tolerant trajectory matching

Stochastic agents reach correct outcomes by different valid paths, so exact-sequence matching is brittle. `trajectory_match_mode` grades the tool trajectory against the baseline with a spectrum of strictness (mirroring the agentevals conventions; baseline = reference):

| Mode | Passes when… | Catches |
| --- | --- | --- |
| `strict` | same tools in the same order | any reordering or change |
| `unordered` | same multiset of tools, any order | added or dropped tools |
| `superset` | the run contains at least every baseline tool call (extras allowed) | **dropped** steps |
| `subset` | the run introduces no tool calls beyond the baseline (omissions allowed) | **unexpected** steps |

### Ordering invariants (`must_precede`)

Some failures are about *order*, not drift. `must_precede` declares pairs where one tool must occur before another:

```json
{
  "must_precede": [
    {"before": "manager_review", "after": "security_review"},
    {"before": "security_review", "after": "grant_access"}
  ]
}
```

This catches the canonical agent-safety failure (performing a privileged action before its required approvals) as a **critical** finding. The rule constrains ordering only: an agent that refuses and escalates (never performing `after`) passes, so the gate never forces the high-risk action to occur. See the worked scenario below.

## Runtime governor: gate a tool call before it runs

The same deterministic engine also runs as a **pre-execution gate**. Instead of auditing a
finished trace, the governor answers a live question — *given everything that has run so far,
is it safe to make this next tool call?* — and blocks it before it executes. It enforces only
the rules decidable before a call runs (allowlist/forbidden membership, `must_precede`
ordering, cumulative budgets, repeated-action limits); rules that need the call's result or the
finished trajectory stay deferred to the post-hoc `run` audit. Still no LLM, no network, no
third-party import.

`plimsoll governor` is the one-shot CLI gate. It reads a proposed tool call as JSON (a
tool-name string, or an object with a `tool` field plus optional `input`/token/cost hints) from
`--call` or stdin, takes the calls that already ran via `--partial-trace`, and exits non-zero
when a rule blocks it:

```bash
# Forbidden tool: blocked outright (exit 1).
echo '{"tool": "deploy"}' | plimsoll governor --policy examples/policies/default_policy.json
# Plimsoll governor: block 'deploy'
#   - tool_allowlist [critical]: 'deploy' is not in the allowlist.
#   - forbidden_tool [critical]: 'deploy' is forbidden by policy.

# Ordering: the task's goal action, proposed before its required approvals have run.
echo '["read_record"]' > runs/prior.json
echo '{"tool": "grant_access"}' | plimsoll governor \
  --policy examples/mcp-governor-session/policy.json --partial-trace runs/prior.json
# Plimsoll governor: block 'grant_access'
#   - tool_order [critical]: 'grant_access' occurred before the required 'manager_review'.
#   - tool_order [critical]: 'grant_access' occurred before the required 'security_review'.
```

For a long-running integration, `plimsoll-governor` serves the same gate over MCP (stdio) as
two tools — `propose_tool_call` (the gate) and `check_trace` (the full audit) — so an MCP host
or agent loop can consult it on every proposed tool call:

```bash
python -m pip install -e '.[mcp]'   # the mcp SDK is an optional extra; the core stays zero-dependency
plimsoll-governor --policy examples/mcp-governor-session/policy.json
```

[docs/MCP_DEMO.md](docs/MCP_DEMO.md) has the host wiring (`.mcp.json`) and a committed,
replayable JSON-RPC session against the real server —
[`examples/mcp-governor-session/`](examples/mcp-governor-session/) — showing an allow, a
`tool_order` deny of the task's own goal action, and a `max_tokens` budget block, all
decided before execution.

If you would rather not depend on the MCP SDK at all, `plimsoll.governor_mcp.make_handlers`
exposes the same two tools as plain `{name: callable}` JSON-in/JSON-out functions.
[`examples/governor_loop_demo.py`](examples/governor_loop_demo.py) wires the gate into a
scripted agent loop and verifies every decision against a ground-truth label.

## Reliability: `pass^k` over repeated runs

Stochastic agents are flaky — a task that passes once may fail on the next run. `pass^k` is the **tau-Bench reliability view**: record the *same* task `k` times and ask how often the agent gets it right *every* time, not just once. It is the fraction of tasks for which **all `k` recorded runs pass** (per-task estimator `C(c, k) / C(n, k)`; `pass^1 = pass@1` is the ordinary per-run rate, `pass^n` asks "did every recorded run pass?"). It is computed **deterministically and offline** — Plimsoll only counts the per-run verdicts it already produces (a run passes when it has no critical/high finding), grouped by `case_id`. No re-evaluation, no LLM, no tokens.

It is report-only by default and becomes a **CI gate** the moment you set `--passk-threshold`:

```bash
plimsoll run --input runs/ --policy policy.json --out out --passk-threshold 0.9
# Plimsoll reliability: pass@1=1.000 pass^1=1.000 over 1 task(s) x up to 1 run(s) [gate >= 0.900: PASS]
```

Point `--input` at multiple recorded runs of the same task (a directory or `.jsonl`, grouped by `case_id`) to get `k > 1`; below the threshold the gate fails the build (exit `1`). The `reliability` block (the full `pass^j` curve, per-task results, and gate verdict) is written into `report.json` and carried through the HTML/JUnit/SARIF/Markdown outputs.

A committed, runnable example lives in [`examples/reliability/`](examples/reliability/) — a **stable** fixture (three runs of one task, all pass → `pass^3 = 1.0`, gate passes) and a **flaky** one (a run bypasses a required approval → `pass^3 = 0.0`, gate fails). The project's own CI runs both as a self-test, and [`examples/ci/github-actions.yml`](examples/ci/github-actions.yml) documents the `--passk-threshold` step.

### Reliability decay curve and the worst-case SLA gate

A point estimate hides its own uncertainty: a lucky `2/2` run reports `pass^2 = 1.0`, which would certify an agent on two data points. `--reliability-sla` is the honest version of that gate — it wraps the observed per-run pass rate in a calibrated confidence band (a Wilson score interval), projects the band across repeated runs, and fails the build unless even the **worst case consistent with the observed runs** clears your SLA (rule `reliability_sla`, independent of the `reliability_pass_k` floor above).

```bash
plimsoll run --input runs/ --policy policy.json --out out --reliability-sla 0.9 --reliability-confidence 0.95
```

The summary and reports show the projected reliability curve, the deepest run count that still clears the SLA, and the first run count where it breaks. The honest caveat: with only a handful of recorded runs the band is wide, so even a perfect small sample will not certify a high SLA — that is the gate working, and recording more runs is the only way to narrow it. The statistics behind the curve (why Wilson, what each headline segment means, what the curve deliberately does not extrapolate) are worked through in [`docs/RELIABILITY.md`](docs/RELIABILITY.md).

### Cheap → expensive cascade telemetry (the deterministic floor)

Plimsoll is the cheap, deterministic layer in front of expensive model-based evaluation, and `--cascade` measures how much work that layer resolves. Its one real cheap → expensive boundary is internal: the pre-execution **gate** (the rules decidable before a call runs) versus the full post-hoc **audit** (every rule over the complete trace). `--cascade` replays that boundary — **zero model spend** — and emits, per boundary, `alpha` (fraction the cheap tier resolves before execution), `disagreementRate` (where the two tiers' verdicts differ), and `losslessViolations` (times the cheap fast path produced a verdict the audit would reverse — zero by construction, since the gate enforces a strict subset of the audit's rules).

> **Measured** over the committed access-request corpus (`examples/access-request/traces`, 3 traces): Plimsoll's deterministic gate tier resolves **33% (1/3)** of traces before they execute at **0 lossless violations** and **0% disagreement** against the full audit.

The same boundary is exposed directly as a **whole-plan dry-run**: `plimsoll governor --plan plan.json` gates an *entire* proposed plan against the policy before anything executes — no tool call made, no token spent — returning a per-step feasibility verdict and a deterministic score (exit `0` feasible / `1` infeasible). Rules that need a call's result (output match, leakage, drift) are not decidable on an un-executed plan and stay deferred to the post-hoc audit — exactly the gate/audit boundary the cascade measures.

## Why use Plimsoll

- **Deterministic and reproducible.** No LLM-as-judge, so there is no flakiness or per-run token cost, and results are identical for every reviewer. Checks you can express as code are the cheapest and most reliable layer — run them first, and reserve judgment-based evaluation for what genuinely needs semantic judgment.
- **Local-first.** It reads local files and writes local reports; there is no account, server, telemetry, or API key involved.
- **Zero runtime dependencies.** Pure standard library; clone-and-run in seconds.
- **CI-native.** Tri-state exit codes, plus JUnit, SARIF, and a Markdown summary that the GitHub Actions step summary, PR comments, and the Security tab all consume.

## Why *not* Plimsoll

- It is **not an LLM judge** and does not evaluate semantic quality — tone, helpfulness, factual grounding, or reasoning quality. Pair it with a judge for those; Plimsoll is the deterministic gate, not the whole eval stack.
- It is **not an observability platform** like Phoenix, Braintrust, or Langfuse — it has no dashboards, live ingestion, or hosted storage.
- It is **not the only deterministic, account-free option.** [promptfoo](https://www.promptfoo.dev/docs/configuration/expected-outputs/deterministic/) already ships offline deterministic trajectory assertions (`trajectory:tool-sequence`, `trajectory:tool-args-match`), and Plimsoll's match-mode vocabulary mirrors [agentevals](https://github.com/langchain-ai/agentevals). Plimsoll's edge is packaging and posture — zero-install stdlib, policy-line-anchored SARIF, `must_precede` ordering, retry-drift — not new technique. See [`docs/RELATED_WORK.md`](docs/RELATED_WORK.md) and the runnable [head-to-head benchmark](docs/BENCHMARK_vs_promptfoo.md).
- The framework adapters below are **documented subset shims for local fixtures**, not full SDK integrations.
- It checks only the trace fields it receives; missing instrumentation means missing evidence.

## Trace formats and adapters

The default `--format native` reads Plimsoll's compact JSON (a single `.json`, a directory of them, or `.jsonl` with one `run` row then `span` rows). Adapters normalize other shapes into the same internal model:

| Format | Maps | Intentionally unsupported |
| --- | --- | --- |
| `native` | Plimsoll fields, tool sequence, token/cost attributes | — |
| `otel` | OTLP-style spans, timestamps, status, GenAI usage attributes | collectors, binary OTLP, arbitrary resource metadata |
| `openinference` | OTel-shaped spans with OpenInference-style kind/token attributes | full OpenInference SDK coverage |
| `langgraph` | LangGraph-inspired local event fixture → spans | real LangGraph export schema |
| `openai-agents` | trace/span fixture shape, usage, ISO timestamps | live Agents SDK export |

Adapters are import shims for local JSON, not collectors, exporters, or SDK compatibility layers. One fixture is a **real** pydantic-ai OpenTelemetry export captured offline (no network, no API key), and the generic OTel adapter ingests it with no framework-specific code (the clean run passes; a bypass run is caught as a critical ordering violation; validating it drove two general gen_ai-correctness fixes to the adapter). See [`PUBLIC_TRACE_VALIDATION.md`](PUBLIC_TRACE_VALIDATION.md) for how the fixtures are validated, and [`SCHEMA.md`](SCHEMA.md) for exact contracts.

## Reports and CI integration

A single run writes `report.json` and `report.html`; add flags for the rest:

```bash
plimsoll run --input traces/ --policy policy.json --baseline baseline/ \
  --out plimsoll-out --junit --sarif --md
```

- **HTML:** a self-contained, dark-mode-aware report with a verdict banner, a finding summary, and a trajectory diff. Status is encoded with an icon, a label, *and* color (never color alone).
- **JUnit XML:** one `testcase` per trace case, for CI test reporters.
- **SARIF 2.1.0:** each finding is anchored to the committed **policy file at the line of the rule that triggered it** (with `region.startLine`, `partialFingerprints`, and `automationDetails`), so results render in the GitHub code-scanning Security tab.
- **Markdown** (`--md`): a verdict + findings table for PR comments. Inside GitHub Actions, Plimsoll also appends this summary to `$GITHUB_STEP_SUMMARY` automatically (no extra action, no extra permissions).

[`examples/ci/github-actions.yml`](examples/ci/github-actions.yml) is a copy-paste workflow demonstrating all four surfaces (step summary, SARIF upload, JUnit check run, sticky PR comment) with minimally scoped permissions.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Ran successfully; no failing findings (or `--exit-zero` was passed) |
| `1` | Ran successfully; at least one case has a high/critical finding |
| `2` | Could not run — invalid input, policy, or arguments |

Failing findings exit `1` **by default** (so a CI gate fails closed). Pass `--exit-zero` for advisory, non-blocking runs; the report still records every finding. (`--fail-on-findings` is retained as a deprecated no-op alias.)

Machine-readable output (`--json`) goes to stdout; the human summary goes to stderr, so `plimsoll ... --json > summary.json` stays byte-clean.

## Bootstrapping a policy

```bash
plimsoll init-policy --input examples/traces/current_ticket_triage.json --out runs/policy.json
```

This infers observed tools, required tools, step/budget ceilings, and a default baseline-drift threshold. Review it before using it as a gate.

## Proof and examples

Checked-in, deterministic artifacts you can inspect without running anything:

- [`examples/output/regression-demo/report.html`](examples/output/regression-demo/report.html) — the canonical failing report (nine findings).
- [`examples/output/golden/`](examples/output/golden/) — golden JSON/HTML/JUnit/SARIF for the clean and regressed cases. Regenerate with `python scripts/build_golden.py`.

For the product narrative behind the sample, read [`CASE_STUDY.md`](CASE_STUDY.md).

## Reference scenario: IT access-request

A worked, end-to-end reliability loop on a high-stakes workflow: an AI assistant that handles privileged IT access requests must never call `grant_access` before a completed `manager_review` and `security_review`. [`examples/access-request/`](examples/access-request/) holds a deterministic reference agent, the access-control policy, seven adversarial probes, a workflow risk plan, and committed clean/failed/fixed traces and reports.

- **Before:** under emergency pressure and an unverified "claimed" approval, the agent grants access early. Plimsoll flags a **critical** `tool_order` bypass (plus a wrong-output finding) and fails the build.
- **After:** the fixed agent refuses, prepares an approval packet, and escalates: no `grant_access`, no findings, build passes.

Regenerate it with `python scripts/build_access_request_demo.py`. Read [`BEFORE_AFTER.md`](BEFORE_AFTER.md), [`EVAL_PLAN.md`](EVAL_PLAN.md), and [`RISK_REGISTER.md`](RISK_REGISTER.md) for the full writeup, evaluation plan, and risk register. This is a **reference scenario on synthetic data**, not a production access-control system.

## Tests and local checks

```bash
python -m pip install -e '.[dev]'      # adds ruff (the only dev dependency)
python -m unittest discover -s tests   # 237 tests
ruff check .
python scripts/validate_public_fixtures.py
```

## Privacy and local-only behavior

Plimsoll does not start a server, call an LLM, send telemetry, upload traces, or require API keys. It reads local files and writes local reports. Reports may contain excerpts copied from your trace evidence; keep real production traces out of version control unless sanitized. See [`SECURITY.md`](SECURITY.md).

## Limitations

- A deterministic harness, not an LLM judge; it does not prove semantic correctness beyond the configured expected output and policy.
- It only checks the trace fields it receives; missing instrumentation means missing evidence.
- PII and secret-like checks are regex-based, conservative, and can false-positive or miss domain-specific data.
- Baseline drift uses tool-sequence edit distance and the trajectory match modes above, not full state-machine equivalence.
- Non-native adapters cover documented subsets of local JSON fixtures, not full framework SDKs.

## Roadmap

- Outcome/state assertions and per-tool argument matching, alongside the sequence modes.

See [`CHANGELOG.md`](CHANGELOG.md) for release history and [`CONTRIBUTING.md`](CONTRIBUTING.md) to get involved.

## License

MIT. See [`LICENSE`](LICENSE).
