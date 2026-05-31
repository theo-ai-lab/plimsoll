import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plimsoll.cli import main


class PolicyInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plimsoll-policy-init-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_policy_from_native_trace(self) -> None:
        out = self.tmp / "policy.json"
        code = main(
            [
                "init-policy",
                "--input",
                "examples/traces/current_ticket_triage.json",
                "--format",
                "native",
                "--out",
                str(out),
            ]
        )

        self.assertEqual(code, 0)
        policy = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(policy["allowed_tools"], ["read_ticket", "search_docs", "summarize"])
        self.assertEqual(policy["required_tools"], ["read_ticket", "search_docs", "summarize"])
        self.assertGreaterEqual(policy["max_steps"], 4)
        self.assertEqual(policy["max_tool_sequence_distance"], 1)


if __name__ == "__main__":
    unittest.main()
