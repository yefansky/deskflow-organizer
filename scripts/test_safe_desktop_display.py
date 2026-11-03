"""Display-failure regressions with fake Win32 APIs; never hides real files."""
import ctypes
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from smart_desktop import DesktopShell


class NativeHideSafetyTests(unittest.TestCase):
    def make_shell(self):
        shell = object.__new__(DesktopShell)
        shell.host = 10
        shell.panel_windows = {100: 101}
        shell.hidden_panel_windows = set()
        shell.attribute_state = {}
        shell.attribute_errors = {}
        shell.organized_native_paths = []
        shell.hidden = False
        shell.user32 = Mock()
        shell.user32.IsWindow.return_value = True
        shell.panel_is_desktop_child = Mock(return_value=True)
        shell.panel_is_visible = Mock(return_value=True)
        shell.kernel32 = Mock()
        shell.kernel32.GetFileAttributesW.return_value = 32
        shell.kernel32.SetFileAttributesW.return_value = 1
        shell._find_listview = Mock(return_value=1)
        shell._save_attribute_state = Mock()
        shell._ensure_guard = Mock()
        shell._notify_desktop_changed = Mock()
        shell.restore_icons = Mock()
        return shell

    def test_failed_render_empty_panels_cannot_hide_on_later_scan(self):
        shell = self.make_shell()
        shell.panel_windows = {}
        shell.hide_icons(["G:/isolated/changed-after-failed-render.txt"])
        shell.kernel32.SetFileAttributesW.assert_not_called()
        shell.restore_icons.assert_called_once()

    def test_missing_host_invalid_window_owner_or_visibility_restores(self):
        for failure in ("host", "window", "owner", "visibility"):
            with self.subTest(failure=failure):
                shell = self.make_shell()
                if failure == "host": shell.host = 0
                if failure == "window": shell.user32.IsWindow.return_value = False
                if failure == "owner": shell.panel_is_desktop_child.return_value = False
                if failure == "visibility": shell.panel_is_visible.return_value = False
                shell.hide_icons(["G:/isolated/item.txt"])
                shell.kernel32.SetFileAttributesW.assert_not_called()
                shell.restore_icons.assert_called_once()

    def test_intentionally_hidden_favorite_panels_preserve_existing_semantics(self):
        shell = self.make_shell()
        shell.hidden_panel_windows = {100}
        shell.panel_is_visible.return_value = False
        path = str(Path("G:/isolated/item.txt"))
        shell.hide_icons([path])
        shell.kernel32.SetFileAttributesW.assert_called_once_with(path, 38)
        shell.restore_icons.assert_not_called()

    def test_intentional_hiding_does_not_exempt_owner_or_window_validation(self):
        shell = self.make_shell()
        shell.hidden_panel_windows = {100}
        shell.panel_is_desktop_child.return_value = False
        shell.hide_icons(["G:/isolated/item.txt"])
        shell.kernel32.SetFileAttributesW.assert_not_called()
        shell.restore_icons.assert_called_once()

    def test_failed_display_clears_stale_hide_retry_target(self):
        shell = self.make_shell()
        shell.attribute_errors = {"G:/isolated/old.txt": 5}
        shell.organized_native_paths = ["G:/isolated/old.txt"]
        shell.panel_windows = {}
        shell.retry_icon_attributes()
        self.assertEqual(shell.attribute_errors, {})
        self.assertEqual(shell.organized_native_paths, [])
        shell.kernel32.SetFileAttributesW.assert_not_called()

    def test_valid_visible_panels_still_hide_organized_items(self):
        shell = self.make_shell()
        path = str(Path("G:/isolated/item.txt"))
        shell.hide_icons([path])
        shell.kernel32.SetFileAttributesW.assert_called_once_with(path, 38)
        self.assertEqual(shell.attribute_state, {path: 32})
        shell._ensure_guard.assert_called_once()

    def test_restore_failure_keeps_exact_original_recovery_evidence(self):
        shell = self.make_shell()
        path = "G:/isolated/denied.txt"
        shell.attribute_state = {path: 32}
        shell.panel_windows = {}
        shell.kernel32.GetFileAttributesW.return_value = 38
        shell.kernel32.SetFileAttributesW.return_value = 0
        shell.restore_icons = DesktopShell.restore_icons.__get__(shell)
        with patch("smart_desktop.Path.exists", return_value=True):
            shell.hide_icons([path])
        self.assertEqual(shell.attribute_state, {path: 32})
        shell.kernel32.SetFileAttributesW.assert_called_once_with(path, 32)


