"""GitHub Release updater for the installed DESKFLOW application."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


REPOSITORY = "yefansky/deskflow-organizer"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PACKAGE_NAME = "DESKFLOW-windows-x64.zip"
CHECKSUM_NAME = PACKAGE_NAME + ".sha256"
APP_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = APP_ROOT / "VERSION"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DeskflowOrganizer"
UPDATE_LOG = STATE_DIR / "update.log"


class UpdateError(RuntimeError):
    pass


def current_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "0.0.0"


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise UpdateError(f"无法识别版本号：{value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer_version(candidate: str, installed: str | None = None) -> bool:
    return version_tuple(candidate) > version_tuple(installed or current_version())


def _github_cli() -> str | None:
    return shutil.which("gh.exe") or shutil.which("gh")


def _run_gh(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = _github_cli()
    if not executable:
        raise UpdateError("这是私有仓库，需要先安装 GitHub CLI 并执行 gh auth login。")
    result = subprocess.run(
        [executable, *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise UpdateError(f"GitHub 请求失败：{detail or result.returncode}")
    return result


def latest_release() -> dict[str, Any]:
    """Return the newest stable Release, using gh for private repositories."""
    if _github_cli():
        try:
            data = json.loads(_run_gh(["api", f"repos/{REPOSITORY}/releases/latest"]).stdout)
            data["transport"] = "gh"
            return data
        except (json.JSONDecodeError, UpdateError):
            # A public repository still works through HTTPS when gh is present
            # but not logged in. Keep that path available before reporting.
            pass
    request = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "DESKFLOW-Updater"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise UpdateError("无法读取 GitHub Release。私有仓库请先安装 GitHub CLI 并执行 gh auth login。") from exc
    data["transport"] = "https"
    return data


def _release_assets(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(asset.get("name")): asset
        for asset in release.get("assets", [])
        if isinstance(asset, dict) and asset.get("name")
    }


def _download_https(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "DESKFLOW-Updater"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def download_and_verify(release: dict[str, Any], destination: Path | None = None) -> Path:
    tag = str(release.get("tag_name", ""))
    version_tuple(tag)
    assets = _release_assets(release)
    missing = [name for name in (PACKAGE_NAME, CHECKSUM_NAME) if name not in assets]
    if missing:
        raise UpdateError("Release 缺少更新文件：" + "、".join(missing))
    destination = destination or (STATE_DIR / "updates" / tag.removeprefix("v"))
    destination.mkdir(parents=True, exist_ok=True)
    package = destination / PACKAGE_NAME
    checksum = destination / CHECKSUM_NAME
    for path in (package, checksum):
        path.unlink(missing_ok=True)

    if release.get("transport") == "gh":
        _run_gh([
            "release", "download", tag, "--repo", REPOSITORY,
            "--pattern", PACKAGE_NAME, "--pattern", CHECKSUM_NAME,
            "--dir", str(destination), "--clobber",
        ])
    else:
        try:
            _download_https(str(assets[PACKAGE_NAME]["browser_download_url"]), package)
            _download_https(str(assets[CHECKSUM_NAME]["browser_download_url"]), checksum)
        except (KeyError, OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise UpdateError(f"下载 Release 更新包失败：{exc}") from exc

    try:
        expected = checksum.read_text(encoding="utf-8-sig").split()[0].lower()
    except (OSError, IndexError) as exc:
        raise UpdateError("Release 校验文件无效。") from exc
    actual = hashlib.sha256(package.read_bytes()).hexdigest().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        package.unlink(missing_ok=True)
        raise UpdateError("更新包 SHA-256 校验失败，已停止更新。")
    try:
        with zipfile.ZipFile(package) as archive:
            broken = archive.testzip()
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("更新包不是有效的 ZIP 文件。") from exc
    if broken:
        raise UpdateError(f"更新包内文件损坏：{broken}")
    return package


def _safe_extract(package: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            target = (destination / item.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise UpdateError(f"更新包包含不安全路径：{item.filename}") from exc
        archive.extractall(destination)


def _wait_for_process(process_id: int, timeout_ms: int = 120_000) -> None:
    if process_id <= 0 or os.name != "nt":
        return
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, process_id)
    if handle:
        try:
            ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_ms)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def apply_update(package: Path, wait_pid: int = 0, install_root: Path | None = None, no_launch: bool = False) -> None:
    _wait_for_process(wait_pid)
    expanded = package.parent / "expanded"
    _safe_extract(package, expanded)
    installer = expanded / "install.ps1"
    if not installer.exists():
        raise UpdateError("更新包缺少 install.ps1。")
    arguments = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer)]
    if install_root:
        arguments.extend(["-InstallRoot", str(install_root)])
    if no_launch:
        arguments.append("-NoLaunch")
    result = subprocess.run(arguments, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise UpdateError(f"安装器返回错误码 {result.returncode}。")


def spawn_update(package: Path, wait_pid: int) -> None:
    command = [sys.executable, str(Path(__file__).resolve()), "--apply", str(package), "--wait-pid", str(wait_pid)]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, close_fds=True, creationflags=flags)


def _write_log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_LOG.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply:
            apply_update(args.apply.resolve(), args.wait_pid, args.install_root, args.no_launch)
            _write_log(f"更新完成：{args.apply}")
            return 0
        release = latest_release()
        summary = {"installed": current_version(), "latest": release.get("tag_name"), "update_available": is_newer_version(str(release.get("tag_name", "")))}
        if args.download:
            summary["package"] = str(download_and_verify(release))
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        _write_log(f"更新失败：{exc}")
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
