"""Shared source/frozen entry points and installation paths."""
from pathlib import Path
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
APP_ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", APP_ROOT))


def command(role="manager", *arguments):
    if FROZEN:
        executable = APP_ROOT / ("DeskflowMCP.exe" if role == "mcp" else "DESKFLOW.exe")
        return [str(executable), *([] if role in ("manager", "mcp") else ["--" + role]), *map(str, arguments)]
    python = Path(sys.executable)
    if role != "mcp" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")
    scripts = {"manager": "deskflow_manager.py", "overlay": "smart_desktop.py", "guard": "native_icon_guard.py", "mcp": "mcp_server.py", "public-icons": "public_desktop_icons.py"}
    return [str(python), str(APP_ROOT / "scripts" / scripts[role]), *map(str, arguments)]


def watch_update(callback):
    """Let installers gracefully release GUI/MCP file locks before replacement."""
    if os.name != "nt":
        return
    import ctypes
    import threading
    def watch():
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        event = kernel.CreateEventW(None, True, False, "Local\\DeskflowOrganizer.Update")
        if event:
            try:
                if kernel.WaitForSingleObject(event, 0xFFFFFFFF) == 0:
                    callback()
            finally:
                kernel.CloseHandle(event)
    threading.Thread(target=watch, daemon=True, name="deskflow-update-exit").start()
