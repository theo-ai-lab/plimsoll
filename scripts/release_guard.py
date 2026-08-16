"""Refuse a release that would burn a PyPI version number.

`release.yml` fires only on a version tag push, so its guards were unexecuted code
protecting the one irreversible action in this repo: a published version cannot be
replaced, and yanking does not free the number. The comparison lives here instead of
inline in the workflow so it can be run — and tested — without pushing a tag.

Every reason is collected rather than short-circuiting on the first. Retrying a tag
push means deleting and re-pushing the tag, and each cycle is another chance to push a
wrong one; an operator should learn everything that is wrong in one pass.

    python -m scripts.release_guard v1.0.0     # or: GITHUB_REF_NAME=v1.0.0 python -m scripts.release_guard
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

PLACEHOLDER = "Prepared, not yet released"
_ROOT = Path(__file__).resolve().parent.parent


def packaged_version(root: Path = _ROOT) -> str:
    """The version the wheel will carry."""
    with (root / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def read_changelog(root: Path = _ROOT) -> str:
    return (root / "CHANGELOG.md").read_text(encoding="utf-8")


def blocking_reasons(*, tag: str, version: str, changelog: str) -> list[str]:
    """Every reason this must not publish. Empty means clear to publish."""
    stripped = tag[1:] if tag.startswith("v") else tag
    reasons: list[str] = []

    if stripped != version:
        reasons.append(
            f"tag v{stripped} does not match the packaged version {version} — "
            f"move the tag or bump pyproject.toml, but do not publish"
        )
    if f"## [{version}]" not in changelog:
        reasons.append(f"CHANGELOG.md has no '## [{version}]' entry — a release with no notes is not a release")
    if PLACEHOLDER in changelog:
        reasons.append(f"CHANGELOG.md still carries the pre-tag placeholder ({PLACEHOLDER!r}) — cut the entry first")
    return reasons


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        # ::error:: annotations must go to stdout — the runner does not parse stderr.
        print("::error::no tag given (argv[1] or GITHUB_REF_NAME) — refusing to publish")
        return 2

    version = packaged_version()
    reasons = blocking_reasons(tag=tag, version=version, changelog=read_changelog())
    for reason in reasons:
        print(f"::error::{reason}")
    if reasons:
        return 1

    print(f"tag {tag} matches pyproject.toml version {version} and the changelog is cut")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
