# The reliability decay curve: statistics and honest limits

This is the reference for what `--reliability-sla` and `--reliability-confidence` compute
and why. The README's [reliability section](../README.md#reliability-passk-over-repeated-runs)
covers day-to-day use; this page defines every term that appears in the output and the
modelling choices behind them. Everything here is computed offline from recorded per-run
verdicts — no re-runs, no LLM, no tokens.

## Two gates, two kinds of claim

Plimsoll has two independent reliability gates over repeated recorded runs of the same
`case_id`. They fail the build independently and emit their own JUnit testcase and SARIF
result.

| Flag | Rule id | What it gates | Kind of claim |
| --- | --- | --- | --- |
| `--passk-threshold` | `reliability_pass_k` | the combinatorial `pass^k` point estimate (per-task `C(c, k) / C(n, k)`) | model-free: a count over recorded runs, no distributional assumption |
| `--reliability-sla` | `reliability_sla` | the **lower edge** of a confidence band on `pass^k` | model-based: assumes i.i.d. Bernoulli runs, quantifies uncertainty |

The first is a pure count and can be lucky: two passing runs report `pass^2 = 1.0`, which
would certify an agent on two data points. The second exists to make that luck visible.

## The model

Treat each recorded run as an independent draw with a single success probability `p`
(success = the run has no critical/high finding). Pool all runs of the task set, estimate
`p`, and project the probability that **all `k` runs pass** as the decay curve
`pass^k = p^k`. For any per-run `p < 1` this curve decays to **zero** as `k` grows — there
is no positive floor — which is why the per-run reliability `p` is the number the report
treats as the governing invariant.

## Why a Wilson interval, not a Wald interval

The uncertainty on `p` is reported as a **Wilson score interval** (Wilson, 1927), not the
textbook Wald interval `p ± z·sqrt(p(1−p)/n)`. At the small `n` and extreme `p` that agent
reliability lives at, the Wald interval is badly miscalibrated: it can fall outside
`[0, 1]` and collapses to width 0 at `p = 0` or `p = 1` — exactly the "lucky 2/2" case it
is supposed to guard against. Wilson inverts the score test instead, stays inside `[0, 1]`,
and keeps near-nominal coverage at small samples. (For the small-sample case against
normal/CLT intervals in LLM evals, see arXiv:2503.01747.)

The normal quantile behind the band is computed with Acklam's rational approximation of
the inverse standard-normal CDF (relative error ~1.15e-9, pure arithmetic, no SciPy), so
`--reliability-confidence` accepts **any** level in `(0, 1)` rather than a hard-coded
table. The default is `0.95`.

## The projected band is exact

The Wilson interval `[p_low, p_high]` for `p` is projected to
`[p_low^k, p_high^k]` for `pass^k`. Because `x → x^k` is monotone on `[0, 1]`, a monotone
transform of a confidence interval is a valid confidence interval — the projected band is
an *exact* interval for `p^k` at the same confidence level, not an approximation stacked
on an approximation.

## Reading the headline

A worked failing example, run on the committed flaky fixture (three recorded runs of one
checkout task; one run bypasses the required authorization):

```bash
plimsoll run --input examples/reliability/flaky --policy examples/reliability/policy.json \
  --out runs/rel-flaky --reliability-sla 0.9 --reliability-confidence 0.95
# Plimsoll: 2/3 passed, avg score 85.0, findings: 1 critical
# Plimsoll reliability: pass@1=0.667 pass^3=0.000 over 1 task(s) x up to 3 run(s) | p=0.667 [0.208,0.939] pass^3 band [0.009,0.827] SLA 0.900: k*=0 MOP=1 [CI gate: FAIL]; flaky: checkout
```

Segment by segment:

- `p=0.667 [0.208,0.939]` — the pooled per-run success rate (2 of 3 runs) with its 95%
  Wilson interval.
- `pass^3 band [0.009,0.827]` — that interval projected to `k = 3`: the probability that
  three consecutive runs all pass is somewhere in this range.
