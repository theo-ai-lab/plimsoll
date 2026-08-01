#!/usr/bin/env python3
"""Score the runtime governor against an EXTERNAL, third-party-labelled corpus.

Regenerates ``examples/external-corpus/rjudge-scorecard.json`` — the committed, auditable
record of how often Plimsoll's gate disagrees with someone else's safety labels.

Two runs are always published side by side:

  * ``default-empty-policy`` — what a user gets with no ``--policy`` at all. It is the floor
    the headline is read against, and it makes the point that the gate is policy-bound, not
    magic. It is NOT a zero: ``Policy.max_repeated_action_count`` defaults to 1, so it still
    blocks a repeated identical call. Scoring this corpus is what surfaced that; the run is
    published with whatever it actually caught rather than an assumed zero.
  * ``side-effect-deny-list`` — the headline. The policy is derived by
    ``plimsoll.corpus.derive_side_effect_policy`` from the corpus TOOL VOCABULARY and the
    verb list predeclared in docs/plans/plans/2026-08-01-external-corpus-miss-rate.md.
    It never reads a label.

Get the corpus first (pinned + digest-verified):

    python scripts/fetch_rjudge_corpus.py --out .corpus/rjudge
    python scripts/score_external_corpus.py --corpus .corpus/rjudge
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plimsoll.corpus import (  # noqa: E402
    RJUDGE_PAPER_URL,
    RJUDGE_PINNED_COMMIT,
    RJUDGE_REPO_URL,
    RJUDGE_SCHEMA_URL,
    SIDE_EFFECT_VERBS,
    derive_side_effect_policy,
    load_rjudge_corpus,
    score_corpus,
)
from plimsoll.governor import Governor  # noqa: E402
from plimsoll.models import Policy, ValidationError  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "examples" / "external-corpus" / "rjudge-scorecard.json"


def build_scorecard(corpus_dir: Path) -> dict:
    report = load_rjudge_corpus(corpus_dir)
    vocabulary = report.tool_vocabulary()
    derived = derive_side_effect_policy(vocabulary)
    runs = {
        "default-empty-policy": score_corpus(report, Governor(Policy())),
        "side-effect-deny-list": score_corpus(report, Governor(derived)),
    }
    return {
        "corpus": {
            "name": "R-Judge",
            "citation": "Yuan et al., R-Judge: Benchmarking Safety Risk Awareness for LLM Agents, "
            "Findings of EMNLP 2024",
            "repository": RJUDGE_REPO_URL,
            "schema_url": RJUDGE_SCHEMA_URL,
            "paper": RJUDGE_PAPER_URL,
            "commit": RJUDGE_PINNED_COMMIT,
            "labels": "0 = safe, 1 = unsafe; annotated by the R-Judge authors, not by this project",
        },
        "adaptation": report.to_dict(),
        "policy": {
            "derivation": "plimsoll.corpus.derive_side_effect_policy over the corpus tool vocabulary",
            "label_blind": True,
            "verbs": list(SIDE_EFFECT_VERBS),
            "forbidden_tools": sorted(derived.forbidden_tools),
            "tool_vocabulary_size": len(vocabulary),
        },
        "runs": {name: score.to_dict() for name, score in runs.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True, help="directory of fetched R-Judge JSON files")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="scorecard output path")
    args = parser.parse_args(argv)

    try:
        scorecard = build_scorecard(args.corpus)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(scorecard, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    for name, run in scorecard["runs"].items():
        rates, confusion, denominators = run["rates"], run["confusion"], run["denominators"]
        print(f"[{name}] policy_digest={run['policy_digest'][:12]}")
        print(
            f"  scored {denominators['scored_records']}/{denominators['total_records']} records "
            f"({denominators['unmapped_records']} unmappable, of which {denominators['unmapped_unsafe']} unsafe)"
        )
        print(
            f"  caught {confusion['caught']}  missed {confusion['missed']}  "
            f"false-rejects {confusion['false_reject']}  clean-allows {confusion['clean_allow']}"
        )
        print(
            f"  miss rate {rates['miss_rate']:.3f} over {denominators['unsafe_scored']} scored unsafe; "
            f"WORST-CASE miss rate {rates['worst_case_miss_rate']:.3f} over "
            f"{denominators['unsafe_total']} unsafe records in the corpus"
        )
        print(f"  false-reject rate {rates['false_reject_rate']:.3f} over {denominators['safe_scored']} safe records")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
