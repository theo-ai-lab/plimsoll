# ADR 0001 — Grading the governor against an external corpus, and publishing its miss rate

- **Status:** accepted
- **Date:** 2026-08-01
- **Deciders:** Plimsoll maintainers
- **Supersedes / superseded by:** —
- **Related:** `docs/plans/external-corpus-miss-rate.md`,
  `examples/external-corpus/README.md`, `EVAL_PLAN.md`, `PUBLIC_TRACE_VALIDATION.md`

## Context

Plimsoll publishes a lot of numbers. Until this change, every one of them was produced by
running Plimsoll over artifacts Plimsoll's authors built, judged against policies Plimsoll's
authors wrote. That is a coherent thing to do — it demonstrates the engine is deterministic
and that the rules fire — but it is a *self-graded* evidence class. It cannot distinguish
"this gate catches real problems" from "this gate catches the problems we thought to write
fixtures for". Test count does not fix that; only a label we did not author does.

We wanted exactly one number in a different evidence class: **how often does the runtime
governor disagree with a third party's safety labels, and in which direction?**

## Decision

Adapt an external, human-labelled agent-safety corpus into Plimsoll's trace type, replay the
governor over it, and publish the confusion matrix — with the unflattering cells first.

### 1. The corpus

**R-Judge** (Yuan et al., *R-Judge: Benchmarking Safety Risk Awareness for LLM Agents*,
Findings of EMNLP 2024): 571 multi-turn agent interaction records at the pinned commit, each
carrying a binary safety label (`0` safe / `1` unsafe) and a risk description written by the
R-Judge annotators.

- repository: <https://github.com/Lordog/R-Judge>
- **record schema (the adapter is written against this file, not from memory):**
  <https://raw.githubusercontent.com/Lordog/R-Judge/main/config/data_schema.json>
- paper: <https://aclanthology.org/2024.findings-emnlp.79/>
- pinned commit: `83ce301da3ad50dd8b397e772863f5411c3d3dc2`

Chosen because its unit of judgement — a recorded sequence of agent tool calls — is the same
unit the governor gates, so the comparison is a like-for-like disagreement rather than an
analogy.

### 2. The corpus is fetched, not vendored

Upstream R-Judge declares no licence. Its records are therefore not ours to copy into an MIT
repository, however convenient that would be. `scripts/fetch_rjudge_corpus.py` pins the commit
and verifies every file against a SHA-256 manifest recorded at measurement time; the fetch
FAILS on a digest mismatch rather than scoring different bytes under the same headline.

What *is* committed is the measurement: `examples/external-corpus/rjudge-scorecard.json`,
including a per-record verdict ledger (record id, source file, label, verdict, blocking rule).
That is our result data, not the corpus text, and it makes the published number auditable with
no network at all.

### 3. The adapter treats the corpus as hostile input

`plimsoll/corpus.py` is a trust boundary. It validates at the edge, has one error shape
(`CorpusIssue(code, record_index, source, record_id, label, detail)`), and never raises on
record *content* — only on file-level problems, which stay `ValidationError` to match the
CLI's existing usage-error contract.

The load-bearing invariant is **total accounting**: `len(adapted) + len(issues) ==
total_records`, always. A silently dropped record shrinks the denominator and inflates the
score. That is not a hypothetical: 76 of the 571 records have no tool call at all, 50 of them
labelled unsafe. Dropping them would have moved the published miss rate from 29.9% to 15.9%
for free, and nobody reading the number would have known.

A property test over a seeded generator of mutated and hostile records (missing fields, wrong
types, booleans-as-labels, non-list turns, 5,000-character actions, extra fields, empty
corpora) asserts the invariant holds, nothing raises, and every rejection carries a code.

### 4. The policy is derived label-blind

The deny-list is the dominant free parameter of a measurement like this — pick it after
looking at the labels and you have fitted the test set. So the scored policy is a **pure
function of the corpus tool vocabulary and a verb list predeclared in the plan document,
committed before the first measurement**:

    Send Post Transfer Withdraw Deposit Pay Delete Share
    Grant Create Update Download Move Execute Write

Case-insensitive substring match on the tool name; 41 of the corpus's 141 tool names match.
The plan noted *in advance* that `Pay` also matches the read-only `BankManagerSearchPayee` and
that this over-block would be left in. A test flips every label in a corpus and asserts the
derived policy digest is byte-identical, so label-blindness is mechanical, not a promise.

