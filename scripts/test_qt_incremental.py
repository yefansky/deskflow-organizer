"""Real offscreen Qt controls; fake Shell never touches the user's desktop."""
from copy import deepcopy
import queue
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QSpinBox
from PySide6.QtTest import QTest

from qt_desktop import QtOrganizerUI


def entry(name):
    return {"name": name + ".txt", "path": "G:/isolated-deskflow/" + name + ".txt",
            "kind": "txt", "modified": "2026-10-24 01:00", "manual": False}


class Fixture:
    def __init__(self, ui_type=QtOrganizerUI, count=8):
        self.app = QApplication.instance() or QApplication([])
        self.groups = {"a": [entry(str(i)) for i in range(count)], "b": [entry("other")]}
        self.saved = 0
        self.scanned = 0
        self.categories = [SimpleNamespace(id=key, title=key, color="#6ba6ef", mode="list") for key in self.groups]
        self.data = {"ui": {"background": "#101827", "alpha": .22}, "category_modes": {}, "tip_summaries": {}}
        def scan():
            self.scanned += 1
            return deepcopy(self.groups)
        def save():
            self.saved += 1
        model = SimpleNamespace(store=SimpleNamespace(data=self.data, save=save, lock=threading.RLock()), scan=scan,
                                sorted_items=lambda _category, items: list(items), desktop_signature=lambda: ())
        shell = SimpleNamespace(reconcile_target_monitor=lambda _ui: False, host=1, panel_windows={},
                                _find_overlay_host=lambda: 1,
                                target_monitor=lambda _target: {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                                attach=lambda _hwnd: True, panel_is_desktop_child=lambda _hwnd: True,
                                panel_is_visible=lambda _hwnd: True, keep_panels_above_desktop_backdrop=lambda: False,
                                hide_icons=lambda _paths: None, restore_icons=lambda: None)
        shell.retry_icon_attributes = lambda: None
        shell.monitors = lambda: []
        shell.remember_target_monitor = lambda ui, target: ui.update(target_monitor=target)
        with patch("qt_desktop.DesktopClickWatcher", return_value=Mock(poll=lambda: 0)):
            self.ui = ui_type(model, queue.Queue(), shell, lambda _store: self.categories)

    def close(self):
        self.ui.focus_timer.stop()
        self.ui.desktop_clicks.close()
        self.ui.tick_timer.stop()
        self.ui.native_timer.stop()
        if hasattr(self.ui, "icon_timer"):
            self.ui.icon_timer.stop()
            self.ui._icon_stop.set()
        self.ui._destroy_all()
        self.app.processEvents()


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.ui = self.fixture.ui

    def tearDown(self):
        self.fixture.close()

    def test_no_change_metadata_order_move_rename_delete(self):
        a, b = self.ui.panels["a"], self.ui.panels["b"]
        originals = list(a.item_widgets)
        self.ui.select_item(originals[2])
        self.ui.render()
        self.assertIs(self.ui.panels["a"], a)
        self.assertEqual(a.item_widgets, originals)
        self.assertIs(self.ui.selected_item, originals[2])
        self.fixture.data["tip_summaries"]["0.txt"] = {"summary": "changed summary"}
        self.ui.render()
        self.assertIn("changed summary", originals[0].tip_text)
        self.assertEqual(a.item_widgets, originals)
        self.fixture.groups["a"].reverse()
        self.ui.render("a")
        self.assertEqual(a.item_widgets, list(reversed(originals)))
        moved = self.fixture.groups["a"].pop()
        self.fixture.groups["b"].append(moved)
        self.ui.render()
        self.assertIs(self.ui.panels["b"], b)
        self.assertEqual(a.count_label.text(), "7")
        self.assertEqual(b.count_label.text(), "2")
        self.assertIs(a.item_widgets[0], originals[-1])
        self.fixture.groups["a"][0] = entry("renamed")
        self.ui.render("a")
        self.assertEqual(a.item_widgets[0].item["name"], "renamed.txt")
        self.assertIs(a.item_widgets[1], originals[-2])
        self.fixture.groups["a"] = [item for item in self.fixture.groups["a"] if item["name"] != "2.txt"]
        self.ui.render("a")
        self.assertIsNone(self.ui.selected_item)
        self.assertEqual(a.count_label.text(), "6")

    def test_preview_reuses_scan_and_cancel_values_can_restore(self):
        scans = self.fixture.scanned
        original = deepcopy(self.fixture.data["ui"])
        self.ui.preview_settings_layout({"background": "#202030"})
        self.assertEqual(self.fixture.scanned, scans)
        self.fixture.data["ui"] = original
        self.ui._settings_preview = True
        self.ui.render()
        self.ui._settings_preview = False
        self.assertEqual(self.fixture.scanned, scans)

    def test_stale_icon_result_does_not_overwrite_new_version(self):
        path = self.fixture.groups["a"][0]["path"]
        key = (path, 22)
        self.ui.invalidate_icon(path)
        self.ui._icon_results.put((key, 0, QImage(1, 1, QImage.Format.Format_RGB32), time.perf_counter()))
        self.ui._apply_icons()
        self.assertNotIn(key, self.ui.icon_cache)

    def test_mode_font_color_opacity_have_correct_rebuild_boundary(self):
        a, b = self.ui.panels["a"], self.ui.panels["b"]
        self.fixture.data["ui"]["alpha"] = .4
        self.ui.render()
        self.assertIs(self.ui.panels["a"], a)
        self.assertEqual(a.plate_opacity, .4)
        self.fixture.data["category_modes"]["a"] = "cards"
        self.ui.render("a")
        self.assertIsNot(self.ui.panels["a"], a)
        self.assertIs(self.ui.panels["b"], b)
        self.assertTrue(all(item.mode == "cards" for item in self.ui.panels["a"].item_widgets))
        self.ui.preview_settings_layout({"font_size": 13, "background": "#303040"})
        self.assertIsNot(self.ui.panels["b"], b)
        self.assertEqual(self.ui.panels["b"].item_widgets[0].name_label.font().pointSize(), 13)

    def test_same_minute_revision_invalidates_only_changed_icon(self):
        a = self.ui.panels["a"]
        first, second = a.item_widgets[:2]
        groups = deepcopy(self.fixture.groups)
        groups["a"][0]["revision"] = "123:456"
        before = self.ui.snapshot_groups(self.fixture.groups)
        after = self.ui.snapshot_groups(groups)
        self.assertNotEqual(before["a"], after["a"])
        self.assertEqual(before["b"], after["b"])
        self.ui.render("a", groups=groups)
        self.assertIs(a.item_widgets[0], first)
        self.assertIs(a.item_widgets[1], second)
        self.assertEqual(self.ui._icon_versions[first.item["path"]], 1)
        self.assertNotIn(second.item["path"], self.ui._icon_versions)

    def test_content_growth_does_not_resize_or_move_any_existing_panel(self):
        a, b = self.ui.panels["a"], self.ui.panels["b"]
        original_a, original_b = a.geometry(), b.geometry()
        survivor = b.item_widgets[0]
        self.fixture.groups["a"].extend(entry(f"new-{i}") for i in range(50))
        with patch.object(a, "setGeometry", wraps=a.setGeometry) as move_a, patch.object(b, "setGeometry", wraps=b.setGeometry) as move_b:
            self.ui.render()
            move_a.assert_not_called()
            move_b.assert_not_called()
        self.assertEqual(a.geometry(), original_a)
        self.assertEqual(b.geometry(), original_b)
        self.assertIs(self.ui.panels["a"], a)
        self.assertIs(b.item_widgets[0], survivor)
        self.assertEqual(a.count_label.text(), "58")

    def test_blocked_shell_worker_does_not_block_ui_heartbeat(self):
        from win32com.shell import shell
        started, release = threading.Event(), threading.Event()
        real_lookup = shell.SHGetFileInfo
        def delayed(*args):
            started.set()
            release.wait(2)
            return real_lookup(*args)
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(True))
        timer.start(5)
        try:
            with patch.object(shell, "SHGetFileInfo", delayed):
                self.ui.icon_for("G:/isolated-deskflow/new-path.txt", 29)
                QTest.qWait(50)
                self.assertTrue(started.is_set())
                self.assertGreater(len(beats), 0)
                self.assertFalse(release.is_set())
                release.set()
        finally:
            release.set()
            timer.stop()

    def test_settings_reopen_cancel_stops_queued_preview_and_rolls_back(self):
        self.ui.tick_timer.stop(); self.ui.native_timer.stop()
        original = deepcopy(self.fixture.data["ui"])
        before_saves = self.fixture.saved
        self.ui.settings()
        dialog = self.ui._settings_dialog
        self.ui.settings()
        self.assertIs(self.ui._settings_dialog, dialog)
        density, font = dialog.findChildren(QSpinBox)
        density.setValue(60); font.setValue(14)
        dialog.reject()
        QTest.qWait(160)
        self.assertIsNone(self.ui._settings_dialog)
        self.assertEqual(self.fixture.data["ui"], original)
        self.assertEqual(self.fixture.saved, before_saves)
        self.ui.settings()
        self.assertIsNot(self.ui._settings_dialog, dialog)
        self.ui._settings_dialog.reject()

    def test_save_before_debounce_uses_latest_values_and_no_late_preview(self):
        self.ui.tick_timer.stop(); self.ui.native_timer.stop()
        before_saves = self.fixture.saved
        self.ui.settings()
        dialog = self.ui._settings_dialog
        density, font = dialog.findChildren(QSpinBox)
        density.setValue(61); font.setValue(12)
        buttons = dialog.findChild(QDialogButtonBox)
        buttons.button(QDialogButtonBox.StandardButton.Save).click()
        QTest.qWait(160)
        self.assertIsNone(self.ui._settings_dialog)
        self.assertEqual(self.fixture.data["ui"]["cell_size"], 61)
        self.assertEqual(self.fixture.data["ui"]["font_size"], 12)
        self.assertGreater(self.fixture.saved, before_saves)

    def test_assignment_during_settings_is_refreshed_after_cancel(self):
        self.ui.tick_timer.stop(); self.ui.native_timer.stop()
        self.ui.settings()
        self.fixture.groups["a"].append(entry("arrived-while-open"))
        self.ui.events.put({"type": "assign", "categories": ["a"]})
        self.ui.tick()
        self.assertEqual(self.ui.panels["a"].count_label.text(), "8")
        self.ui._settings_dialog.reject()
        self.assertEqual(self.ui.panels["a"].count_label.text(), "9")


if __name__ == "__main__":
    unittest.main()
