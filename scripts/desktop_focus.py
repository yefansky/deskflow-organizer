"""Observe desktop clicks without consuming input or changing Explorer state."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import queue
import threading

from diagnostics import get_logger

logger = get_logger(__name__)


class DoubleClickTracker:
    """Two complete blank clicks, using Windows time and distance limits."""

    def __init__(self, interval: int, width: int, height: int, drag_x: int, drag_y: int):
        self.interval, self.width, self.height = interval, width, height
        self.drag_x, self.drag_y = drag_x, drag_y
        self.press = self.previous = None

    def feed(self, kind: str, x: int, y: int, timestamp: int, blank: bool) -> bool:
        if kind == "down":
            self.press = (x, y, timestamp) if blank else None
            if not blank:
                self.previous = None
            return False
        press, self.press = self.press, None
        if kind != "up" or not blank or press is None:
            self.previous = None
            return False
        if abs(x - press[0]) > self.drag_x or abs(y - press[1]) > self.drag_y or \
                ((timestamp - press[2]) & 0xFFFFFFFF) > self.interval:
            self.previous = None
            return False
        previous = self.previous
        self.previous = press
        if previous and ((press[2] - previous[2]) & 0xFFFFFFFF) <= self.interval and \
                abs(press[0] - previous[0]) <= self.width // 2 and \
                abs(press[1] - previous[1]) <= self.height // 2:
            self.previous = None
            return True
        return False


class DesktopClickWatcher:
    def __init__(self):
        # Private DLL instances: prototypes must not change the legacy Shell API.
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ptr = ctypes.c_void_p
        self.user.WindowFromPoint.argtypes = [w.POINT]
        self.user.WindowFromPoint.restype = w.HWND
        self.user.GetParent.argtypes = [w.HWND]
        self.user.GetParent.restype = w.HWND
        self.user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.ScreenToClient.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
        self.user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM,
                                                w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.user.SendMessageTimeoutW.restype = w.LPARAM
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.VirtualAllocEx.argtypes = [w.HANDLE, ptr, ctypes.c_size_t, w.DWORD, w.DWORD]
        self.kernel.VirtualAllocEx.restype = ptr
        self.kernel.VirtualFreeEx.argtypes = [w.HANDLE, ptr, ctypes.c_size_t, w.DWORD]
        for name in ("WriteProcessMemory", "ReadProcessMemory"):
            getattr(self.kernel, name).argtypes = [w.HANDLE, ptr, ptr, ctypes.c_size_t, ptr]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.tracker = DoubleClickTracker(self.user.GetDoubleClickTime(),
            self.user.GetSystemMetrics(36), self.user.GetSystemMetrics(37),
            self.user.GetSystemMetrics(68), self.user.GetSystemMetrics(69))
        self.events = queue.SimpleQueue()
        self.ready = threading.Event()
        self.thread_id = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._listen, name="deskflow-desktop-clicks", daemon=True)
        self.thread.start()

    def _class_name(self, hwnd: int) -> str:
        name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, name, len(name))
        return name.value

    def blank_at(self, x: int, y: int, hwnd: int) -> bool:
        # Check the window captured at the input event, and fail closed on any
        # denied/timeout hit test. Selected icons elsewhere do not imply a hit.
        if self._class_name(hwnd) != "SysListView32" or \
                self._class_name(self.user.GetParent(hwnd)) != "SHELLDLL_DefView":
            return False
        point = w.POINT(x, y)
        if not self.user.ScreenToClient(hwnd, ctypes.byref(point)):
            return False

        class HitTest(ctypes.Structure):
            _fields_ = [("point", w.POINT), ("flags", w.UINT), ("item", ctypes.c_int),
                        ("subitem", ctypes.c_int), ("group", ctypes.c_int)]

        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x0008 | 0x0010 | 0x0020, False, pid.value)
        if not process:
            return False
        remote = None
        try:
            size = ctypes.sizeof(HitTest)
            remote = self.kernel.VirtualAllocEx(process, None, size, 0x3000, 0x04)
            hit = HitTest(point=point, item=-2)
            if not remote or not self.kernel.WriteProcessMemory(process, remote, ctypes.byref(hit), size, None):
                return False
            result = ctypes.c_size_t()
            # LVM_HITTEST takes a pointer in Explorer's process, never ours.
            if not self.user.SendMessageTimeoutW(hwnd, 0x1012, 0, remote, 0x0002, 100, ctypes.byref(result)):
                return False
            if not self.kernel.ReadProcessMemory(process, remote, ctypes.byref(hit), size, None):
                return False
            return hit.item == -1 and bool(hit.flags & 0x0001)  # LVHT_NOWHERE
        finally:
            if remote:
                self.kernel.VirtualFreeEx(process, remote, 0, 0x8000)
            self.kernel.CloseHandle(process)

    def _listen(self):
        hook = None
        try:
            self.kernel.GetCurrentThreadId.restype = w.DWORD
            self.thread_id = self.kernel.GetCurrentThreadId()
            self.user.PeekMessageW(ctypes.byref(w.MSG()), None, 0, 0, 0)
            callback_type = ctypes.WINFUNCTYPE(w.LPARAM, ctypes.c_int, w.WPARAM, w.LPARAM)

            class MouseData(ctypes.Structure):
                _fields_ = [("point", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                            ("time", w.DWORD), ("extra", ctypes.c_size_t)]

            self.user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, w.HINSTANCE, w.DWORD]
            self.user.SetWindowsHookExW.restype = w.HHOOK
            self.user.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
            self.user.CallNextHookEx.restype = w.LPARAM
            self.user.UnhookWindowsHookEx.argtypes = [w.HHOOK]
            pressed = None

            def observe(code, message, data):
                nonlocal pressed
                if code >= 0:
                    mouse = ctypes.cast(data, ctypes.POINTER(MouseData)).contents
                    x, y = mouse.point.x, mouse.point.y
                    kind = None
                    if message == 0x0201:  # WM_LBUTTONDOWN
                        pressed = (x, y)
                        kind = "down"
                    elif message == 0x0202:
                        pressed = None
                        kind = "up"
                    elif message == 0x0200 and pressed and (abs(x - pressed[0]) > self.tracker.drag_x or
                                                           abs(y - pressed[1]) > self.tracker.drag_y):
                        pressed = None
                        kind = "cancel"
                    elif message in (0x0204, 0x0207):  # right/middle interrupts a pair
                        pressed = None
                        kind = "cancel"
                    if kind:
                        hwnd = self.user.WindowFromPoint(mouse.point)
                        self.events.put((kind, x, y, mouse.time, hwnd))
                return self.user.CallNextHookEx(None, code, message, data)

            callback = callback_type(observe)  # keep alive until unhook
            hook = self.user.SetWindowsHookExW(14, callback, None, 0)  # WH_MOUSE_LL
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            logger.info("desktop.click_listener ready thread=%s", self.thread_id)
            self.ready.set()
            message = w.MSG()
            while not self.stopping.is_set() and self.user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                self.user.TranslateMessage(ctypes.byref(message))
                self.user.DispatchMessageW(ctypes.byref(message))
        except Exception:
            logger.exception("desktop.click_listener failed")
        finally:
            self.ready.set()
            if hook:
                self.user.UnhookWindowsHookEx(hook)

    def poll(self) -> int:
        toggles = 0
        while not self.events.empty():
            kind, x, y, timestamp, hwnd = self.events.get_nowait()
            blank = kind != "cancel" and self.blank_at(x, y, hwnd)
            toggles += self.tracker.feed(kind, x, y, timestamp, blank)
        return toggles

    def close(self):
        self.stopping.set()
        if self.thread_id:
            self.user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        self.thread.join(timeout=1)
