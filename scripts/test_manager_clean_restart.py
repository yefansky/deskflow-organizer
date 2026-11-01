"""Tray entry that rebuilds DESKFLOW from a clean parent (system:448 recovery)."""
import json
import os
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import deskflow_manager
from deskflow_manager import Manager, process_redirection_flags


class CleanRestartTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(Manager)
        self.manager.root = SimpleNamespace(after=Mock(return_value="scheduled"), after_cancel=Mock(), winfo_exists=lambda: True)
        self.manager.tray_actions = queue.Queue()
        self.manager.data = {"installed": True, "pid": 4321}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_menu_exposes_clean_restart_item(self):
        with patch.object(Manager, "monitor_choices_for_manager", staticmethod(lambda: {"primary": "primary"})):
            items = self.manager.tray_menu_items()
        labels = [getattr(item, "text", "") for item in items]
        self.assertTrue(any("修复启动上下文" in label for label in labels), labels)
        target = next(item for item in items if "修复启动上下文" in getattr(item, "text", ""))
        target(None)
        self.assertEqual(self.manager.tray_actions.get_nowait()[0], "restart-clean")

    def test_tray_action_runs_restart_and_stops_dispatch(self):
        self.manager.restart_clean = Mock()
        self.manager.tray_actions.put(("restart-clean", None))
        self.manager.process_tray_actions()
        self.manager.restart_clean.assert_called_once_with()
        self.manager.root.after.assert_not_called()

    def test_cancel_keeps_current_instance(self):
        with patch("deskflow_manager.process_redirection_flags", return_value=1), \
             patch("deskflow_manager.messagebox.askyesno", return_value=False), \
             patch.object(Manager, "spawn_clean_restart") as spawn:
            self.manager.exit_desktopflow = Mock()
            self.manager.restart_clean()
        spawn.assert_not_called()
        self.manager.exit_desktopflow.assert_not_called()

    def test_prompt_reports_enforced_policy(self):
        with patch("deskflow_manager.process_redirection_flags", return_value=1), \
             patch("deskflow_manager.messagebox.askyesno", return_value=False) as prompt:
            self.manager.restart_clean()
        self.assertIn("重定向信任", prompt.call_args.args[1])

    def test_prompt_reports_clean_policy(self):
        with patch("deskflow_manager.process_redirection_flags", return_value=0), \
             patch("deskflow_manager.messagebox.askyesno", return_value=False) as prompt:
            self.manager.restart_clean()
        self.assertIn("正常", prompt.call_args.args[1])

    def test_confirm_writes_plan_then_exits(self):
        self.manager.exit_desktopflow = Mock()
        with patch("deskflow_manager.STATE_DIR", Path(self.tmp.name)), \
             patch("deskflow_manager.process_redirection_flags", return_value=1), \
             patch("deskflow_manager.messagebox.askyesno", return_value=True), \
             patch("deskflow_manager.subprocess.Popen") as popen:
            self.manager.restart_clean()
        plan = json.loads((Path(self.tmp.name) / "clean-restart-plan.json").read_text(encoding="utf-8"))
        script_path = Path(self.tmp.name) / "clean-restart.ps1"
        script_text = script_path.read_text(encoding="utf-8-sig")
        self.assertEqual(plan["wait_pid"], os.getpid())
        self.assertTrue(plan["executable"])
        self.assertTrue(any("deskflow_manager.py" in str(arg) for arg in plan["arguments"]))
        self.assertIn("--start", plan["arguments"])
        self.assertIn("Invoke-CimMethod", script_text)
        self.assertIn("Win32_Process", script_text)
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertIn("-File", command)
        self.assertEqual(command[-1], str(script_path))
        self.manager.exit_desktopflow.assert_called_once_with()

    def test_launch_failure_reports_and_keeps_instance(self):
        self.manager.exit_desktopflow = Mock()
        with patch("deskflow_manager.STATE_DIR", Path(self.tmp.name)), \
             patch("deskflow_manager.process_redirection_flags", return_value=0), \
             patch("deskflow_manager.messagebox.askyesno", return_value=True), \
             patch("deskflow_manager.messagebox.showerror") as error, \
             patch("deskflow_manager.subprocess.Popen", side_effect=OSError("denied")):
            self.manager.restart_clean()
        error.assert_called_once()
        self.assertIn("denied", error.call_args.args[1])
        self.manager.exit_desktopflow.assert_not_called()

    def test_policy_reader_returns_int_for_current_process(self):
        flags = process_redirection_flags(os.getpid())
        self.assertIsInstance(flags, int)
        self.assertGreaterEqual(flags, 0)
        self.assertIsNone(process_redirection_flags(0))

    def test_launcher_command_matches_runtime_mode(self):
        with patch("deskflow_manager.STATE_DIR", Path(self.tmp.name)), patch("deskflow_manager.FROZEN", False):
            self.manager.write_clean_restart_launcher()
            source_plan = json.loads((Path(self.tmp.name) / "clean-restart-plan.json").read_text(encoding="utf-8"))
        with patch("deskflow_manager.STATE_DIR", Path(self.tmp.name)), patch("deskflow_manager.FROZEN", True), \
             patch("deskflow_manager.command", return_value=[r"C:\App\DESKFLOW.exe", "--start"]), \
             patch("deskflow_manager.APP_ROOT", Path(r"C:\App")):
            self.manager.write_clean_restart_launcher()
            frozen_plan = json.loads((Path(self.tmp.name) / "clean-restart-plan.json").read_text(encoding="utf-8"))
        self.assertTrue(source_plan["executable"].lower().endswith(("python.exe", "pythonw.exe")))
        self.assertEqual(frozen_plan["executable"], r"C:\App\DESKFLOW.exe")
        self.assertEqual(frozen_plan["cwd"], r"C:\App")


if __name__ == "__main__":
    unittest.main()
