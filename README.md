# Plimsoll

**A deterministic, zero-dependency CLI that gates recorded AI-agent traces in CI: no re-runs, no LLM judge, no account.**

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/theo-ai-lab/plimsoll/actions/workflows/ci.yml/badge.svg)
![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20deps-0-blue)
![Output: JSON · HTML · JUnit · SARIF · Markdown](https://img.shields.io/badge/output-JSON%20%C2%B7%20HTML%20%C2%B7%20JUnit%20%C2%B7%20SARIF%20%C2%B7%20MD-informational)

![Plimsoll HTML report showing a caught regression](docs/assets/report.png)

![How Plimsoll works: a recorded agent trace flows through an adapter, is checked against a policy and baseline by deterministic rules, and emits JSON/HTML/JUnit/SARIF/Markdown reports plus a CI pass/fail exit code, fully offline with zero runtime dependencies](docs/assets/architecture.png)

You changed a prompt, a model, or some tool wiring. Your unit tests still pass, yet the agent quietly started calling the wrong tool, dropped a step, took three extra turns, or leaked a secret into a log, and you found out from a user. **Plimsoll is the deterministic floor under that problem:** point it at a trace you already recorded, check it against a declarative JSON policy and a known-good baseline, and fail the build on a regression, reproducibly and offline, with no tokens spent. It emits every format CI actually consumes (JSON, HTML, JUnit, SARIF, Markdown) and exits non-zero when a gate fails. It is *not* an LLM judge and does not score semantic quality; it is the cheap, reproducible layer you run on every PR.

## Quickstart

Requires Python 3.11+. No runtime dependencies.

```bash
python -m pip install -e .        # or: pip install plimsoll (once published)
plimsoll --version
```

Check a clean run against a baseline (exits `0`):

```bash
plimsoll run \
  --input examples/traces/current_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out runs/sample
# Plimsoll: 1/1 passed, avg score 100.0, findings {}
```

Now check a regressed run. Plimsoll catches nine issues and exits `1`:

```bash
plimsoll run \
  --input examples/traces/regressed_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out runs/regression
# Plimsoll: 0/1 passed, avg score 0.0, findings {'high': 3, 'critical': 3, 'medium': 3}  -> exit 1
```

Reports land in `runs/<name>/report.json` and `report.html`. (`python -m plimsoll ...` works identically to the `plimsoll` console script.)

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

## Why use Plimsoll

- **Deterministic and reproducible.** No LLM-as-judge, so there is no flakiness or per-run token cost, and results are identical for every reviewer. Checks you can express as code are the cheapest and most reliable layer — run them first, and reserve judgment-based evaluation for what genuinely needs semantic judgment.
- **Local-first.** It reads local files and writes local reports; there is no account, server, telemetry, or API key involved.
- **Zero runtime dependencies.** Pure standard library; clone-and-run in seconds.
- **CI-native.** Tri-state exit codes, plus JUnit, SARIF, and a Markdown summary that the GitHub Actions step summary, PR comments, and the Security tab all consume.

## Why *not* Plimsoll

- It is **not an LLM judge** and does not evaluate semantic quality — tone, helpfulness, factual grounding, or reasoning quality. Pair it with a judge for those; Plimsoll is the deterministic gate, not the whole eval stack.
- It is **not an observability platform** like Phoenix, Braintrust, or Langfuse — it has no dashboards, live ingestion, or hosted storage.
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
python -m unittest discover -s tests   # 67 tests
ruff check plimsoll tests
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
- `pass@k` / `pass^k` aggregation over repeated recorded runs for stochastic agents.

See [`CHANGELOG.md`](CHANGELOG.md) for release history and [`CONTRIBUTING.md`](CONTRIBUTING.md) to get involved.

## License

MIT. See [`LICENSE`](LICENSE).
