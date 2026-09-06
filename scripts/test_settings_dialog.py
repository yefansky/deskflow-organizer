from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from smart_desktop import DesktopShell, OrganizerUI


def monitor(handle: int, index: int, *, primary: bool = False, device: str | None = None) -> dict:
    left = index * 1920
    return {
        "handle": handle,
        "left": left,
        "top": 0,
        "right": left + 1920,
        "bottom": 1040,
        "screen_left": left,
        "screen_top": 0,
        "screen_right": left + 1920,
        "screen_bottom": 1080,
        "device": device or rf"\\.\DISPLAY{index + 1}",
        "primary": primary,
    }


class FakeShell(DesktopShell):
    def __init__(self, monitors: list[dict]) -> None:
        self.current_monitors = monitors

    def monitors(self) -> list[dict]:
        return self.current_monitors


class FakeDialog:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.callback = None
        self.exists = True

    def update_idletasks(self) -> None:
        self.calls.append(("update_idletasks",))

    def winfo_width(self) -> int:
        return 320

    def winfo_reqwidth(self) -> int:
        return 300

    def winfo_height(self) -> int:
        return 280

    def winfo_reqheight(self) -> int:
        return 260

    def geometry(self, value: str) -> None:
        self.calls.append(("geometry", value))

    def deiconify(self) -> None:
        self.calls.append(("deiconify",))

    def attributes(self, name: str, value: bool) -> None:
        self.calls.append(("attributes", name, value))

    def lift(self) -> None:
        self.calls.append(("lift",))

    def focus_force(self) -> None:
        self.calls.append(("focus_force",))

    def after(self, delay: int, callback) -> None:
        self.calls.append(("after", delay))
        self.callback = callback

    def winfo_exists(self) -> bool:
        return self.exists

    def destroy(self) -> None:
        self.calls.append(("destroy",))
        self.exists = False


class SettingsDialogTests(unittest.TestCase):
    def make_ui(self) -> OrganizerUI:
        ui = OrganizerUI.__new__(OrganizerUI)
        ui.model = SimpleNamespace(store=SimpleNamespace(data={"ui": {"target_monitor": "primary"}}))
        ui.shell = SimpleNamespace(
            target_monitor=lambda _preference: {"left": 100, "top": 50, "right": 2100, "bottom": 1250}
        )
        ui.settings_dialog = None
        return ui

    def test_present_settings_dialog_centers_and_brings_it_forward(self) -> None:
        ui = self.make_ui()
        dialog = FakeDialog()

        ui.present_settings_dialog(dialog)

        self.assertIn(("geometry", "+940+510"), dialog.calls)
        self.assertIn(("deiconify",), dialog.calls)
        self.assertIn(("attributes", "-topmost", True), dialog.calls)
        self.assertIn(("lift",), dialog.calls)
        self.assertIn(("focus_force",), dialog.calls)
        self.assertIn(("after", 180), dialog.calls)
        self.assertIsNotNone(dialog.callback)
        dialog.callback()
        self.assertIn(("attributes", "-topmost", False), dialog.calls)

    def test_settings_window_is_not_transient_to_hidden_root(self) -> None:
        source = inspect.getsource(OrganizerUI.settings)
        self.assertNotIn(".transient(", source)
        self.assertIn("present_settings_dialog", source)

    def test_dismiss_clears_singleton_reference(self) -> None:
        ui = self.make_ui()
        dialog = FakeDialog()
        ui.settings_dialog = dialog

        ui.dismiss_settings(dialog)

        self.assertIsNone(ui.settings_dialog)
        self.assertFalse(dialog.exists)


class MonitorFallbackTests(unittest.TestCase):
    def test_fewer_remote_screens_temporarily_use_primary(self) -> None:
        shell = FakeShell([monitor(11, 0, primary=True), monitor(22, 1), monitor(33, 2)])
        ui = {}
        shell.remember_target_monitor(ui, "monitor:33")

        shell.current_monitors = [monitor(101, 0, primary=True), monitor(102, 1)]
        self.assertTrue(shell.reconcile_target_monitor(ui))

        self.assertEqual(ui["target_monitor"], "primary")
        self.assertTrue(ui["monitor_fallback_active"])
        self.assertEqual(ui["preferred_monitor_count"], 3)
        self.assertEqual(ui["preferred_monitor_index"], 2)
        self.assertEqual(ui["preferred_monitor"], "monitor:33")

    def test_three_screen_remote_session_restores_same_screen_number(self) -> None:
        shell = FakeShell([monitor(11, 0, primary=True), monitor(22, 1), monitor(33, 2)])
        ui = {}
        shell.remember_target_monitor(ui, "monitor:33")
        shell.current_monitors = [monitor(101, 0, primary=True), monitor(102, 1)]
        shell.reconcile_target_monitor(ui)

        shell.current_monitors = [
            monitor(201, 0, primary=True, device=r"\\.\RDPDISPLAY1"),
            monitor(202, 1, device=r"\\.\RDPDISPLAY2"),
            monitor(203, 2, device=r"\\.\RDPDISPLAY3"),
        ]
        self.assertTrue(shell.reconcile_target_monitor(ui))

        self.assertEqual(ui["target_monitor"], "monitor:203")
        self.assertFalse(ui["monitor_fallback_active"])
        self.assertEqual(ui["preferred_monitor"], "monitor:33")

    def test_handle_churn_uses_saved_device_without_losing_preference(self) -> None:
        shell = FakeShell([monitor(11, 0, primary=True), monitor(22, 1), monitor(33, 2)])
        ui = {}
        shell.remember_target_monitor(ui, "monitor:22")
        shell.current_monitors = [monitor(91, 0, primary=True), monitor(92, 1), monitor(93, 2)]

        self.assertTrue(shell.reconcile_target_monitor(ui))
        self.assertEqual(ui["target_monitor"], "monitor:92")
        self.assertEqual(ui["preferred_monitor"], "monitor:22")
        self.assertFalse(ui["monitor_fallback_active"])

    def test_explicit_remote_choice_replaces_old_three_screen_preference(self) -> None:
        shell = FakeShell([monitor(11, 0, primary=True), monitor(22, 1), monitor(33, 2)])
        ui = {}
        shell.remember_target_monitor(ui, "monitor:33")
        shell.current_monitors = [monitor(101, 0, primary=True), monitor(102, 1)]
        shell.reconcile_target_monitor(ui)

        self.assertTrue(shell.remember_target_monitor(ui, "monitor:102"))
        self.assertEqual(ui["preferred_monitor_count"], 2)
        self.assertEqual(ui["preferred_monitor_index"], 1)
        self.assertEqual(ui["target_monitor"], "monitor:102")
        self.assertFalse(ui["monitor_fallback_active"])

    def test_missing_legacy_handle_falls_back_without_rewriting_it(self) -> None:
        shell = FakeShell([monitor(101, 0, primary=True), monitor(102, 1)])
        ui = {
            "target_monitor": "monitor:999999",
            "preferred_monitor": "monitor:999999",
            "preferred_monitor_count": 0,
            "preferred_monitor_index": -1,
            "preferred_monitor_device": "",
        }

        self.assertTrue(shell.reconcile_target_monitor(ui))
        self.assertEqual(ui["target_monitor"], "primary")
        self.assertEqual(ui["preferred_monitor"], "monitor:999999")
        self.assertTrue(ui["monitor_fallback_active"])


if __name__ == "__main__":
    unittest.main()
