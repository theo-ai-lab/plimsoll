# Ship-gate ledger — plimsoll, 2026-08-02

Branch: `hardening/governor-session-state`. Two verdicts, reported separately
and never merged.

## What this gate caught

**A preregistration claim the repository cannot evidence.** The README and
ADR-0001 said the deny-list verb list was "predeclared before the first
measurement" and "committed before the first measurement". History shows the
plan, the scorer, the fixture corpus and the 12,259-line scorecard all first
appearing together in `df4fb02`. Preregistration is a claim about ordering in
time; it needs an immutable timestamp, and none exists here. Both sites now
claim the narrower thing the code does establish — the scorer is never passed a
label, so it cannot have fitted one.

**A benchmark whose title contradicted its own method section.**
`BENCHMARK_vs_promptfoo.md` called itself "a runnable, honest head-to-head on a
12-case suite" while its methodology stated plainly that only 4 of 12 cases ran
against promptfoo and the other 8 were `ANALYZED`, not `RUN` — and explicitly
declined to fabricate them. A reader who stopped at the summary table would have
counted 8 argued cells as measured ones. The asymmetry now leads the document.

## Scores

| # | Principle | Score | Evidence |
|---|---|---|---|
| P12 | Testing | **4** | **313 tests green from a cold clone with zero installs** — stdlib `unittest`, no venv, no network. The strongest cold-start story in the portfolio, and it matches the repo's zero-dependency thesis rather than merely asserting it. |
| P13 | CI/CD | **3** | Lint, format, `unittest discover`, `compileall`, smoke, plus release-guard and Pages workflows. **Evidence is the PR run.** |
| P14 | Observability | **3** | SARIF and JUnit emitters; findings anchored to policy lines. Known gap: `retry_drift` is absent from the rule-to-policy map and falls back to line 1, and the test asserts only `line >= 1`, which masks it. |
| P15 | Security fundamentals | **3** | Zero runtime dependencies is the security posture — no transitive supply chain to audit on the core path. Offline by construction. |
| P19 | Infrastructure | **4** | Genuinely dependency-free: `python -m plimsoll` works inside a clone with nothing installed. Optional extras (`mcp`, `realtraces`) are isolated and documented as optional. |
| P24 | Measurable success criteria | **3** | The 29.9% worst-case miss rate over 301 unsafe records is the honest denominator — it counts the 50 records with no tool call, which a tool gate is structurally blind to, as misses rather than dropping them. Scored 3 not 4 because the scorecard is recomputed from a committed ledger rather than regenerated from pinned sources, so a fabricated-but-consistent ledger would pass. |
| P32 | Graders | **3** | Externally graded against R-Judge — the only number in the repo not self-graded. Weakened by the same provenance gap above. |
| P36 | Onboarding / accurate mental models | **3** | External adversarial review by an independent frontier model from a different vendor. The two claims it falsified are fixed above. Presentation note left as the owner's call: the externally-graded miss rate — the most credible number here — sits at README ~line 297, past eight sections a 90-second reader never reaches. |

## Verdict 1 — Build Quality

**Strong, and unusually disciplined about scope.** A tool whose whole argument is
"zero dependencies, runs anywhere, offline" and which then actually runs 313
tests from a cold clone with nothing installed has earned its central claim in
the only way that counts.

The honest weakness is provenance rather than logic: the external scorecard is
trusted as committed data. Recomputing arithmetic from a ledger proves the
arithmetic, not the ledger.

## Verdict 2 — External Adoption / Production Validation

**Unproven. No external users.** The R-Judge grading is against a public
external corpus, which is genuinely better than a self-authored set and is the
strongest external signal in the portfolio — but it is a corpus, not a user. No
third party has adopted the gate, and the PyPI release is still pending.
