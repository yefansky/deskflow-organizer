"""Regression tests for the Qt settings dialog's non-persistent previews."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from qt_desktop import QtOrganizerUI


class FakeStore:
    def __init__(self) -> None:
        self.data = {"ui": {"layout_profiles": {}}}
        self.save_calls = 0

    def save(self) -> None:
        self.save_calls += 1


class QtSettingsPreviewTests(unittest.TestCase):
    def make_ui(self) -> QtOrganizerUI:
        ui = object.__new__(QtOrganizerUI)
        ui.model = SimpleNamespace(store=FakeStore())
        ui._settings_preview = False
        ui.panels = {}
        return ui

    def test_layout_preview_updates_memory_only_and_marks_preview_mode(self) -> None:
        ui = self.make_ui()
        modes: list[bool] = []
        ui.render = lambda: modes.append(ui._settings_preview)

        ui.preview_settings_layout({"layout_footprint": 0.63, "font_size": 11})

        self.assertEqual(ui.model.store.data["ui"]["layout_footprint"], 0.63)
        self.assertEqual(ui.model.store.data["ui"]["font_size"], 11)
        self.assertEqual(modes, [True])
        self.assertFalse(ui._settings_preview)
        self.assertEqual(ui.model.store.save_calls, 0)

    def test_footprint_preview_moves_existing_panels_without_render(self) -> None:
        ui = self.make_ui()
        calls: list[tuple[int, int, int, int]] = []
        ui.panels = {"tools": SimpleNamespace(setGeometry=lambda *rect: calls.append(rect))}
        ui.shell = SimpleNamespace(
            target_monitor=lambda _target: {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
            keep_panels_above_desktop_backdrop=lambda: None,
        )
        ui.model.scan = lambda: {"tools": []}
        ui.auto_layout = lambda _groups, _monitor: {"tools": (1600, 12, 304, 320)}
        ui.app = SimpleNamespace(processEvents=lambda: None)
        ui.render = lambda: self.fail("footprint preview must not rebuild panels")

        ui.preview_footprint_layout(0.81)

        self.assertEqual(calls, [(1600, 12, 304, 320)])
        self.assertEqual(ui.model.store.data["ui"]["layout_footprint"], 0.81)
        self.assertEqual(ui.model.store.save_calls, 0)

    def test_new_monitor_profile_is_not_saved_during_preview(self) -> None:
        ui = self.make_ui()
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 1080}
        ui._settings_preview = True

        ui.layout_profile(monitor)

        self.assertEqual(ui.model.store.save_calls, 0)

    def test_new_monitor_profile_is_saved_outside_preview(self) -> None:
        ui = self.make_ui()
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 1080}

        ui.layout_profile(monitor)

        self.assertEqual(ui.model.store.save_calls, 1)


if __name__ == "__main__":
    unittest.main()
