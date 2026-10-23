import logging
import re
from pathlib import Path
import tempfile
import threading
import unittest

from diagnostics import configure_logging, get_logger, logging_arguments, MAX_LOG_BYTES


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        configure_logging(None)

    def test_utf8_rotation_and_giant_traceback_preserve_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            path = configure_logging("debug", target, role="test")
            logger = get_logger("test")
            logger.debug("中文内容" * 40000)
            try:
                raise ValueError("first-scene-marker 中文堆栈")
            except ValueError:
                logger.exception("test failure")
            configure_logging(None)
            logs = list(target.glob("*.log*"))
            self.assertGreater(len(logs), 1)
            self.assertTrue(all(log.stat().st_size <= MAX_LOG_BYTES for log in logs))
            all_text = "".join(log.read_text(encoding="utf-8") for log in logs)
            debug_payload = "".join(re.sub(r"^\S+ \S+ \d+ debug ", "", line)
                                    for line in all_text.splitlines() if " debug " in line)
            self.assertEqual(debug_payload.count("中"), 40000)
            self.assertEqual(debug_payload.count("文"), 40000)
            self.assertIn("Traceback", all_text)
            self.assertIn("first-scene-marker", all_text)
            self.assertIn("error", all_text)
            self.assertIn("test", path.name)

    def test_disabled_has_no_output_and_forwarding_is_opt_in(self):
        configure_logging(None)
        self.assertEqual(logging_arguments(), [])
        with tempfile.TemporaryDirectory() as temporary:
            configure_logging("info", temporary, role="manager")
            self.assertEqual(logging_arguments(), ["--log-level", "info"])
            configure_logging(None)

    def test_unwritable_destination_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "cannot write diagnostic log"):
                configure_logging("debug", Path(temporary) / "absent")

    def test_multithread_rotation_retains_first_scene_beyond_100_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure_logging("debug", temporary, role="thread-test")
            handler = logging.getLogger("deskflow").handlers[0]
            handler.maxBytes = 1024
            logger = get_logger("concurrent")
            logger.error("FIRST_SCENE_MARKER")
            def write(thread):
                for number in range(35):
                    logger.debug("THREAD_%s_RECORD_%s %s", thread, number, "中" * 280)
            threads = [threading.Thread(target=write, args=(number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            configure_logging(None)
            logs = list(Path(temporary).glob("*.log*"))
            self.assertGreater(len(logs), 100)
            self.assertTrue(all(log.stat().st_size <= 1024 for log in logs))
            content = "".join(log.read_text(encoding="utf-8") for log in logs)
            self.assertIn("FIRST_SCENE_MARKER", content)
            for thread in range(4):
                for number in range(35):
                    self.assertIn(f"THREAD_{thread}_RECORD_{number} ", content)


if __name__ == "__main__":
    unittest.main()
