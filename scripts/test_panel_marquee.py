"""Isolated regressions for desktop panel selection and grouped placement."""
from copy import deepcopy
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, QPointF, QEvent, Qt
from PySide6.QtGui import QMouseEvent
from test_qt_incremental import Fixture


class PanelMarqueeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.ui = self.fixture.ui
        self.ui.desktop_clicks.user = Mock()
        self.ui.desktop_clicks.user.IsWindow.return_value = True
        self.ui.desktop_clicks.window_rect.side_effect = lambda hwnd: {
            int(p.winId()): (p.x(), p.y(), p.x() + p.width(), p.y() + p.height())
            for p in self.ui.panels.values()
        }[hwnd]

    def test_reverse_partial_intersection_and_blank_click(self):
        ui = self.ui
        a, b = ui.panels["a"], ui.panels["b"]
        left = min(a.x(), b.x()) + 1
        top = min(a.y(), b.y()) + 1
        right = max(a.x() + a.width(), b.x() + b.width()) - 1
        bottom = max(a.y() + a.height(), b.y() + b.height()) - 1
        ui.desktop_pointer_event("down", right, bottom, 42, True)
        ui.desktop_pointer_event("drag", left, top, 42, False)
        ui.desktop_pointer_event("up", left, top, 0, False)
        self.assertEqual(ui.selected_panels, {"a", "b"})
        ui.desktop_pointer_event("down", 0, 0, 42, True)
        ui.desktop_pointer_event("up", 0, 0, 42, True)
        self.assertEqual(ui.selected_panels, set())

    def test_nonblank_start_and_cancel_do_not_select(self):
        ui = self.ui
        ui.desktop_pointer_event("down", 0, 0, 1, False)
        ui.desktop_pointer_event("drag", 1900, 1000, 1, False)
        self.assertEqual(ui.selected_panels, set())
        ui.desktop_pointer_event("down", 0, 0, 1, True)
        ui.desktop_pointer_event("drag", 1900, 1000, 1, False)
        ui.desktop_pointer_event("cancel", 0, 0, 1, False)
        self.assertIsNone(ui._marquee_start)
        self.assertEqual(ui.selected_panels, set())

    def test_group_persist_once_and_invalid_rollback(self):
        ui = self.ui
        original = {name: QPoint(p.pos()) for name, p in ui.panels.items()}
        before = self.fixture.saved
        for panel in ui.panels.values():
            panel.move(panel.pos() + QPoint(-10, 0))
        self.assertTrue(ui.finish_panel_group(original))
        self.assertEqual(self.fixture.saved, before + 1)

    def test_header_drag_moves_every_selected_panel(self):
        ui = self.ui
        ui.set_selected_panels({"a", "b"})
        before = self.fixture.saved
        original = {name: QPoint(p.pos()) for name, p in ui.panels.items()}
        header = ui.panels["a"].header
        def event(kind, x, button, buttons):
            return QMouseEvent(kind, QPointF(5, 5), QPointF(x, 100), button, buttons,
                               Qt.KeyboardModifier.NoModifier)
        header.mousePressEvent(event(QEvent.Type.MouseButtonPress, 100, Qt.MouseButton.LeftButton,
                                     Qt.MouseButton.LeftButton))
        header.mouseMoveEvent(event(QEvent.Type.MouseMove, 90, Qt.MouseButton.NoButton,
                                    Qt.MouseButton.LeftButton))
        header.mouseReleaseEvent(event(QEvent.Type.MouseButtonRelease, 90, Qt.MouseButton.LeftButton,
                                       Qt.MouseButton.NoButton))
        self.assertEqual({name: p.pos() for name, p in ui.panels.items()},
                         {name: pos + QPoint(-10, 0) for name, pos in original.items()})
        for name, panel in ui.panels.items():
            self.assertEqual(panel.pos(), original[name] + QPoint(-10, 0))
            self.assertEqual(ui.active_profile["panel_positions"][name], f"+{panel.x()}+{panel.y()}")
        moved = {name: QPoint(p.pos()) for name, p in ui.panels.items()}
        profile = deepcopy(ui.active_profile)
        ui.panels["a"].move(-100, 0)
        self.assertFalse(ui.finish_panel_group(moved))
        self.assertEqual({name: p.pos() for name, p in ui.panels.items()}, moved)
        self.assertEqual(ui.active_profile, profile)
        self.assertEqual(self.fixture.saved, before + 1)

    def test_unselected_collision_and_hidden_panel_exclusion(self):
        ui = self.ui
        original = {"a": QPoint(ui.panels["a"].pos())}
        ui.panels["a"].move(ui.panels["b"].pos())
        self.assertFalse(ui.finish_panel_group(original))
        ui.toggle_favorite("a")
        ui.toggle_favorite_focus()
        ui.desktop_pointer_event("down", 0, 0, 1, True)
        ui.desktop_pointer_event("drag", 1900, 1000, 1, False)
        ui.desktop_pointer_event("up", 1900, 1000, 1, False)
        self.assertEqual(ui.selected_panels, {"a"})

    def test_refresh_keeps_selection_but_mode_change_clears(self):
        ui = self.ui
        ui.set_selected_panels({"a", "b"})
        ui.render()
        self.assertEqual(ui.selected_panels, {"a", "b"})
        ui.toggle_favorite_focus()
        self.assertEqual(ui.selected_panels, set())

    def test_native_drop_uses_actual_press_and_excludes_blank(self):
        ui = self.ui
        ui.desktop_clicks.is_desktop_list.return_value = True
        ui.shell.left_button_down = Mock(return_value=True)
        ui.desktop_pointer_event("down", 10, 20, 42, True)
        ui.watch_native_drag()
        self.assertIsNone(ui.native_drag)
        ui.shell.left_button_down.return_value = False
        ui.desktop_pointer_event("up", 10, 20, 42, True)
        ui.watch_native_drag()
        ui.shell.left_button_down.return_value = True
        ui.desktop_pointer_event("down", 30, 40, 42, False)
        ui.watch_native_drag()
        self.assertEqual(ui.native_drag, {"x": 30, "y": 40, "labels": []})

    def test_physical_bounds_reject_even_if_logical_position_appears_inside(self):
        ui = self.ui
        old = {name: QPoint(p.pos()) for name, p in ui.panels.items()}
        ui.desktop_clicks.window_rect.side_effect = None
        ui.desktop_clicks.window_rect.return_value = (1800, 100, 2000, 300)
        self.assertFalse(ui.finish_panel_group(old))
        self.assertEqual({name: p.pos() for name, p in ui.panels.items()}, old)

    def test_failed_hook_keeps_original_native_drag_sampling(self):
        ui = self.ui
        ui.desktop_clicks.listener_alive.return_value = False
        ui.shell.left_button_down = Mock(return_value=True)
        ui.shell.pointer_over_listview = Mock(return_value=True)
        ui.shell.cursor_position = Mock(return_value=(77, 88))
        ui.watch_native_drag()
        self.assertEqual(ui.native_drag, {"x": 77, "y": 88, "labels": []})
        self.assertIsNone(ui._marquee_start)


if __name__ == "__main__":
    unittest.main()
