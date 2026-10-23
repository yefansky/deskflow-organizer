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
from app_runtime import APP_ROOT, RESOURCE_ROOT, FROZEN


REPOSITORY = "yefansky/deskflow-organizer"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PACKAGE_NAME = "DESKFLOW-windows-x64.zip"
CHECKSUM_NAME = PACKAGE_NAME + ".sha256"
INSTALLER_NAME = "DESKFLOW-windows-x64-Setup.exe"
VERSION_FILE = RESOURCE_ROOT / "VERSION"
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


def _request_failure(error: OSError, action: str) -> UpdateError:
    """Describe public-service failures without requesting developer login."""
    if isinstance(error, urllib.error.HTTPError):
        headers = error.headers or {}
        if error.code == 429 or (error.code == 403 and (
            headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")
        )):
            return UpdateError(f"{action}暂时受到服务限流，请稍后重试。")
        if error.code == 404:
            return UpdateError(f"{action}未找到发布文件，请稍后重试或查看软件发布页面。")
        if error.code in (401, 403):
            return UpdateError(f"{action}的公开下载服务暂时不可访问，请稍后重试。")
        return UpdateError(f"{action}服务返回 HTTP {error.code}，请稍后重试。")
    return UpdateError(f"{action}失败，请检查网络连接后重试。")


def latest_release() -> dict[str, Any]:
    """Read the public Release using built-in HTTPS, without tools or credentials."""
    request = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "DESKFLOW-Updater"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise _request_failure(exc, "检查更新") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新服务返回了无效的版本信息，请稍后重试。") from exc
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise UpdateError("更新服务返回了无效的版本信息，请稍后重试。")
    version_tuple(str(data.get("tag_name", "")))
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("更新服务尚未提供正式版本，请稍后重试。")
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
    package_name = INSTALLER_NAME if INSTALLER_NAME in assets else PACKAGE_NAME
    if FROZEN and package_name != INSTALLER_NAME:
        raise UpdateError("Release 缺少 EXE 安装程序，已停止更新。")
    checksum_name = package_name + ".sha256"
    missing = [name for name in (package_name, checksum_name) if name not in assets]
    if missing:
        raise UpdateError("Release 缺少更新文件：" + "、".join(missing))
    destination = destination or (STATE_DIR / "updates" / tag.removeprefix("v"))
    destination.mkdir(parents=True, exist_ok=True)
    package = destination / package_name
    checksum = destination / checksum_name
    for path in (package, checksum):
        path.unlink(missing_ok=True)

    # Ignore transport hints in metadata: installed clients always use anonymous
    # HTTP(S), even if an earlier caller cached a Release obtained through gh.
    try:
        _download_https(str(assets[package_name]["browser_download_url"]), package)
        _download_https(str(assets[checksum_name]["browser_download_url"]), checksum)
    except OSError as exc:
        raise _request_failure(exc, "下载更新") from exc
    except (KeyError, ValueError) as exc:
        raise UpdateError("更新服务缺少有效的下载地址，请稍后重试。") from exc

    try:
        expected = checksum.read_text(encoding="utf-8-sig").split()[0].lower()
    except (OSError, IndexError) as exc:
        raise UpdateError("Release 校验文件无效。") from exc
    actual = hashlib.sha256(package.read_bytes()).hexdigest().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        package.unlink(missing_ok=True)
        raise UpdateError("更新包 SHA-256 校验失败，已停止更新。")
    if package_name == INSTALLER_NAME:
        with package.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise UpdateError("更新文件不是 Windows 可执行安装程序。")
        return package
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
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(synchronize, False, process_id)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
                raise UpdateError("旧版进程未退出，已停止更新。")
        finally:
            kernel.CloseHandle(handle)
    elif ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: process already exited
        raise UpdateError("无法确认旧版进程状态，已停止更新。")


def apply_update(package: Path, wait_pid: int = 0, install_root: Path | None = None, no_launch: bool = False) -> None:
    _wait_for_process(wait_pid)
    if package.suffix.lower() == ".exe":
        arguments = [str(package), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=" + str(install_root or APP_ROOT)]
        if not no_launch:
            arguments.append("/RESTARTAPP=1")
        result = subprocess.run(arguments, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            raise UpdateError(f"安装器返回错误码 {result.returncode}。")
        return
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
    if package.suffix.lower() == ".exe":
        # The helper lives outside the installation and has no Python DLL lock.
        # It waits for the actual parent process exit, then uses the same Inno
        # installer as a first installation. Failed installs leave a durable log.
        helper = package.parent / "apply-update.ps1"
        shutil.copyfile(RESOURCE_ROOT / "packaging" / "apply-update.ps1", helper)
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper),
                   "-Installer", str(package.resolve()), "-WaitPid", str(wait_pid), "-InstallRoot", str(APP_ROOT),
                   "-ExpectedHash", hashlib.sha256(package.read_bytes()).hexdigest()]
        subprocess.Popen(command, close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
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
