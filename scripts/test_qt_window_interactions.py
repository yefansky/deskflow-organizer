"""Isolated Qt interaction checks using synthetic names and no Explorer changes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from types import SimpleNamespace
import queue
import unittest
import threading
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QIcon, QPixmap, QColor, QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox
from qt_desktop import (QtOrganizerUI, PanelWindow, HeaderButton, CHILD_GEOMETRY_SIZE_TOLERANCE,
                        _child_geometry_matches)


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
        self.assertEqual(QRect(*a.setGeometry.call_args.args), original)
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

    def test_embedded_geometry_uses_current_QWindow_not_stale_QWidget_origin(self):
        from PySide6.QtCore import QSize
        handle = SimpleNamespace(mapToGlobal=lambda _: QPoint(-650, 150))
        panel = SimpleNamespace(_desktop_parent=object(), windowHandle=lambda: handle,
                                size=lambda: QSize(280, 230), mapToGlobal=lambda _: QPoint(50, 60))
        self.assertEqual(PanelWindow.screen_geometry(panel), QRect(-650, 150, 280, 230))

    def test_guard_hidden_child_restore_preserves_Qt_state_without_owner_z_change(self):
        handle = SimpleNamespace(isVisible=lambda: True, setVisible=Mock())
        shell = SimpleNamespace(desktop_host_is_safe=lambda: True, panel_is_visible=lambda _: False,
                                show_native_child=Mock(return_value=True))
        panel = SimpleNamespace(_desktop_parent=object(), windowHandle=lambda: handle,
                                controller=SimpleNamespace(shell=shell), winId=lambda: 123)
        PanelWindow.setVisible(panel, True)
        shell.show_native_child.assert_called_once_with(123)
        handle.setVisible.assert_not_called()

    def test_unsafe_host_hides_native_child_and_restores_icons_without_hiding_Qt(self):
        handle = SimpleNamespace(isVisible=lambda: True, setVisible=Mock())
        shell = SimpleNamespace(desktop_host_is_safe=lambda: False, panel_is_visible=lambda _: True,
                                hide_native_child=Mock(), restore_icons=Mock(), desktop_available=True)
        panel = SimpleNamespace(_desktop_parent=object(), windowHandle=lambda: handle,
                                controller=SimpleNamespace(shell=shell), winId=lambda: 123)
        PanelWindow.setVisible(panel, True)
        shell.hide_native_child.assert_called_once_with(123)
        shell.restore_icons.assert_called_once_with()
        self.assertFalse(shell.desktop_available)
        handle.setVisible.assert_not_called()

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


HOST_ORIGIN = QPoint(1000, 40)


class HostOriginTests(unittest.TestCase):
    """A desktop child reports host-relative coordinates, so every product-side
    geometry decision must stay in screen space (persist, collision, hit test,
    move). The host here is deliberately not at the screen origin."""

    class Panel:
        def __init__(self, category, screen_rect):
            self.category = SimpleNamespace(id=category)
            self._rect = QRect(screen_rect)

        def screen_geometry(self):
            return QRect(self._rect)

        def set_screen_geometry(self, rect):
            self._rect = QRect(rect)

        def move_screen(self, point):
            self.set_screen_geometry(QRect(point, self._rect.size()))

        def width(self):
            return self._rect.width()

        def height(self):
            return self._rect.height()

        def x(self):
            return self._rect.x() - HOST_ORIGIN.x()  # host-local on purpose

        def y(self):
            return self._rect.y() - HOST_ORIGIN.y()

        def pos(self):
            return QPoint(self.x(), self.y())

    def make_ui(self):
        ui = object.__new__(QtOrganizerUI)
        ui.model = SimpleNamespace(store=SimpleNamespace(
            data={"ui": {"layout_profiles": {}}, "custom_blocks": [], "category_modes": {},
                  "category_sorting": {}, "item_orders": {}}, save=Mock(), lock=threading.RLock()),
            scan=Mock(return_value={}))
        ui.shell = SimpleNamespace(
            target_monitor=lambda _: {"left": -1000, "top": 0, "right": 1000, "bottom": 900},
            keep_panels_above_desktop_backdrop=Mock())
        ui.panels = {}
        ui.active_profile = {"auto_layout": True, "panel_positions": {}, "panel_sizes": {}}
        ui.focus_favorites = False
        return ui

    def test_persisted_position_is_screen_space_not_host_local(self):
        ui = self.make_ui()
        panel = self.Panel("a", QRect(-600, 100, 200, 160))
        ui.panels = {"a": panel}
        self.assertTrue(ui.finish_panel_geometry("a"))
        self.assertEqual(ui.active_profile["panel_positions"]["a"], "+-600+100")
        self.assertEqual(ui.active_profile["panel_sizes"]["a"], {"width": 200, "height": 160})

    def test_collision_rejects_and_rolls_back_in_screen_space(self):
        ui = self.make_ui()
        panel = self.Panel("a", QRect(-600, 100, 200, 160))
        neighbour = self.Panel("b", QRect(-380, 100, 200, 160))
        ui.panels = {"a": panel, "b": neighbour}
        ui.move_panel("a", -370, 100)  # overlapping b in screen space
        self.assertEqual(panel.screen_geometry(), QRect(-600, 100, 200, 160))
        self.assertEqual(neighbour.screen_geometry(), QRect(-380, 100, 200, 160))
        self.assertEqual(ui.active_profile["panel_positions"], {})

    def test_move_and_hit_test_use_screen_space_on_off_origin_host(self):
        ui = self.make_ui()
        panel = self.Panel("a", QRect(-600, 100, 200, 160))
        ui.panels = {"a": panel}
        ui.move_panel("a", -500, 120)
        self.assertEqual(panel.screen_geometry(), QRect(-500, 120, 200, 160))
        self.assertEqual(ui.active_profile["panel_positions"]["a"], "+-500+120")
        self.assertEqual(ui.panel_at(QPoint(-400, 200)), "a")
        self.assertIsNone(ui.panel_at(QPoint(-400, 20)))

    def test_offscreen_child_is_rejected_even_when_host_local_looks_valid(self):
        ui = self.make_ui()
        panel = self.Panel("a", QRect(-600, 100, 200, 160))
        ui.panels = {"a": panel}
        # Host-local (-190, 60) looks on-screen, but the screen rectangle
        # (810..1010) overflows the 1000px monitor edge and must be rejected.
        panel.set_screen_geometry(QRect(810, 100, 200, 160))
        self.assertFalse(ui.finish_panel_geometry("a"))


class WinIdRebuildTests(unittest.TestCase):
    """A Qt-recreated HWND must not desynchronise focus-mode hiding or Z order."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_panel(self, wanted, old_hwnd=100, new_hwnd=555):
        shell = SimpleNamespace(
            panel_windows={old_hwnd: old_hwnd},
            hidden_panel_windows=set() if wanted else {old_hwnd},
            hide_native_child=Mock(), restore_icons=Mock())
        def attach(hwnd):
            shell.panel_windows[hwnd] = hwnd  # mirrors DesktopShell.attach_native_child
            return True
        shell.attach_native_child = Mock(side_effect=attach)
        noop = lambda *args: None
        controller = SimpleNamespace(
            shell=shell, panel_wanted=lambda _category: wanted, events=queue.Queue(),
            model=SimpleNamespace(store=SimpleNamespace(data={"ui": {}})),
            font_size=lambda: 9, cell_size=lambda: 48, row_height=lambda: 34,
            icon_grid_cell_height=lambda: 72, icon_grid_icon_size=lambda: 40,
            icon_for=lambda *args: QIcon(QPixmap(8, 8)), item_tip=lambda item: "合成摘要",
            show_panel_menu=noop, show_sort_menu=noop, cycle_mode=noop,
            arrange_top_right=noop, settings=noop, close=noop)
        category = SimpleNamespace(id="sample", title="合成分类", color="#55bbaa")
        items = [{"name": "合成条目.txt", "path": "G:/synthetic/one.txt", "kind": "file",
                  "modified": "2026-10-24"}]
        panel = PanelWindow(controller, category, items, "list", "#101827", .7)
        panel._desktop_parent = SimpleNamespace()  # already embedded in a desktop host
        panel._registered_hwnd = old_hwnd
        return panel, shell, new_hwnd

    def test_focus_hidden_block_stays_hidden_after_hwnd_rebuild(self):
        panel, shell, new = self.make_panel(wanted=False)
        panel._reattach_after_win_id_change(new)
        self.assertNotIn(100, shell.hidden_panel_windows)
        self.assertEqual(shell.hidden_panel_windows, {new})
        self.assertNotIn(100, shell.panel_windows)
        self.assertEqual(shell.panel_windows, {new: new})
        shell.hide_native_child.assert_called_once_with(new)
        shell.restore_icons.assert_not_called()
        self.assertEqual(panel._registered_hwnd, new)

    def test_visible_block_is_registered_and_not_marked_hidden(self):
        panel, shell, new = self.make_panel(wanted=True)
        panel._reattach_after_win_id_change(new)
        self.assertEqual(shell.hidden_panel_windows, set())
        self.assertEqual(shell.panel_windows, {new: new})
        shell.hide_native_child.assert_not_called()
        shell.restore_icons.assert_not_called()

    def test_failed_reattach_fails_closed_and_requests_render(self):
        panel, shell, new = self.make_panel(wanted=False)
        shell.attach_native_child.side_effect = lambda _hwnd: False
        panel._reattach_after_win_id_change(new)
        self.assertEqual(shell.hidden_panel_windows, set())
        shell.restore_icons.assert_called_once()
        self.assertIsNone(panel._appearance)
        self.assertEqual(panel.controller.events.get_nowait(), {"type": "render"})


