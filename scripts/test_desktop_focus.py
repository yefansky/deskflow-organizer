"""Favorite focus regressions use synthetic Qt panels and no live desktop writes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from desktop_focus import DoubleClickTracker, DesktopClickWatcher
from smart_desktop import Store
from test_qt_incremental import Fixture
import test_window_layout


class ClickTests(unittest.TestCase):
    def setUp(self):
        self.tracker = DoubleClickTracker(500, 8, 8, 4, 4)

    def click(self, time, x=100, y=100, blank=True):
        self.assertFalse(self.tracker.feed("down", x, y, time, blank))
        return self.tracker.feed("up", x, y, time + 10, blank)

    def test_two_clicks_toggle_once_and_four_toggle_twice(self):
        self.assertEqual([self.click(t) for t in (100, 200, 300, 400)], [False, True, False, True])

    def test_icon_application_or_panel_click_interrupts_blank_pair(self):
        self.click(100)
        self.click(200, blank=False)
        self.assertFalse(self.click(300))
        self.assertTrue(self.click(400))

    def test_single_slow_far_drag_or_right_click_does_not_toggle(self):
        self.assertFalse(self.click(100))
        self.assertFalse(self.click(700))
        self.assertFalse(self.click(800, x=110))
        self.tracker.feed("down", 110, 100, 900, True)
        self.assertFalse(self.tracker.feed("up", 130, 100, 920, True))
        self.assertFalse(self.click(1000))
        self.tracker.feed("cancel", 110, 100, 1050, False)
        self.assertFalse(self.click(1100))

    def test_long_hold_and_drag_away_then_back_cancel(self):
        self.tracker.feed("down", 100, 100, 100, True)
        self.tracker.feed("cancel", 110, 100, 120, False)
        self.assertFalse(self.tracker.feed("up", 100, 100, 150, True))
        self.assertFalse(self.click(200))
        self.tracker.feed("down", 100, 100, 300, True)
        self.assertFalse(self.tracker.feed("up", 100, 100, 1000, True))
        self.assertFalse(self.click(1100))

    def test_timestamp_wrap_and_negative_monitor_coordinates(self):
        self.assertFalse(self.click(0xFFFFFF00, -100, -20))
        self.assertTrue(self.click(10, -101, -20))

    def test_non_desktop_window_fails_before_remote_process_access(self):
        watcher = object.__new__(DesktopClickWatcher)
        watcher._class_name = lambda _: "OtherApp"
        self.assertFalse(watcher.blank_at(1, 1, 42))


class FavoriteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.ui = self.fixture.ui
        self.initial_saved = self.fixture.saved
        self.addCleanup(self.fixture.close)

    def test_real_heart_click_persists_then_unmarks_without_rebuild(self):
        ui, fixture = self.ui, self.fixture
        panel = ui.panels["a"]
        original = deepcopy(fixture.data)
        geometry = {key: p.geometry() for key, p in ui.panels.items()}
        QTest.mouseClick(panel.favorite_button, Qt.MouseButton.LeftButton)
        self.assertEqual(fixture.data["favorite_categories"], ["a"])
        self.assertEqual(panel.favorite_button.text(), "♥")
        self.assertEqual(fixture.saved, self.initial_saved + 1)
        ui.toggle_favorite_focus()
        self.assertTrue(panel.isVisible())
        self.assertFalse(ui.panels["b"].isVisible())
        self.assertIsNone(ui.panel_at(ui.panels["b"].geometry().center()))
        QTest.mouseClick(panel.favorite_button, Qt.MouseButton.LeftButton)
        self.assertFalse(panel.isVisible())
        ui.toggle_favorite_focus()
        self.assertTrue(all(p.isVisible() for p in ui.panels.values()))
        self.assertIs(ui.panels["a"], panel)
        self.assertEqual(geometry, {key: p.geometry() for key, p in ui.panels.items()})
        fixture.data.pop("favorite_categories")
        self.assertEqual(fixture.data, original)

    def test_no_favorites_and_all_favorites_restore_without_saving_switch(self):
        self.ui.toggle_favorite_focus()
        self.assertFalse(any(p.isVisible() for p in self.ui.panels.values()))
        self.ui.toggle_favorite_focus()
        self.assertTrue(all(p.isVisible() for p in self.ui.panels.values()))
        self.assertEqual(self.fixture.saved, self.initial_saved)
        self.ui.toggle_favorite("a")
        self.ui.toggle_favorite("b")
        self.ui.toggle_favorite_focus()
        self.assertTrue(all(p.isVisible() for p in self.ui.panels.values()))

    def test_render_tick_mode_rebuild_and_new_content_preserve_focus(self):
        self.ui.toggle_favorite("a")
        self.ui.toggle_favorite_focus()
        self.fixture.data["category_modes"]["b"] = "compact"
        self.ui.render("b")
        self.ui.render()
        self.ui.tick()
        self.assertFalse(self.ui.panels["b"].isVisible())
        self.assertTrue(self.ui.panels["a"].isVisible())
        self.assertEqual(self.ui.shell.hidden_panel_windows, {int(self.ui.panels["b"].winId())})

    def test_settings_cancel_preserves_heart_and_focus(self):
        self.ui.settings()
        dialog = self.ui._settings_dialog
        self.ui.toggle_favorite("a")
        self.ui.toggle_favorite_focus()
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        self.fixture.app.processEvents()
        self.assertEqual(self.fixture.data["favorite_categories"], ["a"])
        self.assertFalse(self.ui.panels["b"].isVisible())

    def test_favorite_store_roundtrip_and_default_for_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "layout.json"
            config.write_text(json.dumps({"version": 8, "ui": {}}), encoding="utf-8")
            with patch("smart_desktop.DATA_DIR", Path(directory)), patch("smart_desktop.CONFIG_PATH", config):
                store = Store()
                self.assertEqual(store.data["favorite_categories"], [])
                store.data["favorite_categories"] = ["a"]
                store.save()
                self.assertEqual(Store().data["favorite_categories"], ["a"])

    def test_shell_recovery_respects_intentional_hiding_then_restores_normally(self):
        shell = test_window_layout.DesktopLifecycleTests().make_shell()
        shell.hidden_panel_windows = {100}
        shell.user32.IsWindowVisible.return_value = False
        shell.keep_panels_above_desktop_backdrop()
        shell.user32.ShowWindow.assert_not_called()
        shell.hidden_panel_windows.clear()
        shell.keep_panels_above_desktop_backdrop()
        shell.user32.ShowWindow.assert_called_once_with(101, 4)


if __name__ == "__main__":
    unittest.main()
