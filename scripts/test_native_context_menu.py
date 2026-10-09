from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import win32gui

from native_context_menu import CUSTOM, FIRST, LAST, ShellMenu, show_file_menu, refresh_pointer


class NativeMenuTests(unittest.TestCase):
    def test_pointer_refresh_asks_hovered_window_for_its_cursor(self):
        with patch("win32gui.LoadCursor", return_value=42), patch("win32gui.SetCursor") as cursor, patch("win32gui.GetCursorPos", return_value=(200, 300)), patch("win32gui.WindowFromPoint", return_value=123) as hit, patch("win32gui.SendMessage") as message:
            refresh_pointer()
        cursor.assert_called_once_with(42)
        hit.assert_called_once_with((200, 300))
        self.assertEqual(123, message.call_args.args[0])
        self.assertEqual(0x20, message.call_args.args[1])

    def test_pointer_is_refreshed_after_cancel_and_failure(self):
        with patch("win32gui.TrackPopupMenu", return_value=0), patch("native_context_menu.refresh_pointer") as refresh:
            show_file_menu(str(self.path), 100, 100, [], lambda: None)
            refresh.assert_called_once()
        with patch("native_context_menu.refresh_pointer") as refresh:
            with self.assertRaises(OSError):
                show_file_menu(str(self.path.with_name("missing.rdp")), 100, 100, [], lambda: None)
            refresh.assert_called_once()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "远程连接.rdp"
        self.path.write_text("full address:s:127.0.0.1\n", encoding="utf-8")

    def test_real_rdp_menu_contains_edit_and_file_operations(self):
        menu = ShellMenu(str(self.path))
        self.addCleanup(menu.close)
        menu.build([("测试整理功能", lambda: None)])
        commands = [win32gui.GetMenuItemID(menu.menu, i) for i in range(win32gui.GetMenuItemCount(menu.menu))]
        verbs = {menu.verb(command) for command in commands}
        self.assertTrue({"edit", "connect", "properties", "rename", "delete", "copy", "cut"} <= verbs)
        self.assertGreaterEqual(menu.version, 2)
        self.assertEqual({CUSTOM}, set(menu.callbacks))
        self.assertGreater(CUSTOM, LAST)

    def test_folders_and_unicode_files_have_native_properties(self):
        folder = Path(self.temp.name) / "文件夹"
        folder.mkdir()
        document = folder / "说明.txt"
        document.write_text("test", encoding="utf-8")
        for path in [folder, document]:
            with self.subTest(path=path.name):
                menu = ShellMenu(str(path))
                try:
                    menu.build()
                    verbs = {menu.verb(win32gui.GetMenuItemID(menu.menu, i)) for i in range(win32gui.GetMenuItemCount(menu.menu))}
                    self.assertIn("properties", verbs)
                finally:
                    menu.close()

    def test_custom_callback_runs_after_native_menu_returns(self):
        calls = []
        callback = lambda: calls.append("called")
        with patch("win32gui.TrackPopupMenu", return_value=CUSTOM):
            returned = show_file_menu(str(self.path), 100, 100, [("整理", callback)], callback)
        self.assertEqual([], calls)
        self.assertIs(callback, returned)
        returned()
        self.assertEqual(["called"], calls)

    def test_cancel_does_not_invoke_and_native_rename_uses_model_callback(self):
        callback = lambda: None
        with patch("win32gui.TrackPopupMenu", return_value=0), patch.object(ShellMenu, "invoke") as invoke:
            self.assertIsNone(show_file_menu(str(self.path), 100, 100, [], callback))
            invoke.assert_not_called()
        with patch("win32gui.TrackPopupMenu", return_value=FIRST), patch.object(ShellMenu, "verb", return_value="rename"), patch.object(ShellMenu, "invoke") as invoke:
            self.assertIs(callback, show_file_menu(str(self.path), 100, 100, [], callback))
            invoke.assert_not_called()

    def test_native_edit_dispatch_and_missing_file_cleanup(self):
        with patch("win32gui.TrackPopupMenu", return_value=FIRST), patch.object(ShellMenu, "verb", return_value="edit"), patch.object(ShellMenu, "invoke") as invoke:
            show_file_menu(str(self.path), 100, 100, [], lambda: None)
            self.assertEqual(FIRST, invoke.call_args.args[0])
        with self.assertRaises(OSError):
            show_file_menu(str(self.path.with_name("missing.rdp")), 100, 100, [], lambda: None)
        # An unsuccessful construction must not poison the next Shell session.
        with patch("win32gui.TrackPopupMenu", return_value=0):
            show_file_menu(str(self.path), 100, 100, [], lambda: None)


if __name__ == "__main__":
    unittest.main()
