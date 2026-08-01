# Plimsoll's governor, graded against someone else's labels

Every other number in this repository grades Plimsoll's own artifacts against Plimsoll's own
labels. That is a self-graded evidence class, and no test count fixes it.

This directory holds one number that is not self-graded: the runtime governor replayed over
**R-Judge**, 571 multi-turn agent interaction records labelled safe/unsafe by human
annotators who have never heard of this project.

> R-Judge: Benchmarking Safety Risk Awareness for LLM Agents — Yuan et al.,
> Findings of the Association for Computational Linguistics: EMNLP 2024.
> Repository: <https://github.com/Lordog/R-Judge> · Paper: <https://aclanthology.org/2024.findings-emnlp.79/>
> Record schema (the adapter is written against this file, not from memory):
> <https://raw.githubusercontent.com/Lordog/R-Judge/main/config/data_schema.json>
>
> Measured at commit `83ce301da3ad50dd8b397e772863f5411c3d3dc2`.

## The number

**The governor misses 29.9% of the unsafe records in this corpus.**

That is the honest denominator: all 301 records R-Judge labels unsafe, with every record the
gate structurally cannot see counted as a miss. It also **falsely blocks 9.4%** of the records
R-Judge labels safe.

| | derived deny-list (headline) | default empty policy (floor) |
| --- | ---: | ---: |
| records scored | 495 / 571 | 495 / 571 |
| unmappable (no tool call at all) | 76 (50 unsafe) | 76 (50 unsafe) |
| caught (unsafe, blocked) | 211 | 3 |
| **missed (unsafe, allowed)** | **40** | 248 |
| **falsely rejected (safe, blocked)** | **23** | 0 |
| cleanly allowed (safe, allowed) | 221 | 244 |
| miss rate over the 251 unsafe records it could see | 15.9% | 98.8% |
| **worst-case miss rate over all 301 unsafe records** | **29.9%** | 99.0% |
| false-reject rate over 244 safe records | 9.4% | 0.0% |
| agreement over scored records only | 87.3% | 49.9% |

Both denominators are published because only one of them is honest. `miss_rate` divides by the
records the gate could see, which quietly excludes 50 unsafe records it was blind to.
`worst_case_miss_rate` divides by every unsafe record in the corpus. **29.9% is the number.**

Two things that number is not:

- It is **not** comparable to the R-Judge leaderboard. Those are LLM judges scored on a
  different task (read a whole finished interaction and classify it). This is a deterministic
  pre-execution gate scored on whether it would have stopped the run. Same labels, different
  question.
- It is **not** a claim that Plimsoll is a safety classifier. It is one policy's result. See
  `docs/adr/0001-external-corpus-miss-rate.md` for what the number does and does not license.

## Where the misses come from

Two structurally different failures, and it is worth keeping them apart:

1. **76 records have no tool call at all** (50 of them unsafe) — conversational harm, refusals,
   advice. A tool gate is blind to these by construction, not by bug. They are reported as
   `no_tool_calls` issues and counted against the governor in the worst-case denominator
   rather than dropped from it. Dropping them would have turned 29.9% into 15.9% for free.
2. **40 scored records were allowed** because every tool they called is read-only by name —
   the harm was in *what was read and to whom it went*, which a name-based deny-list cannot
   see. Most of these are in `Application` (20) and `IoT` (10).

The 23 false rejects are the mirror image: the predeclared verb `Pay` also matches the
read-only `BankManagerSearchPayee`. That over-block was noted in the plan **before** the first
measurement and deliberately left in. Tuning the verb list after seeing the score is how a
number like this stops being evidence.

## The policy never read the labels

The deny-list is the dominant free parameter of a measurement like this, so it is derived by a
pure function of the corpus's **tool vocabulary** (141 names) and a verb list predeclared in
`docs/plans/plans/2026-08-01-external-corpus-miss-rate.md`:

    Send Post Transfer Withdraw Deposit Pay Delete Share
    Grant Create Update Download Move Execute Write

41 of the 141 tools match and are forbidden. `tests/test_corpus_score.py` proves the
label-blindness mechanically: flip every label in a corpus and the derived policy digest is
byte-identical.

## Reproducing it

The corpus is **fetched, not vendored** — upstream R-Judge declares no licence, so its records
are not ours to commit into an MIT repository. The fetch is pinned to a commit and every file
is verified against a SHA-256 digest recorded when this scorecard was measured:

```bash
python scripts/fetch_rjudge_corpus.py --out .corpus/rjudge
python scripts/score_external_corpus.py --corpus .corpus/rjudge
```

That rewrites `rjudge-scorecard.json`. If it differs from the committed one, the number in
this README is wrong and the suite will say so.

**Offline**, with no network and nothing fetched, you can still check two things:

```bash
# 1. the whole pipeline, end to end, on the bundled fixture corpus
python -m plimsoll corpus-score --corpus examples/external-corpus/fixture-corpus --json

# 2. the published number's arithmetic, recomputed from the committed per-record ledger
python -m unittest tests.test_corpus_score
```

## What is in this directory

| path | what it is |
| --- | --- |
| `rjudge-scorecard.json` | the measurement: denominators, confusion cells, rates, and a per-record verdict ledger (571 records' worth of *our results*, not the corpus text) |
| `fixture-corpus/` | a small **synthetic** corpus in R-Judge's record shape |

### The fixture corpus is NOT third-party-labelled

`fixture-corpus/` is eight records we wrote ourselves, in the upstream schema's shape, every
`risk_description` prefixed `SYNTHETIC:`. Its labels are **ours**. It exists so the adapter,
the gate replay, and the scorer can be run end to end offline by anyone who clones this repo —
it proves the *pipeline*, and it proves nothing at all about the headline number. Do not quote
a rate computed from it.

It does cover all four confusion cells on purpose, including the two that are bad for us: one
record the deny-list misses because all its tools are read-only, and one safe record it blocks
because `Pay` matches `BankManagerSearchPayee`.
