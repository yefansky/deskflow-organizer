from __future__ import annotations

import hashlib
import tempfile
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import release_updater


class ReleaseUpdaterTests(unittest.TestCase):
    def test_valid_cli_login_does_not_read_git_credentials(self):
        result = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(release_updater, '_github_cli', return_value='gh.exe'), \
             patch.object(release_updater.subprocess, 'run', return_value=result), \
             patch.object(release_updater, '_git_github_token') as credentials:
            self.assertIs(release_updater._run_gh(['api', 'repos/example']), result)
        credentials.assert_not_called()

    def test_invalid_cli_login_retries_with_private_child_environment(self):
        failed = subprocess.CompletedProcess([], 1, '', 'invalid login')
        success = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(release_updater, '_github_cli', return_value='gh.exe'), \
             patch.object(release_updater.subprocess, 'run', side_effect=[failed, success]) as run, \
             patch.object(release_updater, '_git_github_token', return_value='fixture-secret'):
            release = release_updater.latest_release()
        self.assertEqual(release['transport'], 'gh')
        self.assertEqual(run.call_args.kwargs['env']['GH_TOKEN'], 'fixture-secret')
        self.assertNotIn('fixture-secret', str(run.call_args.args))
        self.assertNotIn('fixture-secret', str(release))

    def test_failed_retry_redacts_credential_in_error(self):
        failed = subprocess.CompletedProcess([], 1, '', 'invalid login')
        echoed = subprocess.CompletedProcess([], 1, '', 'fixture-secret denied')
        with patch.object(release_updater, '_github_cli', return_value='gh.exe'), \
             patch.object(release_updater.subprocess, 'run', side_effect=[failed, echoed]), \
             patch.object(release_updater, '_git_github_token', return_value='fixture-secret'):
            with self.assertRaises(release_updater.UpdateError) as error:
                release_updater._run_gh(['api', 'repos/example'])
        self.assertNotIn('fixture-secret', str(error.exception))

    def test_git_credential_lookup_cannot_prompt(self):
        result = subprocess.CompletedProcess([], 0, 'username=test\npassword=fixture-secret\n', '')
        with patch.object(release_updater.shutil, 'which', return_value='git.exe'), \
             patch.object(release_updater.subprocess, 'run', return_value=result) as run:
            self.assertEqual(release_updater._git_github_token(), 'fixture-secret')
        environment = run.call_args.kwargs['env']
        self.assertEqual(environment['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(environment['GCM_INTERACTIVE'], 'never')

    def test_git_credential_timeout_is_optional(self):
        with patch.object(release_updater.shutil, 'which', return_value='git.exe'), \
             patch.object(release_updater.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 15)):
            self.assertIsNone(release_updater._git_github_token())

    def test_semantic_versions_only_move_forward(self) -> None:
        self.assertTrue(release_updater.is_newer_version("v0.2.0", "0.1.1"))
        self.assertFalse(release_updater.is_newer_version("v0.1.1", "0.1.1"))
        self.assertFalse(release_updater.is_newer_version("v0.1.0", "0.1.1"))
        with self.assertRaises(release_updater.UpdateError):
            release_updater.version_tuple("latest")

    def test_download_requires_both_release_assets(self) -> None:
        release = {"tag_name": "v0.2.0", "assets": [{"name": release_updater.PACKAGE_NAME}]}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(release_updater.UpdateError, release_updater.CHECKSUM_NAME):
                release_updater.download_and_verify(release, Path(temporary))

    def test_https_download_is_verified_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_zip = root / "source.zip"
            with zipfile.ZipFile(source_zip, "w") as archive:
                archive.writestr("install.ps1", "Write-Host ok")
            digest = hashlib.sha256(source_zip.read_bytes()).hexdigest()
            source_checksum = root / "source.sha256"
            source_checksum.write_text(f"{digest}  {release_updater.PACKAGE_NAME}\n", encoding="utf-8")
            destination = root / "download"
            release = {
                "tag_name": "v0.2.0",
                "transport": "https",
                "assets": [
                    {"name": release_updater.PACKAGE_NAME, "browser_download_url": "package"},
                    {"name": release_updater.CHECKSUM_NAME, "browser_download_url": "checksum"},
                ],
            }

            def copy_url(url: str, target: Path) -> None:
                target.write_bytes((source_zip if url == "package" else source_checksum).read_bytes())

            with patch.object(release_updater, "_download_https", side_effect=copy_url):
                package = release_updater.download_and_verify(release, destination)
            self.assertEqual(digest, hashlib.sha256(package.read_bytes()).hexdigest())

    def test_zip_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "unsafe.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")
            with self.assertRaises(release_updater.UpdateError):
                release_updater._safe_extract(package, root / "expanded")
            self.assertFalse((root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
