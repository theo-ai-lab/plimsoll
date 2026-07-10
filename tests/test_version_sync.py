"""Pins the version everywhere it is declared or embedded, so a release bump cannot skew.

The version lives in two source locations (``plimsoll/__init__.py`` is the runtime source of
truth; ``pyproject.toml`` repeats it for packaging) and is embedded in committed fixtures:
the SARIF reports carry the generating tool's version, and the MCP session transcript's
``initialize`` request carries it as ``clientInfo.version``. Nothing ties these together at
runtime, so a bump that misses one would ship silently inconsistent metadata — these tests
turn that drift into a suite failure. After bumping, regenerate the fixtures with
``scripts/build_golden.py``, ``scripts/build_access_request_demo.py``, and
``scripts/build_mcp_governor_session.py``.
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

MCP_TRANSCRIPT = ROOT / "examples" / "mcp-governor-session" / "transcript.jsonl"


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

    def test_committed_mcp_transcript_embeds_the_current_client_version(self) -> None:
        # build_mcp_governor_session.py stamps plimsoll.__version__ into the initialize
        # request's clientInfo; the replay tests compare only decisions, so without this
        # check a version bump could leave a stale version embedded in the transcript.
        records = [json.loads(line) for line in MCP_TRANSCRIPT.read_text(encoding="utf-8").splitlines() if line]
        initialize = next(record["message"] for record in records if record["message"].get("method") == "initialize")
        self.assertEqual(initialize["params"]["clientInfo"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
