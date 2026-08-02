"""The release guard, tested — because in `release.yml` it can never be.

`release.yml` fires only on a version tag push. Every guard inside it is therefore
unexecuted code, and it is guarding the one action in this repo that cannot be undone:
a bad upload burns that version number on PyPI permanently. Yanking does not free it.

`git tag` is empty and the workflow has never run once, so before this file the honest
statement about those guards was "they have never been observed to do anything".

The comparison logic now lives in `scripts/release_guard.py` and the workflow calls it,
so the refusals below are the same code path that will run on the tag push.
"""

from __future__ import annotations

import unittest

from scripts.release_guard import blocking_reasons, packaged_version, read_changelog

CUT_CHANGELOG = "# Changelog\n\n## [1.0.0] - 2026-07-31\n\n- Initial release.\n"


class ReleaseGuardTests(unittest.TestCase):
    def test_a_matching_tag_with_a_cut_changelog_is_clear_to_publish(self) -> None:
        self.assertEqual(blocking_reasons(tag="v1.0.0", version="1.0.0", changelog=CUT_CHANGELOG), [])

    def test_a_tag_that_does_not_match_the_packaged_version_is_refused(self) -> None:
        reasons = blocking_reasons(tag="v9.9.9", version="1.0.0", changelog=CUT_CHANGELOG)
        self.assertEqual(len(reasons), 1)
        # The operator must learn WHICH two things disagree, or they cannot tell whether to
        # move the tag or bump pyproject.
        self.assertIn("9.9.9", reasons[0])
        self.assertIn("1.0.0", reasons[0])

    def test_a_changelog_with_no_entry_for_this_version_is_refused(self) -> None:
        reasons = blocking_reasons(tag="v1.0.0", version="1.0.0", changelog="# Changelog\n\n## [0.9.0]\n")
        self.assertTrue(any("CHANGELOG" in reason for reason in reasons))

    def test_the_pre_tag_placeholder_still_in_the_changelog_is_refused(self) -> None:
        stale = CUT_CHANGELOG + "\nPrepared, not yet released\n"
        reasons = blocking_reasons(tag="v1.0.0", version="1.0.0", changelog=stale)
        self.assertTrue(any("placeholder" in reason.lower() for reason in reasons))

    def test_a_prerelease_tag_must_match_a_prerelease_version_exactly(self) -> None:
        # The tag pattern in release.yml admits v1.2.3rc1, so this case reaches the guard.
        # Publishing an rc tag from a final-version pyproject would put 1.0.0 on PyPI under
        # an rc tag — the version is then burned and the rc never exists.
        self.assertNotEqual(blocking_reasons(tag="v1.0.0rc1", version="1.0.0", changelog=CUT_CHANGELOG), [])
        self.assertEqual(
            blocking_reasons(
                tag="v1.0.0rc1",
                version="1.0.0rc1",
                changelog="# Changelog\n\n## [1.0.0rc1] - 2026-07-31\n",
            ),
            [],
        )

    def test_every_failure_is_reported_at_once_not_one_per_run(self) -> None:
        """A tag push is expensive to retry: it must be deleted and re-pushed.

        Reporting the first problem only would make fixing three of them take three tag
        cycles, and each cycle is a chance to push a wrong one.
        """
        reasons = blocking_reasons(
            tag="v2.0.0",
            version="1.0.0",
            changelog="# Changelog\n\n## [0.1.0]\n\nPrepared, not yet released\n",
        )
        self.assertGreaterEqual(len(reasons), 2)

    def test_a_leading_v_is_optional_in_the_comparison(self) -> None:
        for tag in ("1.0.0", "v1.0.0"):
            with self.subTest(tag=tag):
                self.assertEqual(blocking_reasons(tag=tag, version="1.0.0", changelog=CUT_CHANGELOG), [])

    def test_the_repo_as_it_stands_is_clear_to_tag(self) -> None:
        """The live check: this is what a v1.0.0 push would evaluate right now."""
        self.assertEqual(blocking_reasons(tag="v1.0.0", version=packaged_version(), changelog=read_changelog()), [])


if __name__ == "__main__":
    unittest.main()
