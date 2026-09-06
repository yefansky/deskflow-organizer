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
    for raw_path, attributes in state.items():
        path = Path(raw_path)
        if not path.exists():
            continue
        current = kernel32.GetFileAttributesW(str(path))
        if current != INVALID_FILE_ATTRIBUTES:
            kernel32.SetFileAttributesW(str(path), int(attributes))
    try:
        state_path.write_text("{}", encoding="utf-8")
    except OSError:
        pass
    # Ask Explorer to refresh its desktop enumeration after the bulk restore.
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    kernel32 = ctypes.windll.kernel32
    synchronize = 0x00100000
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
