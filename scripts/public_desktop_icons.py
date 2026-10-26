"""Explicit, session-scoped elevation for Public Desktop attributes only.

Never executes desktop files, moves them, or changes ACLs. Exiting the overlay
restores original attributes. The ordinary UI and file-opening stay unelevated.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
from urllib.request import urlopen

from win32com.shell import shell, shellcon
from app_runtime import command

INVALID = 0xFFFFFFFF


def read_api(route: str) -> dict:
    with urlopen('http://127.0.0.1:8765' + route, timeout=3) as response:
        return json.load(response)


def launch_elevated() -> None:
    health = read_api('/health')
    if not health.get('pid'):
        raise RuntimeError('请先重新启动 DESKFLOW，再修复公共桌面权限。')
    args = command('public-icons', '--parent', int(health['pid']))
    execute = ctypes.windll.shell32.ShellExecuteW
    execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    execute.restype = ctypes.c_ssize_t
    result = execute(None, 'runas', args[0], subprocess.list2cmdline(args[1:]), None, 0)
    if result <= 32:
        raise OSError('Windows 未允许管理员修复；公共桌面项目保持原状。')


def public_paths(groups: dict, root: Path) -> set[str]:
    """Accept only direct, non-reparse entries in the OS Public Desktop."""
    result = set()
    for category, items in groups.items():
        if category == 'inbox': continue
        for item in items:
            path = Path(item['path'])
            try:
                if path.parent.resolve() != root.resolve(): continue
                if path.is_symlink() or path.is_junction(): continue
                attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
                if attributes != INVALID and not attributes & 0x400:
                    result.add(str(path))
            except OSError:
                continue
    return result


class PublicIcons:
    def __init__(self, root: Path, state: Path):
        self.root, self.state = root, state
        self.originals: dict[str, int] = {}
        try:
            raw = json.loads(state.read_text(encoding='utf-8'))
            allowed = public_paths({'restore': [{'path': p} for p in raw]}, root)
            self.originals = {p: int(a) for p, a in raw.items() if p in allowed and 0 <= int(a) < INVALID}
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.originals, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.state)

    @staticmethod
    def notify(paths: list[str]):
        for path in paths:
            shell.SHChangeNotify(shellcon.SHCNE_ATTRIBUTES, shellcon.SHCNF_PATHW, path, None)
        # Attribute-only updates leave an already-visible hidden icon ghosted.
        for parent in sorted({str(Path(path).parent) for path in paths}):
            shell.SHChangeNotify(shellcon.SHCNE_UPDATEDIR, shellcon.SHCNF_PATHW, parent, None)

    def restore(self, paths: set[str]):
        kernel = ctypes.windll.kernel32
        changed = []
        for path in paths:
            current = kernel.GetFileAttributesW(path)
            original = self.originals[path]
            if current == INVALID or current == original:
                self.originals.pop(path, None)
            elif kernel.SetFileAttributesW(path, original):
                self.originals.pop(path, None)
                changed.append(path)
        self.save()
        if changed: self.notify(changed)

    def reconcile(self, groups: dict):
        wanted = public_paths(groups, self.root)
        self.restore(set(self.originals) - wanted)
        kernel = ctypes.windll.kernel32
        changed = []
        for path in wanted:
            current = kernel.GetFileAttributesW(path)
            if current == INVALID: continue
            if path not in self.originals:
                self.originals[path] = int(current)
                self.save()  # Crash recovery must precede the first mutation.
            if current & 6 != 6 and kernel.SetFileAttributesW(path, current | 6):
                changed.append(path)
        if changed: self.notify(changed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', type=int)
    parser.add_argument('--request', action='store_true')
    args = parser.parse_args()
    if args.request:
        launch_elevated()
        return
    if not args.parent: parser.error('--parent is required')
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.SetLastError(0)
    mutex = kernel.CreateMutexW(None, False, 'Local\\DeskflowOrganizer.PublicIcons')
    if not mutex or kernel.GetLastError() == 183:
        if mutex: kernel.CloseHandle(mutex)
        return
    parent = kernel.OpenProcess(0x00100000, False, args.parent)
    if not parent:
        kernel.CloseHandle(mutex)
        return
    root = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_COMMON_DESKTOPDIRECTORY, None, 0))
    state = Path(os.environ['APPDATA']) / 'SmartDesktopOrganizer' / 'public_icon_state.json'
    worker = PublicIcons(root, state)
    try:
        # Recover any interrupted helper before hiding this session's entries.
        worker.restore(set(worker.originals))
        while kernel.WaitForSingleObject(parent, 1000) == 258:
            try:
                health = read_api('/health')
                if health.get('pid') != args.parent: continue
                worker.reconcile(read_api('/layout')['categories'])
            except (OSError, ValueError, KeyError):
                continue
    finally:
        worker.restore(set(worker.originals))
        kernel.CloseHandle(parent)
        kernel.CloseHandle(mutex)


if __name__ == '__main__': main()
