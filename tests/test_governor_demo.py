"""Locks the live governor-loop demo: its 'blocked N of M unsafe calls' headline is real.

Loads examples/governor_loop_demo.py and drives the scripted loop through the gate, so the
demo can never silently start letting an unsafe call through without this test failing.
"""

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

DEMO_PATH = Path(__file__).resolve().parent.parent / "examples" / "governor_loop_demo.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("governor_loop_demo", DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolve their owning module via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GovernorLoopDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.demo = _load_demo()

    def test_every_unsafe_call_is_blocked_and_no_safe_call_is(self) -> None:
        result = self.demo.run_loop()
        self.assertTrue(result.consistent, f"leaked={result.leaked} over_blocked={result.over_blocked}")
        self.assertEqual(result.blocked_unsafe, result.unsafe_total)
        self.assertEqual(result.allowed_safe, result.safe_total)
        self.assertEqual(result.leaked, [])
        self.assertEqual(result.over_blocked, [])
        # The scripted stream is meant to contain several unsafe calls — guard against a
        # silently-empty demo that would trivially "block 0 of 0".
        self.assertGreaterEqual(result.unsafe_total, 5)
        self.assertEqual(
            result.to_dict()["headline"],
            f"blocked {result.unsafe_total} of {result.unsafe_total} unsafe calls; "
            f"allowed {result.safe_total} of {result.safe_total} safe calls",
        )

    def test_main_exits_zero_when_consistent(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.demo.main([])
        self.assertEqual(code, 0)

    def test_each_decidable_rule_family_fires_at_least_once(self) -> None:
        # The demo should demonstrate the breadth of the gate, not just one rule.
        result = self.demo.run_loop()
        fired = {rule for entry in result.log for rule in entry["rules"]}
        for rule in ("forbidden_tool", "tool_allowlist", "tool_order", "max_tokens", "repeated_action"):
            self.assertIn(rule, fired)


if __name__ == "__main__":
    unittest.main()
