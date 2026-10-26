"""Restore desktop file attributes if DESKFLOW exits unexpectedly.

The organizer temporarily marks only already-organized desktop filesystem
items as hidden system items.  Explorer's virtual icons and pending new files
remain native.  This tiny process makes that reversible even after a crash or
forced termination.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import time
from pathlib import Path


INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def restore(state_path: Path) -> None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    kernel32 = ctypes.windll.kernel32
    remaining = {}
    restored_parents: set[str] = set()
    for raw_path, attributes in state.items():
        path = Path(raw_path)
        if not path.exists():
            continue
        current = kernel32.GetFileAttributesW(str(path))
        if current != INVALID_FILE_ATTRIBUTES:
            if current == int(attributes):
                continue
            if kernel32.SetFileAttributesW(str(path), int(attributes)):
                restored_parents.add(str(path.parent))
                # SHCNE_ATTRIBUTES + SHCNF_PATHW: refresh only this entry.
                # ASSOCCHANGED would invalidate the global icon cache.
                try:
                    ctypes.windll.shell32.SHChangeNotify(0x00000800, 0x0005, ctypes.c_wchar_p(str(path)), None)
                except Exception:
                    pass
            else:
                remaining[raw_path] = attributes
    # Restored files must be re-enumerated to reappear after being filtered
    # from Explorer; attribute notifications alone only update existing rows.
    for parent in sorted(restored_parents):
        try:
            ctypes.windll.shell32.SHChangeNotify(0x00001000, 0x0005, ctypes.c_wchar_p(parent), None)
        except Exception:
            pass
    try:
        state_path.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    kernel32 = ctypes.windll.kernel32
    synchronize = 0x00100000
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(synchronize, False, args.parent)
    if handle:
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel32.CloseHandle(handle)
    else:
        # The parent may be between startup and process-table visibility.
        time.sleep(0.5)
    restore(args.state)


if __name__ == "__main__":
    main()
