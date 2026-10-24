"""Desktop lifecycle and user layout regressions; never touches live Explorer."""
import unittest
import queue
import os
from unittest.mock import Mock, patch

from layout_engine import Rect, can_place_rectangle, restore_rectangles
from smart_desktop import DesktopShell
import smart_desktop


class LayoutTests(unittest.TestCase):
    def test_user_collision_rejected_without_moving_neighbour(self):
        neighbour = Rect("b", 300, 20, 200, 180)
        self.assertFalse(can_place_rectangle(Rect("a", 250, 20, 200, 180), [neighbour]))
        self.assertEqual(neighbour, Rect("b", 300, 20, 200, 180))
        self.assertTrue(can_place_rectangle(Rect("a", 100, 20, 200, 180), [neighbour]))

    def test_touching_manual_layout_survives_restart_exactly(self):
        items = [Rect("a", -900, 8, 200, 180), Rect("b", -700, 8, 200, 180)]
        self.assertEqual(restore_rectangles(items, (-1000, 0, 1000, 900)), {x.key: x for x in items})

    def test_offscreen_profile_is_repaired_after_work_area_shrinks(self):
        result = restore_rectangles([Rect("a", 1800, 400, 200, 180)], (0, 0, 1280, 720))
        self.assertLessEqual(result["a"].x + result["a"].width, 1272)


