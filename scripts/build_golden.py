"""Regenerate the committed golden reports under ``examples/output/golden/``.

These are the canonical reference outputs linked from the README and published to
GitHub Pages, so anyone can see what Plimsoll produces without running it. They
mirror the smoke-test CLI invocations exactly; only the output filenames differ.

Deterministic and offline. Run from anywhere::

    python scripts/build_golden.py

Writes:
  examples/output/golden/clean-report.{json,html}      (current vs baseline, clean)
  examples/output/golden/regression-report.{json,html} (regressed vs baseline)
  examples/output/golden/otel-report.json              (OTel-format input)
  examples/output/golden/inferred-policy.json          (init-policy on current)
  examples/output/golden/report.junit.xml              (regression, JUnit)
  examples/output/golden/report.sarif.json             (regression, SARIF)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACES = "examples/traces"
POLICY = "examples/policies/default_policy.json"
GOLDEN = ROOT / "examples" / "output" / "golden"


def _run(args: list[str]) -> None:
    result = subprocess.run([sys.executable, "-m", "plimsoll.cli", *args], cwd=ROOT, text=True, capture_output=True)
    # `run` exits 1 when findings are present; that is expected for the regression cases.
    if result.returncode not in (0, 1):
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args)}")


def main() -> int:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)

        _run(
            [
                "run",
                "--input",
                f"{TRACES}/current_ticket_triage.json",
                "--baseline",
                f"{TRACES}/baseline_ticket_triage.json",
                "--policy",
                POLICY,
                "--out",
                str(out / "clean"),
            ]
        )
        shutil.copyfile(out / "clean" / "report.json", GOLDEN / "clean-report.json")
        shutil.copyfile(out / "clean" / "report.html", GOLDEN / "clean-report.html")

        _run(
            [
                "run",
                "--input",
                f"{TRACES}/regressed_ticket_triage.json",
                "--baseline",
                f"{TRACES}/baseline_ticket_triage.json",
                "--policy",
                POLICY,
                "--out",
                str(out / "reg"),
                "--junit",
                "--sarif",
            ]
        )
        shutil.copyfile(out / "reg" / "report.json", GOLDEN / "regression-report.json")
        shutil.copyfile(out / "reg" / "report.html", GOLDEN / "regression-report.html")
        shutil.copyfile(out / "reg" / "report.junit.xml", GOLDEN / "report.junit.xml")
        shutil.copyfile(out / "reg" / "report.sarif.json", GOLDEN / "report.sarif.json")

        _run(
            [
                "run",
                "--format",
                "otel",
                "--input",
                f"{TRACES}/otel_ticket_triage.json",
                "--baseline",
                f"{TRACES}/baseline_ticket_triage.json",
                "--baseline-format",
                "native",
                "--policy",
                POLICY,
                "--out",
                str(out / "otel"),
            ]
        )
        shutil.copyfile(out / "otel" / "report.json", GOLDEN / "otel-report.json")

        _run(["init-policy", "--input", f"{TRACES}/current_ticket_triage.json", "--out", str(out / "inferred.json")])
        shutil.copyfile(out / "inferred.json", GOLDEN / "inferred-policy.json")

    for path in sorted(GOLDEN.iterdir()):
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
