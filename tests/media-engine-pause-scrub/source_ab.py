#!/usr/bin/env python3
"""Require the A3.8 source audit to reject A3.7 and accept the candidate."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


TARGET_NEGATIVE_MESSAGES = (
    "required order changed",
    "Pause can leave stale waiting/play scheduling state",
    "pause completion can leave stale waiting/play scheduling state",
    "rate completion can leave stale waiting/play scheduling state",
)


def run_audit(audit: Path, source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(audit),
            str(source),
            "--diagnostic-contract",
            "present",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            f"usage: {Path(sys.argv[0]).name} /path/to/a3.7-wine /path/to/a3.8-wine"
        )

    audit = Path(__file__).with_name("audit.py").resolve()
    baseline = Path(sys.argv[1]).resolve()
    candidate = Path(sys.argv[2]).resolve()

    old = run_audit(audit, baseline)
    if old.returncode == 0:
        raise SystemExit(
            "source A/B failed: the A3.7 negative control unexpectedly passed"
        )
    if not any(message in old.stdout for message in TARGET_NEGATIVE_MESSAGES):
        raise SystemExit(
            "source A/B failed: A3.7 was rejected for an unrelated reason:\n"
            f"{old.stdout.rstrip()}"
        )

    new = run_audit(audit, candidate)
    if new.returncode != 0:
        raise SystemExit(
            "source A/B failed: the A3.8 candidate did not pass:\n"
            f"{new.stdout.rstrip()}"
        )

    old_reason = old.stdout.strip().splitlines()[-1]
    print(f"A3.7 negative control rejected: {old_reason}")
    print("A3.8 candidate accepted: media-engine pause/scrub source audit passed")


if __name__ == "__main__":
    main()
