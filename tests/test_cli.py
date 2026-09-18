"""命令行入口：自检与快照。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CliTest(unittest.TestCase):
    def test_check_reports_bootstrap(self) -> None:
        data_dir = ROOT / "var" / "test-cli-check"
        result = subprocess.run(
            [sys.executable, "-m", "breweryctl", "check", "--data-dir", str(data_dir), "--no-fsync"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("banner", payload)
        self.assertIn("store", payload)

    def test_snapshot_writes_file(self) -> None:
        data_dir = ROOT / "var" / "test-cli-snapshot"
        result = subprocess.run(
            [sys.executable, "-m", "breweryctl", "snapshot", "--data-dir", str(data_dir), "--no-fsync"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(Path(payload["snapshot_path"]).exists())
