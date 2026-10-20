from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import public_desktop_icons as public


class PublicIconsTests(unittest.TestCase):
    def test_only_direct_public_entries_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, 'Public')
            root.mkdir()
            nested = root / 'nested'
            nested.mkdir()
            groups = {'games': [{'path': str(root / 'game.lnk')},
                                 {'path': str(nested / 'other.lnk')},
                                 {'path': str(root.parent / 'private.lnk')}],
                      'inbox': [{'path': str(root / 'pending.lnk')}]}
            with patch.object(public.ctypes.windll.kernel32, 'GetFileAttributesW', return_value=32):
                self.assertEqual(public.public_paths(groups, root), {str(root / 'game.lnk')})

    def test_reparse_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(public.ctypes.windll.kernel32, 'GetFileAttributesW', return_value=0x420):
                self.assertEqual(public.public_paths({'games': [{'path': str(root / 'link')}]}, root), set())

    def test_hide_restore_and_no_repeated_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = str(root / 'game.lnk')
            worker = public.PublicIcons(root, root / 'state.json')
            attributes = {path: 32}
            kernel = Mock()
            kernel.GetFileAttributesW.side_effect = lambda p: attributes.get(p, public.INVALID)
            def set_attributes(p, value):
                attributes[p] = value
                return 1
            kernel.SetFileAttributesW.side_effect = set_attributes
            with patch.object(public.ctypes, 'windll', SimpleNamespace(kernel32=kernel)), patch.object(worker, 'notify') as notify:
                groups = {'games': [{'path': path}]}
                worker.reconcile(groups)
                worker.reconcile(groups)
                self.assertEqual(attributes[path], 38)
                notify.assert_called_once_with(path)
                worker.reconcile({'inbox': [{'path': path}]})
                self.assertEqual(attributes[path], 32)
                self.assertEqual(worker.originals, {})
                self.assertEqual(notify.call_count, 2)


if __name__ == '__main__': unittest.main()
