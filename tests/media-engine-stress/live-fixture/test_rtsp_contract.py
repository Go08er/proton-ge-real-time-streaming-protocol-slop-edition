#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

from pathlib import Path
import unittest

import check_rtsp_contract


class RtspContractTests(unittest.TestCase):
    def test_checked_in_helpers_pass_read_only_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        result = check_rtsp_contract.audit(repo_root)
        self.assertEqual("passed", result["status"])
        self.assertFalse(result["rtsp_runtime_executed"])
        self.assertEqual("configuration-and-helper-contract-only", result["claim"])
        self.assertEqual(4, len(result["files"]))

    def test_wildcard_or_udp_configuration_is_rejected(self) -> None:
        media = Path(__file__).resolve().parents[2] / "media"
        text = (media / "mediamtx.yml").read_text(encoding="utf-8")
        with self.assertRaises(check_rtsp_contract.ContractError):
            check_rtsp_contract.validate_config(
                text.replace("127.0.0.1:8554", "0.0.0.0:8554"), hls=False
            )
        with self.assertRaises(check_rtsp_contract.ContractError):
            check_rtsp_contract.validate_config(
                text.replace("rtspTransports: [tcp]", "rtspTransports: [udp]"), hls=False
            )


if __name__ == "__main__":
    unittest.main()
