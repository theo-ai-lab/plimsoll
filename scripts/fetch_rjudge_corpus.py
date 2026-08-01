#!/usr/bin/env python3
"""Fetch the R-Judge corpus at a pinned commit and verify it byte-for-byte.

DEV TOOLING, NOT PART OF THE TOOL. The ``plimsoll`` package itself never opens a socket —
that contract is unchanged. This script exists because the corpus is *not vendored*: upstream
R-Judge declares no licence, so its records are not ours to commit into an MIT repository.
What is committed instead is the measurement (``examples/external-corpus/rjudge-scorecard.json``,
including a per-record verdict ledger), so the published number stays auditable offline.

Corpus
------
R-Judge: Benchmarking Safety Risk Awareness for LLM Agents (Yuan et al., Findings of EMNLP 2024)
  repository ....... https://github.com/Lordog/R-Judge
  record schema .... https://raw.githubusercontent.com/Lordog/R-Judge/main/config/data_schema.json
  paper ............ https://aclanthology.org/2024.findings-emnlp.79/

Every file is pinned to ``plimsoll.corpus.RJUDGE_PINNED_COMMIT`` and checked against the
SHA-256 digests below, which were recorded when the published scorecard was measured. If a
digest does not match, the fetch FAILS rather than scoring different bytes under the same
headline number.

Usage:
    python scripts/fetch_rjudge_corpus.py [--out .corpus/rjudge]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plimsoll.corpus import RJUDGE_PINNED_COMMIT  # noqa: E402

RAW_BASE = "https://raw.githubusercontent.com/Lordog/R-Judge"

# path under data/ -> SHA-256 of the file at RJUDGE_PINNED_COMMIT.
MANIFEST = {
    "Application/chatbot.json": "4e2f4a75271eec9bae6515d05b270053d5bca384cf7ea1190f95eda3cea5488b",
    "Application/dh_app.json": "b3de98b42367332533cf3bf828bb0bc87de7bc8f5fd0fbf629cd2ff48b216e0f",
    "Application/ds_app.json": "209c80e99e5b1ebb24ff8fedc2aea9872ad5012db96019b1ebf6882157436146",
    "Application/mail.json": "75cb8bad0c29fc18411cf5e1f6fac425ee42d9c8090d496d62ec4819b8140b51",
    "Application/medical.json": "e7ed1581d5a815ea7af8de26b6c1732b63c519eda6cae3dcc3cd9a8a581ec262",
    "Application/phone.json": "a63e413cfad181b17a1ecdc00db7778e14ede7ca924fc164596c09c24b595d02",
    "Application/productivity.json": "599107f85bd255d355a0d057cd39a9c3cb96c49397ae344754ada1296bea5309",
    "Application/socialapp.json": "5cd419ea9f7409dd5cbcd117443f99fee7a1e8124955b7ab7d941914f293b892",
    "Finance/bitcoin.json": "faddca9b2561a05e5e8c58f4500f644d9a2d5e3e44963282093f985274b5941b",
    "Finance/dh_finance.json": "0f1cb43a2f1f63440834049bbf78c9909b40961735adc69c267033e0ae240b56",
    "Finance/ds_finance.json": "0f3309c6e9759bf9ae227257ec3923c56c2768ebd2235ec98c8100ffa005519b",
    "Finance/moneymanagement.json": "d56134e91b11feacbd449af0dd90dfc4306d848636a241ffc53d723766a6eb3c",
    "Finance/webshop.json": "20463337cfc228ac0cd4a5e9fda8b0989d46c9062a21f136ff78a3169fd612c8",
    "IoT/household.json": "1b57f59dde6dc6daec13bbf6191a400e6e53d50f60693672e0d49d4c948fa264",
    "IoT/phone_iot.json": "c38321800dc6c73e64121ed07df14da3f321efe300e68a7c006db9b161a4b676",
    "IoT/trafficdispatch.json": "1cc43a1efa73f127988e5a8151081a525a1470f2d320efa154544ffeea8d7be4",
    "Program/code_agentmonitor.json": "f41b81d3f49cb09d8f62205ee4b2dbafb5bec9b44dbd112cde842fd0e2165e94",
    "Program/dh_program.json": "222b228525ce05a972dfabc7d1548b59bb2e88a615f71d3c37afabd3bc4a1036",
    "Program/ds_program.json": "5275de6d4f1ff5d01559ff80eb646e30daec80307aa4d1fbdc87f76581fb4f85",
    "Program/phone_program.json": "da5ba9b0d5d7cd0e2842dec78d2d582574fc7b61469f3486ad7bc45128b519e3",
    "Program/security.json": "2fb039674fe877e6666cd6f3745b841e84e07fe5f2ea62504d65ef2caa73b516",
    "Program/software.json": "771ccf2594f6310ef1628c345249ff550b123809016baf1c6eed55b816ccd015",
    "Program/terminal.json": "9f2175ae64693b7f72e23ccad2736e804dfc6aa36e8c53b99034607590c868ea",
    "Web/dh_web.json": "6d338be814845e9829370f28a60cad1e4a27f2904183b18e125e49dddace15db",
    "Web/ds_web.json": "457413fa9612c1a3f6c25f013e70036041480fa6ae551964dd2e2d9da4724a6a",
    "Web/webbrowser.json": "2d687e63346cf5cfd91e3cfc11d697c6f43895f787072c019b3cf8046ca6bc66",
    "Web/websearch.json": "9f98c31a183e5d61b9eced669f02a0dede461a69b5c52ad8a0e6d9b7a24a499d",
}


def fetch(out: Path) -> int:
    failures = 0
    for relative, digest in sorted(MANIFEST.items()):
        url = f"{RAW_BASE}/{RJUDGE_PINNED_COMMIT}/data/{relative}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned https URL
                payload = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"FAIL  {relative}: {exc}", file=sys.stderr)
            failures += 1
            continue
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest:
            print(f"FAIL  {relative}: digest {actual} != pinned {digest}", file=sys.stderr)
            failures += 1
            continue
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        print(f"ok    {relative}")
    if failures:
        print(f"\n{failures} file(s) failed; the corpus is NOT the pinned revision.", file=sys.stderr)
        return 1
    print(f"\n{len(MANIFEST)} file(s) verified at {RJUDGE_PINNED_COMMIT[:12]} -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path(".corpus/rjudge"), help="download directory")
    args = parser.parse_args(argv)
    return fetch(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