class ChildGeometryTests(unittest.TestCase):
    """Real-desktop evidence: show() adjusted the child height 86 -> 91 at the
    exact same origin, which used to fail the whole layer closed."""

    EXPECTED = QRect(2246, 531, 304, 86)  # synthetic, matches the reported shape

    def make_panel(self, actual):
        shell = SimpleNamespace(host=131620, hide_native_child=Mock(),
                                desktop_available=True, renderer_error="")
        panel = SimpleNamespace(controller=SimpleNamespace(shell=shell),
                                screen_geometry=lambda: QRect(actual),
                                winId=lambda: 4242)
        return panel, shell

    def test_show_time_height_growth_is_accepted(self):
        panel, shell = self.make_panel(QRect(2246, 531, 304, 91))  # +5 height
        self.assertTrue(PanelWindow._confirm_child_geometry(panel, self.EXPECTED))
        shell.hide_native_child.assert_not_called()
        self.assertTrue(shell.desktop_available)
        self.assertFalse(hasattr(shell, "_rejected_host"))
        self.assertEqual(shell.renderer_error, "")

    def test_show_time_width_growth_is_accepted(self):
        panel, shell = self.make_panel(QRect(2246, 531, 309, 91))  # +5 width, +5 height
        self.assertTrue(PanelWindow._confirm_child_geometry(panel, self.EXPECTED))
        shell.hide_native_child.assert_not_called()
        self.assertTrue(shell.desktop_available)

    def test_position_drift_still_fails_closed(self):
        panel, shell = self.make_panel(QRect(2247, 531, 304, 86))
        self.assertFalse(PanelWindow._confirm_child_geometry(panel, self.EXPECTED))
        shell.hide_native_child.assert_called_once_with(4242)
        self.assertFalse(shell.desktop_available)
        self.assertEqual(shell._rejected_host, 131620)
        self.assertIn("坐标", shell.renderer_error)

    def test_size_deviation_beyond_tolerance_fails_closed(self):
        panel, shell = self.make_panel(QRect(2246, 531, 304, 95))  # +9 height
        self.assertFalse(PanelWindow._confirm_child_geometry(panel, self.EXPECTED))
        shell.hide_native_child.assert_called_once_with(4242)
        self.assertFalse(shell.desktop_available)

    def test_tolerance_boundary_is_inclusive(self):
        for width, height, accepted in ((312, 94, True), (313, 94, False),
                                        (312, 95, False), (296, 78, True), (295, 78, False)):
            panel, shell = self.make_panel(QRect(2246, 531, width, height))
            self.assertEqual(PanelWindow._confirm_child_geometry(panel, self.EXPECTED), accepted,
                             f"width={width} height={height}")
            if accepted:
                shell.hide_native_child.assert_not_called()
            else:
                shell.hide_native_child.assert_called_once_with(4242)
        self.assertEqual(CHILD_GEOMETRY_SIZE_TOLERANCE, 8)
        self.assertTrue(_child_geometry_matches(self.EXPECTED, QRect(2246, 531, 312, 94)))
        self.assertFalse(_child_geometry_matches(self.EXPECTED, QRect(2246, 531, 313, 94)))


if __name__ == "__main__":
    unittest.main()

