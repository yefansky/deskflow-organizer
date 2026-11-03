"""Tray receipts and failure recovery, without creating a native UI window."""
import ctypes
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deskflow_manager import Manager, RegisteredTrayIcon, notify_tray, tray_win32


class TrayRegistrationTests(unittest.TestCase):
    def manager(self, registered=False, alive=True):
        m = object.__new__(Manager)
        m.root = Mock()
        m.root.winfo_exists.return_value = True
        m.tray = Mock(get=lambda: True)
        m.tray_status = Mock()
        m.tray_actions = queue.Queue()
        m._tray_ready = registered
        m._tray_hide_pending = False
        m.tray_icon = Mock(registered=registered)
        m.tray_icon.worker_alive.return_value = alive
        return m

    def icon(self):
        icon = object.__new__(RegisteredTrayIcon)
        icon.registered = False
        icon._report = Mock()
        icon._worker = None
        icon._loop_entered = False
        icon._running = False
        icon._hwnd = 123
        icon._menu_hwnd = None
        icon._atom = 1
        icon._icon_handle = None
        return icon

    def test_failed_registration_never_withdraws_manager(self):
        m = self.manager()
        m.hide_to_tray()
        m.root.withdraw.assert_not_called()
        m.root.deiconify.assert_called_once()
        self.assertTrue(m._tray_hide_pending)

    def test_registration_receipt_allows_pending_close(self):
        m = self.manager()
        m.hide_to_tray()
        m.tray_icon.registered = True
        m.tray_actions.put(("tray-result", (m.tray_icon, True, "ADD ok")))
        m.process_tray_actions()
        m.root.withdraw.assert_called_once()
        self.assertTrue(m._tray_ready)
        self.assertFalse(m._tray_hide_pending)

    def test_error_receipt_reopens_manager_without_worker_tk_calls(self):
        m = self.manager(registered=True)
        m.tray_actions.put(("tray-result", (m.tray_icon, False, "ADD false error=0")))
        m.root.deiconify.assert_not_called()
        with patch("deskflow_manager.logger.warning") as log:
            m.process_tray_actions()
            self.assertIn("error=0", log.call_args.args[1])
        self.assertFalse(m._tray_ready)
        m.root.deiconify.assert_called_once()
        self.assertIn("保持打开", m.tray_status.set.call_args.args[0])

    def test_stale_success_cannot_hide_after_worker_exits(self):
        m = self.manager(registered=True, alive=False)
        m._tray_hide_pending = True
        m.tray_actions.put(("tray-result", (m.tray_icon, True, "previous ADD")))
        m.process_tray_actions()
        m.root.withdraw.assert_not_called()
        self.assertFalse(m._tray_ready)

    def test_pending_receipt_does_not_hide_explicitly_opened_manager(self):
        m = self.manager()
        m._tray_hide_pending = True
        m.tray_actions.put(("show", None))
        m.tray_actions.put(("tray-result", (m.tray_icon, True, "ADD ok")))
        m.process_tray_actions()
        m.root.withdraw.assert_not_called()

    def test_living_but_unregistered_icon_does_not_spawn_more_threads(self):
        m = self.manager()
        with patch("deskflow_manager.RegisteredTrayIcon") as create:
            self.assertFalse(m.ensure_tray())
            self.assertFalse(m.ensure_tray())
            create.assert_not_called()
        m.retry_tray()
        m.tray_icon.retry_registration.assert_called_once()

    def test_dead_thread_can_be_replaced_only_when_requested(self):
        m = self.manager(alive=False)
        m.tray_menu_items = Mock(return_value=())
        m.tray_image = Mock()
        with patch("deskflow_manager.RegisteredTrayIcon") as create:
            m.retry_tray()
            create.assert_called_once()
            create.return_value.run_detached.assert_called_once()
            create.return_value.worker_alive.return_value = True
            m.ensure_tray()
            create.assert_called_once()

    def test_native_registration_bool_and_zero_error_are_not_success(self):
        icon = self.icon()
        with patch("deskflow_manager.notify_tray", return_value=(False, 0)) as send:
            self.assertFalse(icon._message(tray_win32.NIM_ADD, 0))
        self.assertEqual(send.call_args.args[1].uID, 0)
        self.assertEqual(send.call_args.args[1].hWnd, 123)
        self.assertFalse(icon.registered)
        self.assertIn("error=0", icon._report.call_args.args[2])

    def test_add_modify_delete_receipts(self):
        icon = self.icon()
        with patch("deskflow_manager.notify_tray", return_value=(True, 0)):
            icon._message(tray_win32.NIM_ADD, 0)
            self.assertTrue(icon.registered)
            icon._message(tray_win32.NIM_MODIFY, 0)
            self.assertTrue(icon.registered)
            icon._message(tray_win32.NIM_DELETE, 0)
            self.assertFalse(icon.registered)
        with patch("deskflow_manager.notify_tray", return_value=(True, 0)):
            icon._message(tray_win32.NIM_ADD, 0)
        with patch("deskflow_manager.notify_tray", return_value=(False, 5)):
            icon._message(tray_win32.NIM_MODIFY, 0)
        self.assertFalse(icon.registered)

    def test_notify_preserves_immediate_error_and_checks_bool(self):
        api = Mock(return_value=0)
        events = []
        api.side_effect = lambda *_: events.append("api") or 0
        with patch("deskflow_manager.ctypes.WinDLL", return_value=SimpleNamespace(Shell_NotifyIconW=api)), \
             patch("deskflow_manager.ctypes.set_last_error", side_effect=lambda e: events.append(("clear", e))), \
             patch("deskflow_manager.ctypes.get_last_error", side_effect=lambda: events.append("error") or 0):
            self.assertEqual(notify_tray(0, tray_win32.NOTIFYICONDATAW()), (False, 0))
        self.assertEqual(events, [("clear", 0), "api", "error"])

    def test_retry_posts_to_existing_thread_and_keeps_backend_restart_handler(self):
        icon = self.icon()
        icon._worker = Mock(is_alive=lambda: True)
        with patch("deskflow_manager.tray_win32.PostMessage") as post:
            self.assertTrue(icon.retry_registration())
            post.assert_called_once_with(123, icon.RETRY_MESSAGE, 0, 0)
        icon._worker = Mock(is_alive=lambda: False)
        with patch("deskflow_manager.tray_win32.PostMessage") as post:
            self.assertFalse(icon.retry_registration())
            post.assert_not_called()
        # Explorer rebuild still uses the mature backend's existing handler.
        self.assertIs(RegisteredTrayIcon._on_taskbarcreated, __import__("pystray").Icon._on_taskbarcreated)

    def test_explicit_retry_reuses_icon_and_returns_new_add_receipt(self):
        icon = self.icon()
        icon._icon_handle = 456
        icon._title = "DESKFLOW test"
        with patch("deskflow_manager.notify_tray", side_effect=[(False, 0), (True, 0)]) as send:
            icon._retry_message(0, 0)
        icon._icon_handle = None  # Synthetic handle must never reach DestroyIcon.
        self.assertEqual([c.args[0] for c in send.call_args_list],
                         [tray_win32.NIM_DELETE, tray_win32.NIM_ADD])
        self.assertTrue(icon.registered)
        self.assertTrue(icon._report.call_args.args[1])

    def test_pre_ready_thread_failure_is_reported_and_cleanup_is_bounded(self):
        icon = self.icon()
        with patch("deskflow_manager.pystray.Icon._run", side_effect=OSError("window denied")), \
             patch("deskflow_manager.tray_win32.DestroyWindow") as destroy, \
             patch.object(icon, "_unregister_class") as unregister:
            icon.run_detached()
            icon._worker.join(2)
        self.assertFalse(icon.worker_alive())
        self.assertFalse(icon.registered)
        destroy.assert_called_once_with(123)
        unregister.assert_called_once_with(1)
        self.assertTrue(any("window denied" in call.args[2] for call in icon._report.call_args_list))

    def check_partial_window_failure(self, failed_window):
        icon = self.icon()
        icon._hwnd = None
        handles = [101, 202]
        effects = [OSError("filter denied")] if failed_window == 1 else [None, OSError("filter denied")]
        with patch("deskflow_manager.tray_win32.PeekMessage"), \
             patch("deskflow_manager.tray_win32.GetModuleHandle", return_value=999), \
             patch("deskflow_manager.tray_win32.CreateWindowEx", side_effect=handles) as create, \
             patch("deskflow_manager.tray_win32.ChangeWindowMessageFilterEx", side_effect=effects), \
             patch("deskflow_manager.tray_win32.DestroyWindow") as destroy, \
             patch.object(icon, "_unregister_class") as unregister:
            # Exercise the actual backend _run and both _create_window calls,
            # rather than replacing the entire message-loop startup with a mock.
            icon.run_detached()
            icon._worker.join(2)
        self.assertFalse(icon.worker_alive())
        self.assertFalse(icon.registered)
        self.assertEqual(create.call_count, failed_window)
        self.assertCountEqual([c.args[0] for c in destroy.call_args_list], handles[:failed_window])
        unregister.assert_called_once_with(1)
        self.assertFalse(icon._loop_entered)
        self.assertTrue(any("filter denied" in c.args[2] for c in icon._report.call_args_list))

    def test_first_window_filter_failure_destroys_half_created_hwnd(self):
        self.check_partial_window_failure(1)

    def test_second_window_filter_failure_destroys_both_hwnds(self):
        self.check_partial_window_failure(2)


if __name__ == "__main__":
    unittest.main()
