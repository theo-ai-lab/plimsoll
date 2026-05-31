"""Module entrypoint so `python -m plimsoll` works like the console script."""

from __future__ import annotations

from plimsoll.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
