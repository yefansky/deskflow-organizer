from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import release_updater


class ReleaseUpdaterTests(unittest.TestCase):
    def test_release_check_is_anonymous_despite_developer_environment(self):
        response = io.BytesIO(json.dumps({"tag_name": "v0.3.19", "assets": []}).encode())
        with patch.dict(os.environ, GH_TOKEN="fixture-secret", GITHUB_TOKEN="fixture-secret"), \
             patch.object(release_updater.shutil, "which", side_effect=AssertionError("tool discovery")), \
             patch.object(release_updater.subprocess, "run", side_effect=AssertionError("external tool")), \
             patch.object(release_updater.urllib.request, "urlopen", return_value=response) as request:
            result = release_updater.latest_release()
        self.assertEqual(result["transport"], "https")
        self.assertIsNone(request.call_args.args[0].get_header("Authorization"))
        self.assertNotIn("fixture-secret", str(request.call_args))

    def test_service_errors_never_request_login_or_installation(self):
        for status, headers, expected in [
            (401, {}, "不可访问"), (403, {}, "不可访问"), (403, None, "不可访问"),
            (403, {"X-RateLimit-Remaining": "0"}, "限流"),
            (429, {}, "限流"), (404, {}, "未找到"), (503, {}, "503"),
        ]:
            with self.subTest(status=status, headers=headers):
                error = urllib.error.HTTPError("https://example.test", status, "fixture", headers, None)
                with patch.object(release_updater.urllib.request, "urlopen", side_effect=error):
                    with self.assertRaises(release_updater.UpdateError) as caught:
                        release_updater.latest_release()
                message = str(caught.exception)
                self.assertIn(expected, message)
                for forbidden in ("登录", "CLI", "auth login", "安装"):
                    self.assertNotIn(forbidden, message)

    def test_network_failure_is_retryable(self):
        for error in (TimeoutError(), urllib.error.URLError("offline")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(release_updater.urllib.request, "urlopen", side_effect=error):
                with self.assertRaisesRegex(release_updater.UpdateError, "网络连接后重试"):
                    release_updater.latest_release()

    def test_malformed_or_unstable_release_is_rejected(self):
        for body in (b"not json", b"\xff", b"[]", b"null", b"{}",
                     b'{"tag_name":"v0.3.19","assets":[],"prerelease":true}',
                     b'{"tag_name":"v0.3.19","assets":[],"draft":true}'):
            with self.subTest(body=body), \
                 patch.object(release_updater.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(release_updater.UpdateError):
                    release_updater.latest_release()

    def test_real_http_discovery_and_download_never_invoke_tools(self):
        payload = b"MZ-inert-fixture-never-executed"
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/latest":
                    base = f"http://127.0.0.1:{self.server.server_port}"
                    body = json.dumps({"tag_name": "v0.3.19", "assets": [
                        {"name": name, "browser_download_url": base + "/" + name}
                        for name in (release_updater.INSTALLER_NAME, release_updater.INSTALLER_NAME + ".sha256")
                    ]}).encode()
                elif self.path.endswith(".sha256"):
                    body = hashlib.sha256(payload).hexdigest().encode()
                else:
                    body = payload
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, PATH="", GH_TOKEN="fixture-secret"), \
                 patch.object(release_updater, "RELEASE_API", f"http://127.0.0.1:{server.server_port}/latest"), \
                 patch.object(release_updater.shutil, "which", side_effect=AssertionError("tool discovery")), \
                 patch.object(release_updater.subprocess, "run", side_effect=AssertionError("external tool")):
                release = release_updater.latest_release()
                # Stale metadata from a previous gh caller must not re-enable CLI.
                release["transport"] = "gh"
                package = release_updater.download_and_verify(release, Path(directory))
                self.assertEqual(package.read_bytes(), payload)
            self.assertEqual(len(seen), 3)
            self.assertTrue(all(auth is None for _, auth in seen))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

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
