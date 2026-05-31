from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


OPTIONAL_MODULES = {"ruff lint": "ruff", "ruff format check": "ruff"}


COMMANDS = [
    (
        "ruff lint",
        [sys.executable, "-m", "ruff", "check", "."],
        0,
        [],
    ),
    (
        "ruff format check",
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        0,
        [],
    ),
    (
        "tests",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        0,
        [],
    ),
    (
        "native clean",
        [
            sys.executable,
            "-m",
            "plimsoll.cli",
            "run",
            "--input",
            "examples/traces/current_ticket_triage.json",
            "--baseline",
            "examples/traces/baseline_ticket_triage.json",
            "--policy",
            "examples/policies/default_policy.json",
            "--out",
            "runs/sample",
        ],
        0,
        ["runs/sample/report.json", "runs/sample/report.html"],
    ),
    (
        "native regression",
        [
            sys.executable,
            "-m",
            "plimsoll.cli",
            "run",
            "--input",
            "examples/traces/regressed_ticket_triage.json",
            "--baseline",
            "examples/traces/baseline_ticket_triage.json",
            "--policy",
            "examples/policies/default_policy.json",
            "--out",
            "examples/output/regression-demo",
        ],
        1,
        ["examples/output/regression-demo/report.json", "examples/output/regression-demo/report.html"],
    ),
    (
        "otel fixture",
        [
            sys.executable,
            "-m",
            "plimsoll.cli",
            "run",
            "--format",
            "otel",
            "--input",
            "examples/traces/otel_ticket_triage.json",
            "--baseline",
            "examples/traces/baseline_ticket_triage.json",
            "--baseline-format",
            "native",
            "--policy",
            "examples/policies/default_policy.json",
            "--out",
            "runs/otel-sample",
        ],
        0,
        ["runs/otel-sample/report.json"],
    ),
    (
        "ci artifacts",
        [
            sys.executable,
            "-m",
            "plimsoll.cli",
            "run",
            "--input",
            "examples/traces/regressed_ticket_triage.json",
            "--baseline",
            "examples/traces/baseline_ticket_triage.json",
            "--policy",
            "examples/policies/default_policy.json",
            "--out",
            "runs/ci-demo",
            "--junit",
            "--sarif",
        ],
        1,
        ["runs/ci-demo/report.junit.xml", "runs/ci-demo/report.sarif.json"],
    ),
    (
        "policy init",
        [
            sys.executable,
            "-m",
            "plimsoll.cli",
            "init-policy",
            "--input",
            "examples/traces/current_ticket_triage.json",
            "--format",
            "native",
            "--out",
            "runs/inferred-policy.json",
        ],
        0,
        ["runs/inferred-policy.json"],
    ),
    (
        "public fixture validation",
        [sys.executable, "scripts/validate_public_fixtures.py"],
        0,
        ["examples/public_trace_sources.json", "PUBLIC_TRACE_VALIDATION.md"],
    ),
]


def main() -> int:
    for name, command, expected_code, artifacts in COMMANDS:
        required = OPTIONAL_MODULES.get(name)
        if required and not _has_module(required):
            print(f"SKIP {name}: {required} not installed (pip install -e '.[dev]')")
            continue
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        if result.returncode != expected_code:
            print(f"FAIL {name}: expected exit {expected_code}, got {result.returncode}", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return 1
        missing = [artifact for artifact in artifacts if not (ROOT / artifact).exists()]
        if missing:
            print(f"FAIL {name}: missing artifacts {missing}", file=sys.stderr)
            return 1
        print(f"OK {name}")

    regression = json.loads((ROOT / "examples/output/regression-demo/report.json").read_text(encoding="utf-8"))
    if regression["summary"]["failed"] != 1:
        print("FAIL regression summary did not preserve expected failing case", file=sys.stderr)
        return 1
    print("OK regression summary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
