"""Desktop lifecycle and user layout regressions; never touches live Explorer."""
import unittest
from unittest.mock import Mock

from layout_engine import Rect, can_place_rectangle, restore_rectangles
from smart_desktop import DesktopShell


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
        shell.panel_windows = {100: 101}
        shell.user32 = Mock()
        shell.user32.GetAncestor.return_value = 20
        shell.user32.GetWindow.return_value = 20
        shell.user32.IsWindow.return_value = True
        shell.user32.IsWindowVisible.return_value = True
        shell.user32.GetWindowLongW.return_value = 0x08000000
        shell._find_overlay_host = Mock(return_value=10)
        shell.attach = Mock(return_value=True)
        shell.restore_icons = Mock()
        return shell

    def test_host_replacement_reattaches_existing_window(self):
        shell = self.make_shell()
        shell._find_overlay_host.return_value = 11
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        shell.attach.assert_called_once_with(100)
        shell.restore_icons.assert_not_called()

    def test_missing_explorer_hides_popup_and_restores_icons_once(self):
        shell = self.make_shell()
        shell._find_overlay_host.return_value = 0
        shell.keep_panels_above_desktop_backdrop()
        shell.keep_panels_above_desktop_backdrop()
        shell.user32.ShowWindow.assert_called_with(101, 0)
        shell.restore_icons.assert_called_once()

    def test_lost_visibility_restored_without_activation(self):
        shell = self.make_shell()
        shell.user32.IsWindowVisible.return_value = False
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        shell.user32.ShowWindow.assert_called_once_with(101, 4)

    def test_topmost_is_removed_before_returning_to_desktop_band(self):
        shell = self.make_shell()
        shell.user32.GetWindowLongW.return_value = 0x08000008
        shell.keep_panels_above_desktop_backdrop()
        calls = shell.user32.SetWindowPos.call_args_list
        self.assertEqual([call.args[1] for call in calls], [-2, 20])
        self.assertTrue(all(call.args[-1] & 0x10 for call in calls))

    def test_failed_owner_repair_hides_popup_and_restores_icons(self):
        shell = self.make_shell()
        shell.user32.GetWindow.return_value = 99
        shell.attach.return_value = False
        shell.keep_panels_above_desktop_backdrop()
        shell.user32.ShowWindow.assert_called_once_with(101, 0)
        shell.restore_icons.assert_called_once()

    def test_missing_host_failed_attach_then_success_retains_recovery_path(self):
        shell = self.make_shell()
        shell._find_overlay_host.side_effect = [0, 11, 11]
        shell.user32.GetWindow.return_value = 99
        shell.attach.side_effect = [False, True]
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertEqual(shell.attach.call_count, 2)
        self.assertEqual(shell.panel_windows, {100: 101})
        self.assertEqual(shell.host, 11)

    def test_invalid_native_window_restores_original_icons_before_rebuild(self):
        shell = self.make_shell()
        shell.user32.IsWindow.return_value = False
        self.assertTrue(shell.keep_panels_above_desktop_backdrop())
        self.assertEqual(shell.panel_windows, {})
        shell.restore_icons.assert_called_once()


if __name__ == "__main__":
    unittest.main()
