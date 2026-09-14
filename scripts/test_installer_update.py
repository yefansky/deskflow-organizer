import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app_runtime
import release_updater as updater


class InstallerUpdateTests(unittest.TestCase):
    def release(self):
        return {'tag_name': 'v0.3.10', 'transport': 'https', 'assets': [
            {'name': name, 'browser_download_url': name} for name in (updater.INSTALLER_NAME, updater.INSTALLER_NAME + '.sha256')]}

    def test_installer_is_preferred_and_verified(self):
        content = b'MZ' + b'fixture installer'
        def download(url, target):
            target.write_bytes((hashlib.sha256(content).hexdigest() + '  installer.exe').encode() if url.endswith('.sha256') else content)
        with tempfile.TemporaryDirectory() as temporary, patch.object(updater, '_download_https', side_effect=download):
            package = updater.download_and_verify(self.release(), Path(temporary))
            self.assertEqual(package.suffix, '.exe')
            self.assertEqual(package.read_bytes(), content)

    def test_bad_checksum_removes_installer_and_stops(self):
        def download(url, target):
            target.write_bytes(b'0' * 64 if url.endswith('.sha256') else b'MZcorrupt')
        with tempfile.TemporaryDirectory() as temporary, patch.object(updater, '_download_https', side_effect=download):
            with self.assertRaisesRegex(updater.UpdateError, 'SHA-256'):
                updater.download_and_verify(self.release(), Path(temporary))
            self.assertFalse((Path(temporary) / updater.INSTALLER_NAME).exists())

    def test_frozen_client_refuses_legacy_zip_only_release(self):
        release = {'tag_name': 'v0.3.10', 'assets': [{'name': updater.PACKAGE_NAME}, {'name': updater.CHECKSUM_NAME}]}
        with patch.object(updater, 'FROZEN', True):
            with self.assertRaisesRegex(updater.UpdateError, 'EXE'):
                updater.download_and_verify(release)

    def test_hash_valid_non_executable_is_rejected(self):
        content = b'<html>not an installer</html>'
        def download(url, target):
            target.write_bytes(hashlib.sha256(content).hexdigest().encode() if url.endswith('.sha256') else content)
        with tempfile.TemporaryDirectory() as temporary, patch.object(updater, '_download_https', side_effect=download):
            with self.assertRaisesRegex(updater.UpdateError, 'Windows'):
                updater.download_and_verify(self.release(), Path(temporary))

    def test_frozen_child_roles_use_exes_in_install_directory(self):
        root = Path('C:/Apps/Deskflow with spaces')
        with patch.object(app_runtime, 'FROZEN', True), patch.object(app_runtime, 'APP_ROOT', root):
            self.assertEqual(app_runtime.command('overlay'), [str(root / 'DESKFLOW.exe'), '--overlay'])
            self.assertEqual(app_runtime.command('guard', '--parent', 123), [str(root / 'DESKFLOW.exe'), '--guard', '--parent', '123'])
            self.assertEqual(app_runtime.command('mcp'), [str(root / 'DeskflowMCP.exe')])

    def test_updater_runs_external_helper_instead_of_locked_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / updater.INSTALLER_NAME
            package.write_bytes(b'MZinstaller')
            with patch.object(updater.subprocess, 'Popen') as popen:
                updater.spawn_update(package, 456)
            args = popen.call_args.args[0]
            self.assertEqual(args[0], 'powershell.exe')
            self.assertIn('456', args)
            self.assertIn(hashlib.sha256(package.read_bytes()).hexdigest(), args)
            self.assertTrue((Path(temporary) / 'apply-update.ps1').is_file())


if __name__ == '__main__':
    unittest.main()
