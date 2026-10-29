"""Isolated Qt interaction checks using synthetic names and no Explorer changes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QIcon, QPixmap, QColor, QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox
from qt_desktop import QtOrganizerUI, PanelWindow, HeaderButton


class QtInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for font in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/seguisym.ttf"):
            if Path(font).exists(): QFontDatabase.addApplicationFont(font)

    def make_ui(self):
        ui = object.__new__(QtOrganizerUI)
        ui.model = SimpleNamespace(store=SimpleNamespace(
            data={"ui": {"layout_profiles": {}}, "custom_blocks": [{"id": "custom"}],
                  "category_modes": {}, "category_sorting": {}, "item_orders": {}}, save=Mock(), lock=threading.RLock()),
            scan=Mock(return_value={}))
        ui.shell = SimpleNamespace(keep_panels_above_desktop_backdrop=Mock(),
            target_monitor=lambda _: {"left": -1000, "top": 0, "right": 1000, "bottom": 900})
        ui.render = Mock()
        ui.panels = {}
        ui.active_profile = {"auto_layout": True, "panel_positions": {}, "panel_sizes": {}}
        return ui

    def test_empty_custom_removal_requires_confirmation(self):
        ui = self.make_ui()
        ui.model.store.data["favorite_categories"] = ["custom", "other"]
        with patch.object(ui, "confirm_empty_block", return_value=False):
            ui.delete_empty_block("custom")
        self.assertEqual(ui.model.store.data["custom_blocks"], [{"id": "custom"}])
        ui.model.store.save.assert_not_called()
        with patch.object(ui, "confirm_empty_block", return_value=True):
            ui.delete_empty_block("custom")
        self.assertEqual(ui.model.store.data["custom_blocks"], [])
        self.assertEqual(ui.model.store.data["favorite_categories"], ["other"])

    def test_builtin_removal_preserves_classification_and_explains_reappearance(self):
        ui = self.make_ui()
        with patch.object(ui, "confirm_empty_block", return_value=True) as confirm:
            ui.delete_empty_block("documents")
        confirm.assert_called_once_with(False)
        self.assertEqual(ui.model.store.data["ui"]["hidden_empty_categories"], ["documents"])
        self.assertEqual(ui.model.store.data["custom_blocks"], [{"id": "custom"}])

    def test_content_arriving_during_confirmation_prevents_removal(self):
        ui = self.make_ui()
        ui.model.scan.side_effect = [{}, {"custom": [{"name": "synthetic"}]}]
        with patch.object(ui, "confirm_empty_block", return_value=True), \
             patch.object(QMessageBox, "information"):
            ui.delete_empty_block("custom")
        ui.model.store.save.assert_not_called()

    def test_slow_empty_scan_leaves_store_lock_available_and_rechecks_new_assignment(self):
        ui = self.make_ui()
        scan_started, release_scan = threading.Event(), threading.Event()
        errors = []
        def slow_scan():
            self.assertFalse(ui.model.store.lock._is_owned())
            scan_started.set()
            if not release_scan.wait(3): raise AssertionError("scan barrier timeout")
            return {}  # old snapshot, before the concurrent assignment
        ui.model.scan = slow_scan
        def remove():
            try: ui._remove_confirmed_empty_block("custom", True)
            except BaseException as error: errors.append(error)
        with patch.object(QMessageBox, "information") as information:
            worker = threading.Thread(target=remove)
            worker.start()
            self.assertTrue(scan_started.wait(3))
            acquired = ui.model.store.lock.acquire(timeout=.5)
            try:
                self.assertTrue(acquired, "slow Shell scan blocked Store lock")
                ui.model.store.data["overrides"] = {"new.txt": "custom"}
            finally:
                if acquired: ui.model.store.lock.release()
                release_scan.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            information.assert_called_once()
        self.assertEqual(ui.model.store.data["custom_blocks"], [{"id": "custom"}])
        ui.model.store.save.assert_not_called()
        ui.render.assert_not_called()

    def test_scan_render_and_modal_notice_never_hold_store_lock(self):
        ui = self.make_ui()
        observed = []
        ui.model.scan = lambda: observed.append(("scan", ui.model.store.lock._is_owned())) or {}
        ui.render = lambda *args: observed.append(("render", ui.model.store.lock._is_owned()))
        ui._remove_confirmed_empty_block("custom", True)
        self.assertEqual(observed, [("scan", False), ("render", False)])
        ui.model.scan = lambda: {"documents": [{"name": "new"}]}
        with patch.object(QMessageBox, "information", side_effect=lambda *args:
                          observed.append(("modal", ui.model.store.lock._is_owned()))):
            ui._remove_confirmed_empty_block("documents", False)
        self.assertEqual(observed[-1], ("modal", False))

    def test_collision_restores_A_and_leaves_B_and_saved_state_untouched(self):
        ui = self.make_ui()
        a = SimpleNamespace(x=lambda: 280, y=lambda: 20, width=lambda: 200, height=lambda: 160, move=Mock())
        b = SimpleNamespace(x=lambda: 300, y=lambda: 20, width=lambda: 200, height=lambda: 160, move=Mock())
        ui.panels = {"a": a, "b": b}
        self.assertFalse(ui.finish_panel_geometry("a", QPoint(20, 20)))
        a.move.assert_called_once_with(QPoint(20, 20))
        b.move.assert_not_called()
        ui.model.store.save.assert_not_called()

    def test_resize_collision_restores_full_geometry(self):
        ui = self.make_ui()
        a = SimpleNamespace(x=lambda: 20, y=lambda: 20, width=lambda: 400, height=lambda: 300, setGeometry=Mock())
        b = SimpleNamespace(x=lambda: 300, y=lambda: 20, width=lambda: 200, height=lambda: 160, move=Mock())
        ui.panels = {"a": a, "b": b}
        original = QRect(20, 20, 200, 160)
        self.assertFalse(ui.finish_panel_geometry("a", original))
        a.setGeometry.assert_called_once_with(original)
        b.move.assert_not_called()

    def test_geometry_bounds_and_negative_screen_positions(self):
        from layout_engine import Rect
        ui = self.make_ui()
        self.assertTrue(ui.can_place_panel(Rect("a", -992, 8, 200, 160)))
        self.assertFalse(ui.can_place_panel(Rect("a", -993, 8, 200, 160)))
        self.assertFalse(ui.can_place_panel(Rect("a", 900, 8, 200, 160)))
        self.assertFalse(ui.can_place_panel(Rect("a", 8, 800, 200, 160)))
        self.assertEqual(ui._position_from_saved("+-900+8", (0, 0)), (-900, 8))
        self.assertEqual(ui._position_from_saved("-900+8", (0, 0)), (-900, 8))

    def test_hidden_builtin_reappears_when_content_arrives(self):
        ui = self.make_ui()
        category = SimpleNamespace(id="documents", mode="list", title="合成分类")
        ui.categories_provider = lambda _: [category]
        ui.model.store.data["ui"]["hidden_empty_categories"] = ["documents"]
        monitor = {"left": 0, "top": 0, "right": 1280, "bottom": 720}
        self.assertEqual(ui.auto_layout({}, monitor), {})
        self.assertIn("documents", ui.auto_layout({"documents": [{"name": "synthetic"}]}, monitor))

    def test_isolated_panel_modes_and_button_help(self):
        output = Path(__file__).resolve().parents[1] / "build" / "d13"
        output.mkdir(parents=True, exist_ok=True)
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("#55bbaa"))
        noop = lambda *args: None
        controller = SimpleNamespace(model=SimpleNamespace(store=SimpleNamespace(data={"ui": {}})),
            font_size=lambda: 9, cell_size=lambda: 48, row_height=lambda: 34,
            icon_grid_cell_height=lambda: 72, icon_grid_icon_size=lambda: 40,
            icon_for=lambda *args: QIcon(pixmap), item_tip=lambda item: "合成示例摘要，说明用途。",
            show_panel_menu=noop, show_sort_menu=noop, cycle_mode=noop,
            arrange_top_right=noop, settings=noop, close=noop)
        category = SimpleNamespace(id="sample", title="合成分类", color="#55bbaa")
        items = [{"name": f"合成条目 {index}", "path": f"G:/synthetic/{index}.txt",
                  "kind": "file", "modified": "2026-10-24"} for index in range(12)]
        for mode in ("list", "compact", "cards"):
            panel = PanelWindow(controller, category, items, mode, "#101827", .7)
            panel.setGeometry(0, 0, 420, 440)
            panel.show()
            self.app.processEvents()
            buttons = panel.findChildren(HeaderButton)
            self.assertEqual(len(buttons), 6)
            self.assertTrue(all(button.help_timer.interval() == 2000 and button.help_text for button in buttons))
            for width in (420, 310):
                panel.resize(width, 440)
                self.app.processEvents()
                for position, index in (("first", 0), ("middle", 4), ("last", 11)):
                    anchor = panel.item_widgets[index]
                    panel.tip_bubble.show_for(anchor, "合成示例摘要，说明用途。")
                    self.app.processEvents()
                    self.assertGreaterEqual(panel.tip_bubble.x(), 8)
                    self.assertLessEqual(panel.tip_bubble.geometry().right(), panel.width() - 8)
                    self.assertGreaterEqual(panel.tip_bubble.y(), 8)
                    panel.grab().save(str(output / f"synthetic-tip-{mode}-{width}-{position}.png"))
            panel.tip_bubble.show_for(buttons[0], buttons[0].help_text)
            panel.grab().save(str(output / f"synthetic-header-help-{mode}.png"))
            panel.close()
        dialog = QMessageBox(QMessageBox.Icon.Question, "确认移除空整理块",
            "移除这个空整理块；有新内容时会重新显示。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.button(QMessageBox.StandardButton.Yes).setText("移除")
        dialog.button(QMessageBox.StandardButton.No).setText("取消")
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.show()
        self.app.processEvents()
        dialog.grab().save(str(output / "synthetic-empty-confirmation.png"))
        dialog.close()


if __name__ == "__main__":
    unittest.main()
