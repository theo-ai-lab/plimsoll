#!/usr/bin/env bash
# Rehearse the release locally, without publishing anything.
#
#   ./scripts/rehearse_release.sh v1.0.0
#
# Publishing is irreversible: a version number on PyPI cannot be replaced, and yanking
# does not free it. `release.yml` runs these same steps AFTER a tag push, which is the
# wrong time to discover that the build breaks or the wheel is wrong — deleting and
# re-pushing a tag is possible but every cycle is a chance to push a wrong one.
#
# This runs everything the workflow runs EXCEPT the publish step:
#   1. the release guard (tag vs packaged version vs changelog)
#   2. python -m build
#   3. install the wheel into a clean venv, off the checkout's sys.path
#   4. assert the installed CLI reports the tagged version
#   5. run one real gate end to end from the wheel against committed fixtures
#
# Nothing here touches the network except pip installing `build` into a scratch venv,
# and nothing here can publish.
set -euo pipefail

TAG="${1:-}"
if [ -z "$TAG" ]; then
  echo "usage: ./scripts/rehearse_release.sh <tag>   (e.g. v1.0.0)" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$ROOT"

echo "── 1. release guard ─────────────────────────────────────────────"
python3 -m scripts.release_guard "$TAG"

echo "── 2. build sdist + wheel ───────────────────────────────────────"
python3 -m venv "$WORK/buildenv"
"$WORK/buildenv/bin/pip" install --quiet --upgrade build
"$WORK/buildenv/bin/python" -m build --outdir "$WORK/dist" >/dev/null
ls -1 "$WORK/dist"

echo "── 3. install the wheel into a CLEAN venv ───────────────────────"
python3 -m venv "$WORK/smoke"
"$WORK/smoke/bin/pip" install --quiet "$WORK"/dist/*.whl

echo "── 4. the installed CLI must report the tagged version ──────────"
reported="$("$WORK/smoke/bin/plimsoll" --version)"
expected="plimsoll ${TAG#v}"
if [ "$reported" != "$expected" ]; then
  echo "REFUSED — installed wheel reports '$reported', expected '$expected'" >&2
  exit 1
fi
echo "$reported"

echo "── 5. one real gate, run FROM the wheel ─────────────────────────"
GITHUB_STEP_SUMMARY="$WORK/summary.md" "$WORK/smoke/bin/plimsoll" run \
  --input examples/traces/current_ticket_triage.json \
  --baseline examples/traces/baseline_ticket_triage.json \
  --policy examples/policies/default_policy.json \
  --out "$WORK/release-smoke" --quiet

echo
echo "REHEARSED — every step of release.yml except the publish passed for $TAG."
echo "Remaining, and yours to do: configure the PyPI Trusted Publisher for this repo +"
echo "workflow + the 'pypi' environment, then push the tag. Nothing above published."