class AttachDiagnosticsTests(unittest.TestCase):
    def make_shell(self, denied=False):
        shell = object.__new__(DesktopShell)
        shell.host = 0x100000010
        shell.panel_windows = {}
        shell.user32 = Mock()
        shell.kernel32 = Mock()
        state = {"error": 0}
        shell.kernel32.SetLastError.side_effect = lambda value: state.update(error=value)
        shell.kernel32.GetLastError.side_effect = lambda: state["error"]
        shell.user32.GetAncestor.side_effect = lambda hwnd, _flag: hwnd
        shell.user32.GetWindowLongW.return_value = 0
        shell.user32.SetWindowLongW.return_value = 0  # Valid previous zero.
        shell.user32.SetWindowPos.return_value = 1
        shell.user32.GetWindow.return_value = 0 if denied else shell.host
        def set_owner(*_args):
            state["error"] = 5 if denied else 0
            return 0
        shell.user32.SetWindowLongPtrW.side_effect = set_owner
        return shell

    def test_owner_error_is_captured_before_later_calls_clear_it(self):
        shell = self.make_shell(denied=True)
        with self.assertLogs("deskflow.smart_desktop", level="DEBUG") as logs:
            self.assertFalse(shell.attach(0x200000020))
        self.assertTrue(any("operation=SetWindowLongPtr.owner" in line and "error=5" in line
                            for line in logs.output))
        self.assertTrue(any("operation=GetWindow.owner" in line and "error=0" in line
                            for line in logs.output))
        self.assertEqual(shell.panel_windows, {})

    def test_previous_zero_without_error_is_success_and_handles_are_pointer_sized(self):
        shell = self.make_shell()
        with self.assertLogs("deskflow.smart_desktop", level="DEBUG") as logs:
            self.assertTrue(shell.attach(0x200000020))
        self.assertFalse(any("ERROR" in line for line in logs.output))
        self.assertEqual(shell.panel_windows, {0x200000020: 0x200000020})
        self.assertEqual(shell.user32.SetWindowLongPtrW.argtypes,
                         [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t])
        self.assertEqual(shell.user32.GetWindow.restype, ctypes.wintypes.HWND)
        shell.user32.SetWindowLongPtrW.assert_called_once_with(0x200000020, -8, shell.host)
        self.assertEqual(shell.kernel32.SetLastError.call_count,
                         shell.kernel32.GetLastError.call_count)


class QtFailedDisplayTests(unittest.TestCase):
    def test_signature_change_after_failed_attach_never_hides_without_panels(self):
        # Offscreen Qt + fake Shell fixture; every attribute API is a Mock.
        from test_qt_incremental import Fixture
        fixture = Fixture()
        try:
            ui = fixture.ui
            ui.shell.attach = Mock(return_value=False)
            for panel in ui.panels.values(): panel._appearance = None
            ui.render()
            self.assertEqual(ui.panels, {})
            shell = NativeHideSafetyTests().make_shell()
            shell.__dict__.update(vars(ui.shell))
            shell.panel_windows = {}
            # Retain the real safety implementation, not the fixture no-op.
            shell.__dict__.pop("hide_icons", None)
            shell.__dict__.pop("retry_icon_attributes", None)
            shell.restore_icons = Mock()
            ui.shell = shell
            ui.model.desktop_signature = lambda: ("changed-after-failed-attach",)
            ui.tick()
            shell.kernel32.SetFileAttributesW.assert_not_called()
            shell.restore_icons.assert_called()
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
