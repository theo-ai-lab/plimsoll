"""Pins the version everywhere it is declared or embedded, so a release bump cannot skew.

The version lives in two source locations (``plimsoll/__init__.py`` is the runtime source of
truth; ``pyproject.toml`` repeats it for packaging) and is embedded in the committed SARIF
report fixtures by the tool that generated them. Nothing ties these together at runtime, so
a bump that misses one would ship silently inconsistent metadata — these tests turn that
drift into a suite failure. After bumping, regenerate the fixtures with
``scripts/build_golden.py`` and ``scripts/build_access_request_demo.py``.
"""

import json
import tomllib
import unittest
from pathlib import Path

from plimsoll import __version__

ROOT = Path(__file__).resolve().parent.parent

# Committed report fixtures that embed the version of the tool that generated them.
VERSIONED_SARIF_FIXTURES = [
    ROOT / "examples" / "output" / "golden" / "report.sarif.json",
    ROOT / "examples" / "access-request" / "reports" / "failed-report.sarif.json",
]


class VersionSyncTests(unittest.TestCase):
    def test_pyproject_version_matches_package_version(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        self.assertEqual(pyproject["project"]["version"], __version__)

    def test_committed_sarif_fixtures_embed_the_current_version(self) -> None:
        for path in VERSIONED_SARIF_FIXTURES:
            with self.subTest(fixture=str(path.relative_to(ROOT))):
                driver = json.loads(path.read_text(encoding="utf-8"))["runs"][0]["tool"]["driver"]
                self.assertEqual(driver["version"], __version__)


if __name__ == "__main__":
    unittest.main()
