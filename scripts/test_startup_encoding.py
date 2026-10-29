import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from deskflow_manager import Manager


class StartupEncodingTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows command interpreter required")
    def test_chinese_and_spaces_under_legacy_codepage(self):
        root = Path(__file__).resolve().parent.parent / "build"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="d18-", dir=root) as tmp:
            folder = Path(tmp) / "实验 室"
            folder.mkdir()
            marker = folder / "完成.txt"
            script = folder / "启动.py"
            script.write_text("from pathlib import Path\nPath(__file__).with_name('完成.txt').write_text('ok')\n", encoding="utf-8")
            batch = Path(tmp) / "startup.bat"
            with patch("deskflow_manager.STARTUP", batch), patch("deskflow_manager.command", return_value=[sys.executable, str(script)]):
                Manager.write_autostart_entry()
            result = subprocess.run(["cmd", "/d", "/c", "chcp 936 >nul & call startup.bat"], cwd=tmp, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists(), "startup did not execute the Unicode path")
            self.assertEqual(marker.read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
