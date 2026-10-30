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
import qt_desktop
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
        for profile in fixture.data["ui"]["layout_profiles"].values():
            profile.pop("mode_layouts", None)
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

    def test_independent_positions_survive_rebuild_and_return_to_focus(self):
        ui = self.ui
        ui.toggle_favorite("a")
        normal = {key: p.geometry() for key, p in ui.panels.items()}
        ui.toggle_favorite_focus()
        # The hidden block is not an obstacle in focus mode.
        target = normal["b"].topLeft()
        original = ui.panels["a"].pos()
        ui.panels["a"].move(target)
        self.assertTrue(ui.finish_panel_geometry("a", original))
        focused = ui.panels["a"].geometry()
        ui.render("a")
        self.assertEqual(ui.panels["a"].geometry(), focused)
        ui.toggle_favorite_focus()
        self.assertEqual({key: p.geometry() for key, p in ui.panels.items()}, normal)
        ui.toggle_favorite_focus()
        self.assertEqual(ui.panels["a"].geometry(), focused)

    def test_display_recovery_preserves_saved_focus_and_normal_layout(self):
        ui = self.ui
        ui.toggle_favorite("a")
        normal = {key: p.geometry() for key, p in ui.panels.items()}
        ui.toggle_favorite_focus()
        ui.panels["a"].move(400, 400)
        self.assertTrue(ui.finish_panel_geometry("a"))
        focused = ui.panels["a"].geometry()
        before = deepcopy(self.fixture.data["ui"]["layout_profiles"])
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 500,
                   "screen_left": 0, "screen_top": 0, "screen_right": 1920, "screen_bottom": 1080}
        ui.shell.target_monitor = lambda _target: monitor
        ui.tick()
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)
        self.assertLessEqual(ui.panels["a"].geometry().bottom(), 492)
        monitor["bottom"] = 1080
        ui.tick()
        self.assertEqual(ui.panels["a"].geometry(), focused)
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)
        ui.toggle_favorite_focus()
        self.assertEqual({key: p.geometry() for key, p in ui.panels.items()}, normal)

    def test_temporary_bounds_toggle_keeps_saved_focus_and_manual_move_updates_it(self):
        ui = self.ui
        ui.toggle_favorite("a")
        ui.toggle_favorite_focus()
        ui.panels["a"].move(400, 600)
        self.assertTrue(ui.finish_panel_geometry("a"))
        before = deepcopy(self.fixture.data["ui"]["layout_profiles"])
        ui.toggle_favorite_focus()
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 500,
                   "screen_left": 0, "screen_top": 0, "screen_right": 1920, "screen_bottom": 1080}
        ui.shell.target_monitor = lambda _target: monitor
        ui.tick()
        ui.toggle_favorite_focus()
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)
        ui.panels["a"].move(500, 100)
        self.assertTrue(ui.finish_panel_geometry("a"))
        profile = next(iter(self.fixture.data["ui"]["layout_profiles"].values()))
        self.assertEqual(profile["mode_layouts"]["favorites"]["panel_positions"]["a"], "+500+100")
        monitor["bottom"] = 1080
        ui.tick()
        self.assertEqual((ui.panels["a"].x(), ui.panels["a"].y()), (500, 100))

    def test_explicit_arrange_still_saves_during_display_recovery(self):
        ui = self.ui
        ui.toggle_favorite("a")
        ui.toggle_favorite_focus()
        ui.panels["a"].move(400, 400)
        self.assertTrue(ui.finish_panel_geometry("a"))
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 800,
                   "screen_left": 0, "screen_top": 0, "screen_right": 1920, "screen_bottom": 1080}
        ui.shell.target_monitor = lambda _target: monitor
        ui.events.put({"type": "arrange"})
        ui.tick()
        profile = next(iter(self.fixture.data["ui"]["layout_profiles"].values()))
        self.assertNotEqual(profile["mode_layouts"]["favorites"]["panel_positions"]["a"], "+400+400")

    def test_saved_focus_layout_survives_store_reload(self):
        ui = self.ui
        ui.toggle_favorite("a")
        ui.toggle_favorite_focus()
        ui.panels["a"].move(400, 400)
        self.assertTrue(ui.finish_panel_geometry("a"))
        expected = ui.panels["a"].geometry()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "layout.json"
            with patch("smart_desktop.DATA_DIR", Path(directory)), patch("smart_desktop.CONFIG_PATH", config):
                store = Store()
                store.data.update(deepcopy(self.fixture.data))
                store.save()
                loaded = Store()
                ui.model.store.data = loaded.data
                ui.focus_favorites = False
                ui._destroy_all()
                ui.render()
                ui.toggle_favorite_focus()
                self.assertEqual(ui.panels["a"].geometry(), expected)

    def test_switch_to_normal_in_temporary_bounds_is_visible_without_overwriting(self):
        ui = self.ui
        ui.panels["a"].move(400, 600)
        self.assertTrue(ui.finish_panel_geometry("a"))
        normal = ui.panels["a"].geometry()
        ui.toggle_favorite("a")
        ui.toggle_favorite_focus()
        before = deepcopy(self.fixture.data["ui"]["layout_profiles"])
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 500,
                   "screen_left": 0, "screen_top": 0, "screen_right": 1920, "screen_bottom": 1080}
        ui.shell.target_monitor = lambda _target: monitor
        ui.tick()
        ui.toggle_favorite_focus()
        self.assertLessEqual(ui.panels["a"].geometry().bottom(), 492)
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)
        monitor["bottom"] = 1080
        ui.tick()
        self.assertEqual(ui.panels["a"].geometry(), normal)
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)

    def test_single_panel_rebuild_in_temporary_bounds_stays_visible(self):
        ui = self.ui
        ui.toggle_favorite("a")
        ui.toggle_favorite_focus()
        ui.panels["a"].move(400, 600)
        self.assertTrue(ui.finish_panel_geometry("a"))
        before = deepcopy(self.fixture.data["ui"]["layout_profiles"])
        monitor = {"left": 0, "top": 0, "right": 1920, "bottom": 500,
                   "screen_left": 0, "screen_top": 0, "screen_right": 1920, "screen_bottom": 1080}
        ui.shell.target_monitor = lambda _target: monitor
        ui.tick()
        self.fixture.data["category_modes"]["a"] = "compact"
        ui.render("a")
        self.assertLessEqual(ui.panels["a"].geometry().bottom(), 492)
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)
        monitor["bottom"] = 1080
        ui.tick()
        self.assertEqual((ui.panels["a"].x(), ui.panels["a"].y()), (400, 600))
        self.assertEqual(self.fixture.data["ui"]["layout_profiles"], before)

    def test_focus_collision_rejects_without_changing_normal_layout(self):
        ui = self.ui
        ui.toggle_favorite("a")
        ui.toggle_favorite("b")
        ui.toggle_favorite_focus()
        normal_profile = deepcopy(next(iter(self.fixture.data["ui"]["layout_profiles"].values())))
        original = ui.panels["a"].pos()
        ui.panels["a"].move(ui.panels["b"].pos())
        self.assertFalse(ui.finish_panel_geometry("a", original))
        self.assertEqual(ui.panels["a"].pos(), original)
        self.assertEqual(next(iter(self.fixture.data["ui"]["layout_profiles"].values())), normal_profile)

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
        # The structural fix keeps the maintenance tick read-only: it reports
        # that Qt must restore visibility instead of re-inserting the block
        # into the global Z order.
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        shell.user32.ShowWindow.assert_not_called()
        shell.user32.SetWindowPos.assert_not_called()

    def test_tick_restores_guard_hidden_child_through_Qt_only(self):
        ui, shell = self.ui, self.ui.shell
        panel = ui.panels["a"]
        hwnd = int(panel.winId())
        shell.keep_panels_above_desktop_backdrop = lambda: True
        shell.desktop_available = True
        shell.panel_windows = {int(p.winId()): int(p.winId()) for p in ui.panels.values()}
        shell.panel_is_visible = lambda handle: handle != hwnd
        shown = []
        panel.show = lambda: shown.append(int(panel.winId()))
        ui.tick()
        self.assertEqual(shown, [hwnd])

    def test_render_fail_closed_is_logged_once_without_window_titles(self):
        ui = self.ui
        ui.shell.desktop_host_is_safe = lambda: False
        with patch.object(qt_desktop.logger, "warning") as warning:
            ui.render()
            ui.render()
        fail_closed = [call for call in warning.call_args_list if "fail_closed" in call.args[0]]
        self.assertEqual(len(fail_closed), 1)
        message, host, reason = fail_closed[0].args
        self.assertEqual(host, 1)  # host handle only, never a window title
        self.assertEqual(reason, "host_band_or_identity_unsafe")
        self.assertEqual(message.count("%s"), 2)
        self.assertEqual(ui.panels, {})
        self.assertEqual(self.fixture.saved, self.initial_saved)


if __name__ == "__main__":
    unittest.main()