- `k*=0` — the largest `k` whose **lower** band edge still clears the SLA. `0` means not
  even a single run can be certified at 0.90 from this evidence.
- `MOP=1` — the **Meltdown Onset Point**, the first `k` at which the SLA breaks
  (always `k* + 1`).
- `[CI gate: FAIL]` — the `reliability_sla` verdict. The gate is armed on the lower band
  edge, so it is a worst-case gate: it passes only when even the most pessimistic
  reliability consistent with the observed runs clears the SLA.

## Three perfect runs do not certify a 90% SLA

The same command on the committed **stable** fixture (all three runs pass) still fails the
SLA gate:

```bash
plimsoll run --input examples/reliability/stable --policy examples/reliability/policy.json \
  --out runs/rel-stable --reliability-sla 0.9 --reliability-confidence 0.95
# Plimsoll: 3/3 passed, avg score 100.0, findings: none
# Plimsoll reliability: pass@1=1.000 pass^3=1.000 over 1 task(s) x up to 3 run(s) | p=1.000 [0.439,1.000] pass^3 band [0.084,1.000] SLA 0.900: k*=0 MOP=1 [CI gate: FAIL]
```

The point estimate is perfect, but the Wilson lower bound on three successes is `0.439` —
three runs simply cannot distinguish a 95%-reliable agent from a 50%-reliable one that got
lucky. This is deliberate: the gate refuses to certify what the sample cannot support.
Record more runs to narrow the band. (The model-free `--passk-threshold` gate, by
contrast, would pass here — it gates the observed count, not the uncertainty.)

## Duration buckets

When trace data carries per-run durations, the curve adds **rank-balanced
per-task-duration buckets** — tasks are sorted by duration and split into equal-size
groups, and each bucket reports its own per-run reliability with its own Wilson interval.
The buckets are **descriptive only**: no cross-bucket significance is computed or claimed.
They exist to make "the agent is less reliable on long tasks" visible, not to prove it.

## What the curve refuses to extrapolate

The decay is a **sample-`k` saturation over a fixed gold set**: it answers "if I re-run
*these recorded tasks* `k` times, how likely is a clean sweep?". It is **not** an
extrapolation over gold-set *size*, and it reports no positive asymptote — for any
`p < 1` the all-`k`-pass probability goes to 0, and the report says so
(`asymptote: 0.0` with an explanatory note in `report.json`) instead of inventing a floor.

## Cascade telemetry metrics

`--cascade` replays Plimsoll's one real cheap → expensive boundary — the pre-execution
**gate** (the rules decidable before a call runs) versus the full post-hoc **audit** (every
rule over the complete trace) — and writes one block per boundary into `report.json`:

- **`alpha`** — the fraction of traces the cheap tier resolves before execution: how much
  work never has to reach the expensive tier.
- **`disagreementRate`** — the fraction of traces where the two tiers' verdicts differ.
- **`losslessViolations`** — the number of times the cheap fast path produced a verdict the
  audit would reverse. For the gate/audit boundary this is zero **by construction**: the
  gate enforces a strict subset of the audit's rules, so a gate block is always also an
  audit failure.

The replay re-evaluates recorded traces against both tiers deterministically and offline,
so measuring the cascade costs zero model spend.

## Where this sits among Plimsoll's gates

Every other Plimsoll gate is a provable predicate over the recorded trace — set
membership, ordering, arithmetic thresholds, exact matching. The reliability curve is the
one deliberately model-based gate in the tool. `report.json` labels this boundary
explicitly: with `--cascade`, every gate carries a `regime` field (`model-free / provable`
vs `model-based residual`) and a `locus` field naming the level its evidence points at
(`turn`, `claim`, `action`, `step`, or `chunk`); the curve's own block carries
`regime: "model-based residual"` and `locus: "turn"` — the residual it models is whole-run
flakiness. The labels keep the boundary honest: what Plimsoll proves is marked proved, and
the one thing it estimates is marked estimated.
