from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import native_icon_guard
import smart_desktop
from smart_desktop import DesktopShell


class NativeIconNotificationsTest(unittest.TestCase):
    def make_shell(self):
        shell = DesktopShell.__new__(DesktopShell)
        shell.attribute_state = {}
        shell.user32 = Mock()
        shell.kernel32 = Mock()
        shell.kernel32.GetFileAttributesW.return_value = 32
        shell.kernel32.SetFileAttributesW.return_value = 1
        shell._find_listview = Mock(return_value=1)
        shell._save_attribute_state = Mock()
        shell._ensure_guard = Mock()
        return shell

    def test_local_notifications_preserve_unrelated_icon_cache(self):
        with patch.object(smart_desktop.shell, 'SHChangeNotify') as notify:
            DesktopShell._notify_desktop_changed(['G:\\Desktop\\示例.lnk'])
        notify.assert_called_once_with(0x800, 5, 'G:\\Desktop\\示例.lnk', None)

    def test_repeated_maintenance_does_not_notify_or_rewrite_attributes(self):
        shell = self.make_shell()
        shell._notify_desktop_changed = Mock()
        path = str(Path('G:/Desktop/example.lnk'))
        for _ in range(10):
            shell.hide_icons([path])
        shell.kernel32.SetFileAttributesW.assert_called_once_with(path, 38)
        shell._notify_desktop_changed.assert_called_once_with([path])

    def test_return_to_native_desktop_notifies_only_restored_entry(self):
        shell = self.make_shell()
        shell._notify_desktop_changed = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory, 'example.lnk'))
            Path(path).touch()
            shell.attribute_state[path] = 32
            shell.hide_icons([])
        shell.kernel32.SetFileAttributesW.assert_called_once_with(path, 32)
        shell._notify_desktop_changed.assert_called_once_with([path])
        self.assertEqual(shell.attribute_state, {})

    def test_failed_hide_does_not_emit_change(self):
        shell = self.make_shell()
        shell.kernel32.SetFileAttributesW.return_value = 0
        with patch.object(smart_desktop.shell, 'SHChangeNotify') as notify:
            shell.hide_icons(['G:/Desktop/example.lnk'])
        notify.assert_not_called()

    def test_crash_guard_restores_with_path_notification(self):
        kernel = Mock()
        kernel.GetFileAttributesW.return_value = 38
        kernel.SetFileAttributesW.return_value = 1
        notifier = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, '示例.lnk')
            path.touch()
            state = Path(directory, 'state.json')
            state.write_text(json.dumps({str(path): 32}), encoding='utf-8')
            with patch.object(native_icon_guard.ctypes, 'windll', SimpleNamespace(
                kernel32=kernel, shell32=SimpleNamespace(SHChangeNotify=notifier)
            )):
                native_icon_guard.restore(state)
            self.assertEqual(json.loads(state.read_text(encoding='utf-8')), {})
            event, flags, item, extra = notifier.call_args.args
            self.assertEqual((event, flags, item.value, extra), (0x800, 5, str(path), None))


if __name__ == '__main__':
    unittest.main()