class DesktopLifecycleTests(unittest.TestCase):
    def make_shell(self):
        shell = object.__new__(DesktopShell)
        shell.host = 10
        shell.desktop_available = True
        shell.panel_windows = {100: 101}
        shell.user32 = Mock()
        shell.user32.GetAncestor.side_effect = lambda hwnd, flag: 10 if flag == 1 else 20
        shell.user32.IsWindow.return_value = True
        shell.user32.IsWindowVisible.return_value = True
        shell.user32.GetWindowLongW.return_value = 0x48000000
        shell._find_overlay_host = Mock(return_value=10)
        shell.desktop_host_is_safe = Mock(return_value=True)
        shell.trace_window_topology = Mock()
        shell._is_own_panel_hwnd = Mock(return_value=True)
        shell.restore_icons = Mock()
        return shell

    def test_host_replacement_invalidates_child_for_Qt_rebuild(self):
        shell = self.make_shell()
        shell._find_overlay_host.return_value = 11
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertEqual(shell.panel_windows, {})
        shell.restore_icons.assert_called_once()
        shell.user32.SetWindowPos.assert_not_called()

    def test_missing_host_restores_icons_once_without_moving_other_windows(self):
        shell = self.make_shell()
        shell._find_overlay_host.return_value = 0
        shell.keep_panels_above_desktop_backdrop()
        shell.keep_panels_above_desktop_backdrop()
        shell.user32.ShowWindow.assert_called_with(101, 0)
        shell.restore_icons.assert_called_once()
        shell.user32.SetWindowPos.assert_not_called()

    def test_lost_visibility_delegates_to_QWindow_not_native_ShowWindow(self):
        shell = self.make_shell()
        shell.user32.IsWindowVisible.return_value = False
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        shell.user32.ShowWindow.assert_not_called()
        shell.user32.SetWindowPos.assert_not_called()

    def test_owner_popup_is_not_misidentified_as_desktop_child(self):
        shell = self.make_shell()
        shell.user32.GetWindowLongW.return_value = 0x88000000
        shell.user32.GetWindow.return_value = 20
        self.assertFalse(shell.panel_is_desktop_child(100))
        shell.keep_panels_above_desktop_backdrop()
        shell.user32.ShowWindow.assert_called_once_with(101, 0)
        self.assertEqual(shell.panel_windows, {})

    def test_missing_unsafe_then_safe_host_has_single_recovery_transition(self):
        shell = self.make_shell()
        shell._find_overlay_host.side_effect = [0, 10, 10]
        shell.desktop_host_is_safe.side_effect = [False, True]
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertTrue(not shell.desktop_available)
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertFalse(shell.desktop_available)
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertTrue(shell.desktop_available)
        shell.restore_icons.assert_called_once()
        shell.user32.SetWindowPos.assert_not_called()

    def test_reused_foreign_hwnd_is_never_hidden_or_reparented(self):
        shell = self.make_shell()
        shell._is_own_panel_hwnd.return_value = False
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertEqual(shell.panel_windows, {})
        shell.user32.ShowWindow.assert_not_called()
        shell.user32.SetWindowPos.assert_not_called()
        shell.restore_icons.assert_called_once()

    def test_own_hwnd_validation_reads_actual_process_identity(self):
        shell = self.make_shell()
        def identify(hwnd, pid):
            pid._obj.value = os.getpid() if hwnd == 100 else os.getpid() + 1
            return 1
        shell.user32.GetWindowThreadProcessId.side_effect = identify
        self.assertTrue(DesktopShell._is_own_panel_hwnd(shell, 100))
        self.assertFalse(DesktopShell._is_own_panel_hwnd(shell, 200))

    def test_legacy_popup_refused_without_mutating_any_window(self):
        shell = self.make_shell()
        self.assertFalse(shell.attach(100))
        self.assertTrue(shell.qt_required)
        shell.user32.SetWindowPos.assert_not_called()
        shell.user32.SetWindowLongW.assert_not_called()

    def test_same_awareness_different_monitor_dpi_is_not_rejected(self):
        shell = self.make_shell()
        shell.user32.GetWindowDpiAwarenessContext.return_value = 2
        shell.user32.AreDpiAwarenessContextsEqual.return_value = True
        shell.user32.GetDpiForWindow.side_effect = [144, 96]
        self.assertTrue(shell.native_child_preflight(100))
        self.assertFalse(hasattr(shell, "_rejected_host"))

    def test_different_awareness_rejected_before_parent_mutation_without_loop(self):
        shell = self.make_shell()
        shell.user32.GetWindowDpiAwarenessContext.side_effect = [2, 0]
        shell.user32.AreDpiAwarenessContextsEqual.return_value = False
        shell.user32.GetDpiForWindow.side_effect = [96, 96]
        self.assertFalse(shell.native_child_preflight(100))
        self.assertEqual(shell._rejected_host, 10)
        self.assertFalse(shell.desktop_available)
        shell.user32.SetParent.assert_not_called()

    def test_hide_icons_and_retry_do_not_undo_failclosed_restoration(self):
        shell = self.make_shell()
        shell.desktop_available = False
        shell.attribute_errors = {"synthetic": 5}
        shell.hide_icons(["synthetic"])
        shell.retry_icon_attributes()
        shell.restore_icons.assert_called_once()
        shell.user32.SetWindowPos.assert_not_called()

    def test_native_recovery_only_shows_own_child_without_any_z_order_change(self):
        shell = self.make_shell()
        self.assertTrue(shell.show_native_child(100))
        call = shell.user32.SetWindowPos.call_args
        self.assertEqual(call.args[0], 100)
        self.assertTrue(call.args[-1] & 0x0004)  # NOZORDER
        self.assertTrue(call.args[-1] & 0x0200)  # NOOWNERZORDER
        self.assertTrue(call.args[-1] & 0x0010)  # NOACTIVATE

    def test_host_above_presented_app_is_rejected_without_reordering(self):
        shell = self.make_shell()
        shell._host_identity_valid = Mock(return_value=True)
        shell.user32.GetTopWindow.return_value = 20
        shell.user32.GetWindow.side_effect = lambda hwnd, relation: 30 if hwnd == 20 else 0
        shell.user32.GetClassNameW.side_effect = lambda hwnd, buffer, size: setattr(buffer, "value", "SyntheticApp") or 12
        shell._window_is_presented = Mock(return_value=True)
        self.assertFalse(DesktopShell.desktop_host_is_safe(shell))
        shell._window_is_presented.return_value = False  # minimized/cloaked/zero-size window
        self.assertTrue(DesktopShell.desktop_host_is_safe(shell))
        shell.user32.SetWindowPos.assert_not_called()


class RendererFallbackTests(unittest.TestCase):
    def test_unavailable_renderer_restores_once_retains_service_until_close_no_restart(self):
        events = queue.Queue()
        events.put({"type": "render"})
        events.put({"type": "settings"})
        events.put({"type": "close"})
        shell = Mock()
        root = Mock()
        with patch.object(smart_desktop.tk, "Tk", return_value=root), \
             patch.object(smart_desktop.messagebox, "showerror") as notice, \
             patch.object(smart_desktop.subprocess, "Popen") as spawn:
            smart_desktop.run_without_renderer(shell, events, "synthetic renderer unavailable")
        shell.restore_icons.assert_called_once()
        notice.assert_called_once()
        root.destroy.assert_called_once()
        spawn.assert_not_called()
        self.assertTrue(events.empty())

    def test_error_dialog_failure_still_preserves_native_and_accepts_close(self):
        events = queue.Queue()
        events.put({"type": "close"})
        shell = Mock()
        with patch.object(smart_desktop.tk, "Tk", side_effect=smart_desktop.tk.TclError("synthetic no display")):
            smart_desktop.run_without_renderer(shell, events, "synthetic renderer unavailable")
        shell.restore_icons.assert_called_once()
        self.assertTrue(events.empty())


if __name__ == "__main__":
    unittest.main()
