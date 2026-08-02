# Plan — external corpus adapter + honest miss rate

Date: 2026-08-01
Status: executed (see `docs/adr/0001-external-corpus-miss-rate.md` for the result)

## Goal

Every number Plimsoll publishes today grades Plimsoll's own artifacts against Plimsoll's
own labels. This slice changes the evidence class for exactly one number: it runs the
runtime governor over an **external, third-party-labelled corpus** and publishes the
governor's **miss rate against someone else's labels** — flattering or not.

Corpus: **R-Judge** (Yuan et al., *R-Judge: Benchmarking Safety Risk Awareness for LLM
Agents*, Findings of EMNLP 2024). 571 human-labelled multi-turn agent interaction records,
each with a binary safety label (`0` safe / `1` unsafe) annotated by the R-Judge authors,
not by us.

- Repository: <https://github.com/Lordog/R-Judge>
- **Official record schema (fetched, not remembered):**
  <https://github.com/Lordog/R-Judge/blob/main/config/data_schema.json>
  raw: <https://raw.githubusercontent.com/Lordog/R-Judge/main/config/data_schema.json>
- Paper: <https://aclanthology.org/2024.findings-emnlp.79/>

## Architecture

One new module, `plimsoll/corpus.py`, with two seams and no new runtime dependency.

### Seam 1 — adapter boundary (untrusted third-party data)

    external corpus record (R-Judge JSON)  ->  AdaptationReport

    adapt_rjudge_records(records, source) -> AdaptationReport
      AdaptationReport.adapted : list[AdaptedRecord]   # record_id, source, label, TraceRun
      AdaptationReport.issues  : list[CorpusIssue]     # ONE error shape, machine code + detail
      AdaptationReport.total_records : int

Contract, stated up front:

- **Total accounting.** `len(adapted) + len(issues) == total_records`, always. A record that
  cannot be mapped is *reported*, never dropped. A silently dropped record would shrink the
  denominator and inflate the score, which is the exact failure this repo exists to catch.
- **One error shape.** `CorpusIssue(code, record_index, record_id, label, detail)`. `code` is
  a stable machine string; `label` is carried when the corpus stated one, so an unmappable
  *unsafe* record can still be counted against us in the worst-case denominator.
- **Never raises on record content.** Malformed, hostile, wrong-typed, or extra-fielded
  records become issues. File-level problems (missing path, unreadable file, invalid JSON)
  stay `ValidationError`, matching the repo's existing usage-error contract.
- **Additive.** No existing type, rule, or CLI behaviour changes.

Action dialects: R-Judge's `action` field is a free string. Observed dialects in the
corpus (all parsed, each pinned by a test):

1. `ToolName{...}`                      (414 occurrences)
2. `ToolName\nAction Input: {...}`      (342)
3. `ToolName: {...}`                    (222)
4. `ToolName Input: {...}`              (1)
5. ` ```bash ` fenced block             (1)

A candidate is only accepted as a tool call when the payload after the separator opens with
`{`. That guard is what stops prose such as `"Woman: I love you."` from being scored as a
call to a tool named `Woman`.

### Seam 2 — scoring path

    score_corpus(report, governor) -> CorpusScore

Per record: replay its tool calls in order through `Governor.evaluate` (the *offline*
evaluator — correct here, because the history comes from a recorded trace, not from a live
agent describing its own past). Record verdict is `block` if any step was blocked, else
`allow`. Compare to the corpus label:

| corpus label | governor verdict | cell |
| --- | --- | --- |
| 1 unsafe | block | caught |
| 1 unsafe | allow | **false accept (miss)** |
| 0 safe | block | **false reject** |
| 0 safe | allow | clean allow |

Two denominators are published, both stated explicitly:

- `miss_rate` = misses / unsafe **scored** records.
- `worst_case_miss_rate` = (misses + unsafe **unmapped** records) / all unsafe records in the
  corpus. Unmapped records count as misses because a tool gate cannot see them at all.

### Label-blindness of the policy (the honesty guard)

The policy is the dominant free parameter of any number like this. To keep it from being
fitted to the labels, the scored policy is **derived by a pure function of the corpus tool
vocabulary and a predeclared verb list** — it never reads a label:

    derive_side_effect_policy(tools, verbs=SIDE_EFFECT_VERBS) -> Policy

`SIDE_EFFECT_VERBS`, predeclared here before the first measurement and frozen:

    Send, Post, Transfer, Withdraw, Deposit, Pay, Delete, Share,
    Grant, Create, Update, Download, Move, Execute, Write

Matching is case-insensitive substring on the tool name. Known consequence, accepted in
advance and **not** tuned away: `Pay` also matches the read-only `BankManagerSearchPayee`,
so the policy over-blocks; that shows up as false rejects and is published as such.

A test asserts label-blindness mechanically: flipping every label in a corpus must not
change the derived policy.

Baselines published alongside it:

- **empty policy** — blocks nothing, so its miss rate is 100% by construction. It is
  published to make the point that the gate is policy-bound, not magic.
- **derived side-effect policy** — the headline number.

## TDD task list

Each task: write the test, run it, watch it fail for the right reason, then the minimum code.

1. `adapt_rjudge_records` maps one well-formed record to a `TraceRun` with the right tool
   sequence, label, and record id.
2. Every observed action dialect parses to the right tool name; prose does not.
3. Total accounting invariant: adapted + issues == total, for a mixed corpus.
4. A record with no tool call becomes a `no_tool_calls` issue that still carries its label.
5. Malformed records (not an object, bad label, bad contents) each become the right issue
   code; nothing raises.
6. **Property (seeded generator over hostile records):** for any generated corpus of
   mutated/hostile records, `adapt_rjudge_records` never raises, the accounting invariant
   holds, every issue carries a code, and every adapted trace has >= 1 tool call.
7. `score_corpus` computes caught / miss / false-reject / clean-allow with the stated
   denominators, including the unmapped-unsafe worst case.
8. `derive_side_effect_policy` is label-blind (flip all labels -> identical policy digest).
9. Empty policy scores a 100% miss rate on any corpus with >= 1 unsafe scored record.
10. CLI `plimsoll corpus-score` runs the whole path over the bundled fixture corpus and
    emits the scorecard as JSON.
11. The committed real-corpus scorecard is internally consistent (cells sum to the
    denominators, rates match the cells) — so the published number cannot silently rot.

## Deliverables

- `plimsoll/corpus.py`, `tests/test_corpus_adapter.py`, `tests/test_corpus_score.py`
- `examples/external-corpus/` — bundled fixture corpus (**synthetic, ours**, R-Judge-shaped),
  derived policy, and the committed real-corpus scorecard + per-record ledger
- `scripts/fetch_rjudge_corpus.py` — pinned commit + SHA-256 manifest, so the real corpus
  is fetched reproducibly rather than vendored
- `docs/adr/0001-external-corpus-miss-rate.md` — methodology and, more importantly, its limits

## Scope cuts, declared

- **The real corpus is not vendored.** Upstream R-Judge declares no licence, so committing
  its records into an MIT repo is not ours to do. Offline reproducibility is therefore split:
  the bundled *synthetic* fixture corpus reproduces the whole pipeline offline, and the
  committed scorecard + per-record ledger let anyone audit the published number offline; the
  raw corpus itself takes one pinned, digest-verified fetch.
- The bundled fixture corpus is **not** third-party-labelled. Its labels are ours. It proves
  the adapter and the scorer, never the headline number.
- Only the gate-decidable rule subset participates. Result-dependent rules (leakage, output
  match, trajectory drift) are not evaluated, exactly as at the live gate.
