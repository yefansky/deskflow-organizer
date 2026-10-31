"""Manager worker completion uses queues; Tk and persisted state stay on owner."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskflow_manager import Manager


class DeferredThread:
    calls = []
    def __init__(self, target, **_kwargs):
        self.target = target
    def start(self):
        self.calls.append(self.target)


class ManagerAsyncTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(Manager)
        self.manager.root = SimpleNamespace(after=Mock(return_value="scheduled"), after_cancel=Mock(), winfo_exists=lambda: True)
        self.manager.tray_actions = queue.Queue()
        self.manager.data = {"installed": True}
        self.manager.update_status_controls = Mock()
        self.manager.online = Mock(return_value=True)
        self.manager.api_post = Mock()
        self.manager.cell_size = Mock(get=lambda: 48)
        self.manager.font_size = Mock(get=lambda: 10)
        self.manager.layout_footprint = Mock(get=lambda: 70)
        self.manager.alpha = Mock(get=lambda: 22)
        DeferredThread.calls = []

    def run_worker(self):
        target = DeferredThread.calls.pop(0)
        worker = threading.Thread(target=target)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_source_mode_never_checks_or_installs_release(self):
        m = self.manager
        m.update_status = Mock()
        m.update_button = Mock()
        m.update_busy = True
        with patch("deskflow_manager.FROZEN", False), \
             patch("deskflow_manager.latest_release") as latest, \
             patch("deskflow_manager.messagebox.askyesno") as prompt, \
             patch("deskflow_manager.download_and_verify") as download, \
             patch("deskflow_manager.spawn_update") as install:
            m.check_for_updates(silent=True)
            m.check_for_updates()
            m.handle_update_result({"tag_name": "v999.0.0"})
            m.apply_downloaded_update("unused.zip")
        latest.assert_not_called()
        prompt.assert_not_called()
        download.assert_not_called()
        install.assert_not_called()
        self.assertFalse(m.update_busy)

    def test_refresh_coalesces_and_exception_completion_clears_pending(self):
        m = self.manager
        with patch("deskflow_manager.threading.Thread", DeferredThread):
            m.refresh(); m.refresh()
        self.assertEqual(len(DeferredThread.calls), 1)
        m.root.after_cancel.assert_called_once_with("scheduled")
        m.online.side_effect = RuntimeError("health transport failure")
        self.run_worker()
        self.assertTrue(m._health_pending)
        m.process_tray_actions()
        self.assertFalse(m._health_pending)
        self.assertFalse(m._online_cached)

    def test_open_settings_coalesces_and_worker_does_not_touch_tk(self):
        m = self.manager
        with patch("deskflow_manager.threading.Thread", DeferredThread):
            m.open_settings(); m.open_settings()
        self.assertEqual(len(DeferredThread.calls), 1)
        self.run_worker()
        m.api_post.assert_called_once_with("/open-settings")
        m.root.after.assert_not_called()
        m.process_tray_actions()
        self.assertFalse(m._settings_pending)

    def test_launch_failure_returns_error_without_worker_dialog(self):
        m = self.manager
        m.online.return_value = False
        with patch("deskflow_manager.threading.Thread", DeferredThread):
            m.open_settings()
        with patch("deskflow_manager.subprocess.Popen", side_effect=OSError("launch denied")), patch("deskflow_manager.messagebox.showinfo") as message:
            self.run_worker()
            message.assert_not_called()
            m.process_tray_actions()
            self.assertIn("launch denied", message.call_args.args[1])
        self.assertFalse(m._settings_pending)

    def test_pid_is_persisted_only_on_ui_completion(self):
        m = self.manager
        m.online.side_effect = [False, True]
        with patch("deskflow_manager.threading.Thread", DeferredThread):
            m.open_settings()
        with patch("deskflow_manager.subprocess.Popen", return_value=SimpleNamespace(pid=123)), patch("deskflow_manager.save") as save:
            self.run_worker()
            save.assert_not_called()
            self.assertNotIn("pid", m.data)
            m.process_tray_actions()
            save.assert_called_once_with(m.data)
            self.assertEqual(m.data["pid"], 123)

    def test_worker_after_window_exit_only_queues_result(self):
        m = self.manager
        with patch("deskflow_manager.threading.Thread", DeferredThread):
            m.open_settings()
        m.root = Mock()
        self.run_worker()
        m.root.assert_not_called()
        self.assertEqual(len(m.root.mock_calls), 0)
        self.assertEqual(m.tray_actions.get_nowait()[0], "settings-result")

    def test_latest_preferences_wait_for_previous_request(self):
        m = self.manager
        with patch("deskflow_manager.threading.Thread", DeferredThread):
            m.apply_preferences()
            m.font_size.get = lambda: 14
            m.apply_preferences()
        self.assertEqual(len(DeferredThread.calls), 1)
        with patch("deskflow_manager.urlopen") as open_url:
            self.run_worker()
            with patch("deskflow_manager.threading.Thread", DeferredThread):
                m.process_tray_actions()
            self.assertEqual(len(DeferredThread.calls), 1)
            self.run_worker()
            m.process_tray_actions()
            self.assertIn(b'"font_size": 14', open_url.call_args.args[0].data)
        self.assertFalse(m._preferences_pending)


if __name__ == "__main__":
    unittest.main()
