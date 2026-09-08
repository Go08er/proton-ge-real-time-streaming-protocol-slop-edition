#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Read-only contract check for the existing loopback MediaMTX helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_small(path: Path) -> str:
    require(path.is_file() and not path.is_symlink(), f"missing regular file: {path.name}")
    require(0 < path.stat().st_size <= 256 * 1024, f"file size is invalid: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"file is not UTF-8: {path.name}") from error


def require_setting(text: str, setting: str, label: str) -> None:
    require(re.search(rf"(?m)^[ \t]*{re.escape(setting)}\s*$", text) is not None,
            f"{label} lacks exact setting: {setting}")


def validate_config(text: str, *, hls: bool) -> None:
    label = "HLS MediaMTX config" if hls else "RTSP MediaMTX config"
    require(text.startswith("# SPDX-License-Identifier: BSD-3-Clause\n"),
            f"{label} lacks its BSD SPDX header")
    for setting in (
        "api: false",
        "metrics: false",
        "pprof: false",
        "playback: false",
        "rtsp: true",
        "rtspTransports: [tcp]",
        'rtspEncryption: "no"',
        "rtspAddress: 127.0.0.1:8554",
        "rtmp: false",
        "webrtc: false",
        "srt: false",
        "moq: false",
        'ips: ["127.0.0.1"]',
    ):
        require_setting(text, setting, label)
    require("0.0.0.0" not in text and "[::]" not in text and "localhost" not in text,
            f"{label} contains a nonliteral listener")
    require(re.search(r"(?i)https?://|rtsp://|rtspt://", text) is None,
            f"{label} contains an external-style URL")
    if hls:
        for setting in (
            "hls: true",
            "hlsAddress: 127.0.0.1:8888",
            "hlsEncryption: false",
            "hlsAlwaysRemux: true",
            "hlsVariant: mpegts",
            'hlsDirectory: ""',
        ):
            require_setting(text, setting, label)
    else:
        require_setting(text, "hls: false", label)


def validate_scripts(runner: str, probe: str) -> None:
    for label, text in (("runner", runner), ("probe", probe)):
        require(text.startswith("#!/usr/bin/env bash\n# SPDX-License-Identifier: BSD-3-Clause\n"),
                f"{label} lacks its BSD SPDX header")
        require("curl " not in text and "wget " not in text,
                f"{label} contains a download-capable command")
        require("0.0.0.0" not in text, f"{label} contains a wildcard listener")
    require('RTSPT_FIXTURE_URL="rtspt://127.0.0.1:8554/fixture"' in runner,
            "runner RTSPT URL differs")
    require('HLS_LIVE_URL="http://127.0.0.1:8888/fixture/index.m3u8"' in runner,
            "runner HLS URL differs")
    require("-rtsp_transport tcp" in runner and "-rtsp_transport tcp" in probe,
            "RTSP helper does not require interleaved TCP")
    require("timeout --foreground" in probe,
            "RTSP decode probe lacks its wall-clock timeout")


def audit(repo_root: Path) -> dict:
    media = repo_root.resolve() / "tests/media"
    files = {
        "mediamtx.yml": media / "mediamtx.yml",
        "mediamtx-hls-live.yml": media / "mediamtx-hls-live.yml",
        "run-a34-fixture.sh": media / "run-a34-fixture.sh",
        "probe-stream.sh": media / "probe-stream.sh",
    }
    texts = {name: read_small(path) for name, path in files.items()}
    validate_config(texts["mediamtx.yml"], hls=False)
    validate_config(texts["mediamtx-hls-live.yml"], hls=True)
    validate_scripts(texts["run-a34-fixture.sh"], texts["probe-stream.sh"])
    return {
        "claim": "configuration-and-helper-contract-only",
        "files": {name: sha256(path) for name, path in sorted(files.items())},
        "rtsp_runtime_executed": False,
        "schema": 1,
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    args = parser.parse_args()
    try:
        result = audit(args.repo_root)
    except (ContractError, OSError) as error:
        print(f"RTSP fixture contract: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
