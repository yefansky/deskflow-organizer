"""D15 real Explorer filtering regression; opt in, isolated files only.

Run with DESKFLOW_REAL_EXPLORER_TEST=1 and TEMP/TMP on a non-system drive.
No production desktop item, registry preference, or desktop window is changed.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pythoncom
import win32com.client
from win32com.shell import shell

from smart_desktop import DesktopShell
import native_icon_guard
from public_desktop_icons import PublicIcons
import public_desktop_icons as public
import smart_desktop


class NotificationBoundaryTests(unittest.TestCase):
    def test_main_and_public_batch_refresh_only_affected_parent_once(self):
        paths = [str(Path("G:/isolated/one/甲.txt")),
                 str(Path("G:/isolated/one/乙.lnk")),
                 str(Path("G:/isolated/two/目录"))]
        for notify in (DesktopShell._notify_desktop_changed, PublicIcons.notify):
            with self.subTest(notify=notify.__qualname__), patch.object(smart_desktop.shell, "SHChangeNotify") as event:
                notify(paths)
                calls = [call.args for call in event.call_args_list]
                self.assertEqual([(0x800, 5, p, None) for p in paths],
                                 [call for call in calls if call[0] == 0x800])
                directories = [call[2] for call in calls if call[0] == 0x1000]
                self.assertCountEqual([str(Path(paths[0]).parent), str(Path(paths[2]).parent)], directories)
                self.assertEqual(5, len(calls))
                self.assertFalse(any(call[0] == 0x8000000 for call in calls))
                event.reset_mock()
                notify([])
                event.assert_not_called()

    def test_public_failed_and_unchanged_paths_never_refresh_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = str(root / "denied.lnk")
            worker = PublicIcons(root, root / "state.json")
            worker.originals = {path: 32}
            kernel = Mock()
            kernel.GetFileAttributesW.return_value = 32
            kernel.SetFileAttributesW.return_value = 0
            groups = {"group": [{"path": path}]}
            with patch.object(public.ctypes, "windll", SimpleNamespace(kernel32=kernel)), \
                 patch.object(public, "public_paths", return_value={path}), \
                 patch.object(worker, "notify") as notify:
                worker.reconcile(groups)
                self.assertTrue(all(not call.args[0] for call in notify.call_args_list))
                self.assertEqual({path: 32}, worker.originals)
                notify.reset_mock()
                kernel.GetFileAttributesW.return_value = 38
                worker.reconcile(groups)
                self.assertTrue(all(not call.args[0] for call in notify.call_args_list))
                kernel.SetFileAttributesW.assert_called_once_with(path, 38)

    def test_guard_mixed_success_failure_only_refreshes_successful_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            succeeded = root / "ok.txt"
            failed = root / "denied.txt"
            succeeded.touch()
            failed.touch()
            state = root / "state.json"
            state.write_text(json.dumps({str(succeeded): 32, str(failed): 32}), encoding="utf-8")
            kernel = Mock()
            kernel.GetFileAttributesW.return_value = 38
            kernel.SetFileAttributesW.side_effect = lambda path, _: path == str(succeeded)
            notify = Mock()
            with patch.object(native_icon_guard.ctypes, "windll", SimpleNamespace(
                    kernel32=kernel, shell32=SimpleNamespace(SHChangeNotify=notify))):
                native_icon_guard.restore(state)
            calls = [(event, flags, path.value, extra)
                     for event, flags, path, extra in (call.args for call in notify.call_args_list)]
            self.assertCountEqual([(0x800, 5, str(succeeded), None),
                                   (0x1000, 5, str(root), None)], calls)
            self.assertEqual({str(failed): 32}, json.loads(state.read_text(encoding="utf-8")))


@unittest.skipUnless(os.environ.get("DESKFLOW_REAL_EXPLORER_TEST") == "1",
                     "opt-in isolated Explorer integration")
class ExplorerVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="d15-view-")
        self.root = Path(self.temporary.name).resolve()
        self.control = self.root / "unchanged.txt"
        self.control.write_text("unchanged control", encoding="utf-8")
        self.targets = [self.root / "示例.lnk", self.root / "folder-文件夹"]
        self.targets[0].write_bytes(b"isolated non-executable fixture")
        self.targets[1].mkdir()
        self.original = {str(p): ctypes.windll.kernel32.GetFileAttributesW(str(p))
                         for p in self.targets}
        self.window = None
        self.addCleanup(self.cleanup)
        app = win32com.client.Dispatch("Shell.Application")
        app.Explore(str(self.root))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and self.window is None:
            for window in app.Windows():
                try:
                    if Path(window.Document.Folder.Self.Path) == self.root:
                        self.window = window
                        break
                except (pythoncom.com_error, AttributeError, TypeError):
                    pass
            self.pump(.05)
        self.assertIsNotNone(self.window, "isolated Explorer window did not open")
        provider = self.window._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
        browser = provider.QueryService(shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
        self.view = browser.QueryActiveShellView().QueryInterface(shell.IID_IFolderView)
        self.wait_count(3)

    @staticmethod
    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pythoncom.PumpWaitingMessages()
            time.sleep(.02)

    def wait_count(self, expected):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            count = self.view.ItemCount(2)  # SVGIO_ALLVIEW, actual visible view rows
            if count == expected:
                return
            self.pump(.05)
        self.assertEqual(expected, self.view.ItemCount(2),
                         "file attributes changed but Explorer view membership is stale")

    def set_hidden(self):
        for path, original in self.original.items():
            self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(path, original | 6))
            self.assertEqual(original | 6, ctypes.windll.kernel32.GetFileAttributesW(path))

    def restore_attributes(self):
        for path, original in self.original.items():
            self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(path, original))

    def assert_files_untouched(self):
        self.assertEqual("unchanged control", self.control.read_text(encoding="utf-8"))
        self.assertEqual(b"isolated non-executable fixture", self.targets[0].read_bytes())
        self.assertTrue(self.targets[1].is_dir())

    def cleanup(self):
        for path, original in self.original.items():
            ctypes.windll.kernel32.SetFileAttributesW(path, original)
        if self.window is not None:
            self.window.Quit()
        self.temporary.cleanup()

    def test_main_notification_removes_hidden_rows_and_restores_them(self):
        self.set_hidden()
        DesktopShell._notify_desktop_changed(list(self.original))
        self.wait_count(1)
        self.assert_files_untouched()
        self.restore_attributes()
        DesktopShell._notify_desktop_changed(list(self.original))
        self.wait_count(3)
        self.assert_files_untouched()

    def test_public_helper_notification_removes_rows_and_restores_them(self):
        self.set_hidden()
        PublicIcons.notify(list(self.original))
        self.wait_count(1)
        self.restore_attributes()
        PublicIcons.notify(list(self.original))
        self.wait_count(3)
        self.assert_files_untouched()

    def test_crash_guard_restoration_reinserts_hidden_rows(self):
        self.set_hidden()
        # Establish an accurately filtered baseline independently of production.
        self.window.Refresh()
        self.wait_count(1)
        state = self.root.parent / (self.root.name + "-state.json")
        self.addCleanup(state.unlink, missing_ok=True)
        state.write_text(json.dumps(self.original, ensure_ascii=False), encoding="utf-8")
        native_icon_guard.restore(state)
        self.wait_count(3)
        self.assertEqual({}, json.loads(state.read_text(encoding="utf-8")))
        for path, original in self.original.items():
            self.assertEqual(original, ctypes.windll.kernel32.GetFileAttributesW(path))
        self.assert_files_untouched()


if __name__ == "__main__":
    unittest.main()

