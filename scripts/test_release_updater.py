from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import release_updater


class ReleaseUpdaterTests(unittest.TestCase):
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