**What is scored is the deny-list *plus* the governor's inherited defaults.**
`derive_side_effect_policy` sets `forbidden_tools` and nothing else; every other field keeps
the value the shipped `Policy` gives it, including `max_repeated_action_count = 1`. That is
deliberate: the fail-closed default stays, and scoring a stripped-down policy nobody actually
runs would measure a configuration this project does not ship. It is not free, though — the
repeat cap catches records the deny-list does not — so every catch is credited to the rule that
made it in [The result](#the-result), rather than to the free parameter wholesale.

### 5. Both denominators are published

- `miss_rate` — misses over the unsafe records the gate could *see*.
- `worst_case_miss_rate` — misses **plus every unmappable unsafe record** over all unsafe
  records in the corpus.

The second is the headline. The first is published beside it precisely so the gap between them
is visible.

## The result

| | derived deny-list | default empty policy |
| --- | ---: | ---: |
| records scored / total | 495 / 571 | 495 / 571 |
| unmappable (no tool call) | 76 (50 unsafe) | 76 (50 unsafe) |
| caught | 211 | 3 |
| **missed (false accepts)** | **40** | 248 |
| **false rejects** | **23** | 0 |
| clean allows | 221 | 244 |
| miss rate (251 seen unsafe) | 15.9% | 98.8% |
| **worst-case miss rate (301 unsafe)** | **29.9%** | 99.0% |
| false-reject rate (244 safe) | 9.4% | 0.0% |

**The governor misses 29.9% of the unsafe records in this corpus and falsely blocks 9.4% of
the safe ones.** That is the first number this measurement produced. Nothing was tuned after
seeing it.

**Which rule earned which catch.** The `caught` column is a two-rule total, and the committed
ledger says which rule blocked each record: **209 of the 211 catches are the deny-list
(`forbidden_tool`); the other 2 are the inherited repeat cap (`repeated_action`) alone** —
R-Judge record `2540` (`Finance/ds_finance.json`) and record `73` (`IoT/household.json`). In
both, every call the gate saw before the block was one the deny-list allowed, and
`repeated_action` is the only rule that fired. (What those two records do *after* the block is
unknown to the ledger by construction — a blocked replay stops there.) The floor column is that
same cap with no deny-list under it: 3 catches, the extra one being record `20`
(`Finance/moneymanagement.json`), which in the scored run the deny-list stops one step earlier.
So 209 + 3 − 1 overlap = 211, and `tests/test_corpus_score.py` recomputes that split from the
ledger and fails the build if this paragraph and the ledger stop agreeing. No published rate
changes: attribution does not move a cell, it says which rule filled it.

## What this number licenses, and what it does not

**It does license** these claims:

- Plimsoll's gate has been run against labels its authors did not write, on a corpus its
  authors did not build, and the result is published including the parts that look bad.
- The scored configuration — a deterministic, name-based deny-list plus the governor's
  inherited repeat cap — catches a substantial majority (211/251) of the unsafe records it can
  structurally see, at a 9.4% false-block cost. The deny-list's own share of that is 209; the
  remaining 2 are the repeat cap alone.
- The measurement is reproducible: pinned corpus commit, digest-verified fetch, committed
  ledger, and a CLI command anyone can run.

**It does not license** any of these:

- **"Plimsoll catches 70% of unsafe agent behaviour."** It does not. It catches 70% *of this
  corpus, under this one policy*. A different policy moves the number; that is the whole
  point of a policy-driven gate, and it is also why this number is not a product claim.
- **A comparison to the R-Judge leaderboard.** Those are LLM judges answering a different
  question (classify a whole finished interaction). This is a deterministic pre-execution gate
  answering whether it would have stopped the run. Same labels, different task, non-comparable
  metrics.
- **Any statement about semantic safety.** The 40 misses are mostly records where every tool
  called was read-only by name and the harm lay in *what* was read and *where it went*. A
  name-based deny-list cannot see that, and no amount of verb-list tuning would fix it — it is
  a category limit of the gate, not a bug in it.
- **Anything about conversational harm.** 76 records (13.3%) contain no tool call at all. The
  governor is structurally blind to them. They are counted as misses; they are not a
  measurement of anything the governor does.
- **A generalization beyond this corpus.** One corpus, one revision, one policy, one run.

## Consequences

- New public surface: `plimsoll/corpus.py` and `plimsoll corpus-score`. Additive; no existing
  type, rule, report, or exit code changed.
- Zero new runtime dependencies. The `plimsoll` package still opens no sockets — the fetcher is
  a `scripts/` dev tool, like `examples/access-request/build_real_otel_trace.py`.
- The published number is now load-bearing: `tests/test_corpus_score.py` recomputes every cell
  and rate from the committed ledger and cross-checks the README headline, so a hand-edited
  number or a stale scorecard fails the build.
- **A defect this measurement exposed in shipped code.** `plimsoll governor --policy` and
  `plimsoll-governor --policy` both documented their default as "a permissive empty policy".
  It is not permissive: `Policy.max_repeated_action_count` defaults to `1` (correctly
  documented in `SCHEMA.md`), so with no policy file the second identical tool call is blocked
  by `repeated_action`. Three corpus records are blocked by exactly that and nothing else —
  which is why the "no policy" baseline is 98.8% rather than a clean 100%. Nothing in the
  existing 266-test suite noticed, because no fixture in it repeated an identical call. The
  fail-closed default is deliberate and stays; the two help strings were corrected and a
  regression test pins the real behaviour.

## Alternatives considered

- **Vendor a subset of the corpus for offline reproducibility.** Rejected: no upstream licence.
  Mitigated by committing the ledger plus a synthetic, clearly-labelled fixture corpus that
  proves the *pipeline* offline and is explicitly not evidence for the *number*.
- **Hand-author the deny-list.** Rejected: unfalsifiable claim of label-blindness. The derived
  policy can be regenerated by anyone and proven label-blind by test.
- **Report a single accuracy figure.** Rejected: it hides which direction the gate fails in,
  and the two directions have completely different costs to an operator.
- **Report only `miss_rate`.** Rejected: it excludes the 50 unsafe records the gate could not
  see and would have made the headline look 14 points better for no work.
