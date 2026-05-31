# Case Study: Support Ticket Triage Regression Caught by Plimsoll

## Problem

Agent regressions often hide inside the path a system took, not only the final response. A support agent can produce plausible text while using the wrong tool, retrying with a different query, leaking sensitive-looking values into traces, or exceeding a budget.

Plimsoll's sample scenario is a small support-agent workflow:

1. Read a billing ticket.
2. Search policy docs.
3. Summarize the routing decision.

## Baseline

The baseline run correctly identifies a refund policy mismatch:

```bash
python3 -m plimsoll.cli run \
  --input examples/traces/current_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out runs/sample
```

Result: `1/1 passed`, score `100`.

## Regression

The regressed run starts from the same ticket but drifts after a failed search retry:

- It changes the retry query from refund policy to shipping policy.
- It drafts an email through a forbidden tool.
- It exceeds the token and estimated-cost budgets.
- It carries sensitive-looking data into trace evidence.

```bash
python3 -m plimsoll.cli run \
  --input examples/traces/regressed_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out examples/output/regression-demo
```

Result: `0/1 passed`, score `0`, with nine findings.

## What Plimsoll Catches

- Final output no longer contains the expected policy mismatch.
- The tool sequence diverges from the baseline.
- The retry changes tool input after an error.
- A `send_email` call falls outside the policy's tool allowlist.
- The same `send_email` call also matches the forbidden-tools list.
- Token and estimated-cost budgets are exceeded *(two findings)*.
- PII and secret-like token patterns appear in trace text *(two findings)*.

## Evidence Artifacts

- `examples/output/regression-demo/report.json`
- `examples/output/regression-demo/report.html`

The HTML report leads with a pass/fail verdict, then a top finding summary, severity badges, a compact trajectory timeline, and metric deltas. The verdict uses a glyph, a label, and color together so it does not rely on color alone, and the report follows the reader's light or dark theme. The JSON report keeps the same data machine-readable for CI or follow-up analysis.

## What CI Would Catch

Any critical or high-severity finding returns a non-zero exit code by default; pass `--exit-zero` for report-only mode. Adding `--junit` and `--sarif` writes `report.junit.xml` and `report.sarif.json` alongside `report.json`; `scripts/smoke.py` runs that invocation against `runs/ci-demo/`, so `runs/ci-demo/report.junit.xml` and `runs/ci-demo/report.sarif.json` are the CI evidence artifacts. The JUnit output makes the case visible as a failed testcase. The SARIF output records Plimsoll rules and per-finding results anchored to the committed policy file's lines, so findings render in GitHub code scanning. Adding `--md` writes a Markdown summary for PR comments, and inside GitHub Actions the same summary is appended to the job's run summary automatically.

## Why This Matters

Final-answer checks alone would miss the workflow drift. Plimsoll keeps the proof local and deterministic: the reviewer can inspect the trace, policy, JSON report, HTML report, JUnit XML, and SARIF JSON without an account, model call, hosted service, or production data.

Fixtures are shaped from public documentation and validated against Plimsoll's adapter expectations, as described in `PUBLIC_TRACE_VALIDATION.md`. This keeps interoperability claims concrete without pretending the fixtures are exhaustive SDK exports.

## Limitations

This case study uses sanitized fixtures. It does not prove full compatibility with every tracing SDK, and it does not replace semantic human review. The value is narrower: deterministic local evidence that a candidate agent run violated explicit workflow, budget, baseline, and privacy-style checks.
