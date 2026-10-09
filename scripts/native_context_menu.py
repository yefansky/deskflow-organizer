"""Windows Shell file menus, including extension submenus and DESKFLOW actions.

Use the real file PIDL (never a shortcut's resolved target). ctypes is used for
COM because pywin32 does not wrap IContextMenu2/3, needed by owner-drawn menus.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import uuid

import pythoncom
import pywintypes
import win32con
import win32gui

FIRST, LAST, CUSTOM = 1, 0x6FFF, 0x7000
HRESULT = C.c_long
PTR = C.c_void_p


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


def _method(obj, index, result, *args):
    table = C.cast(obj, C.POINTER(C.POINTER(PTR))).contents
    return C.WINFUNCTYPE(result, PTR, *args)(table[index])


def _check(hr):
    if hr < 0:
        raise OSError(f"Windows Shell failed: 0x{hr & 0xFFFFFFFF:08X}")


def _release(obj):
    if obj:
        _method(obj, 2, W.ULONG)(obj)


def refresh_pointer():
    """Re-evaluate the real window's cursor after the native menu loop.

    The pointer may not move after dismissal, so Windows/Qt need not receive a
    new WM_SETCURSOR. Never leave a Shell extension's or resize area's cursor
    on the desktop; ask the window currently under the pointer to choose it.
    """
    win32gui.SetCursor(win32gui.LoadCursor(0, win32con.IDC_ARROW))
    target = win32gui.WindowFromPoint(win32gui.GetCursorPos())
    if target:
        win32gui.SendMessage(target, win32con.WM_SETCURSOR, target,
                             win32con.HTCLIENT | (win32con.WM_MOUSEMOVE << 16))


class InvokeInfo(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("fMask", W.DWORD), ("hwnd", W.HWND),
                ("lpVerb", PTR), ("lpParameters", C.c_char_p), ("lpDirectory", C.c_char_p),
                ("nShow", C.c_int), ("dwHotKey", W.DWORD), ("hIcon", W.HANDLE),
                ("lpTitle", C.c_char_p), ("lpVerbW", PTR),
                ("lpParametersW", W.LPCWSTR), ("lpDirectoryW", W.LPCWSTR),
                ("lpTitleW", W.LPCWSTR), ("ptInvoke", W.POINT)]


class ShellMenu:
    def __init__(self, path: str):
        self.path = str(Path(path).absolute())
        self.context, self.extended = PTR(), PTR()
        self.version = 0
        self.menu = None
        pidl, folder = PTR(), PTR()
        dll = C.WinDLL("shell32")
        parse = dll.SHParseDisplayName
        parse.argtypes = [W.LPCWSTR, PTR, C.POINTER(PTR), W.DWORD, C.POINTER(W.DWORD)]
        parse.restype = HRESULT
        bind = dll.SHBindToParent
        bind.argtypes = [PTR, C.POINTER(GUID), C.POINTER(PTR), C.POINTER(PTR)]
        bind.restype = HRESULT
        free = C.WinDLL("ole32").CoTaskMemFree
        free.argtypes = [PTR]
        try:
            _check(parse(self.path, None, C.byref(pidl), 0, None))
            child = PTR()
            _check(bind(pidl, C.byref(GUID("000214E6-0000-0000-C000-000000000046")), C.byref(folder), C.byref(child)))
            children = (PTR * 1)(child)
            _check(_method(folder, 10, HRESULT, W.HWND, W.UINT, C.POINTER(PTR), C.POINTER(GUID), PTR, C.POINTER(PTR))(
                folder, 0, 1, children, C.byref(GUID("000214E4-0000-0000-C000-000000000046")), None, C.byref(self.context)))
            for version, iid in [(3, "BCFCE0A0-EC17-11D0-8D10-00A0C90F2719"), (2, "000214F4-0000-0000-C000-000000000046")]:
                hr = _method(self.context, 0, HRESULT, C.POINTER(GUID), C.POINTER(PTR))(
                    self.context, C.byref(GUID(iid)), C.byref(self.extended))
                if hr >= 0:
                    self.version = version
                    break
        except Exception:
            self.close()
            raise
        finally:
            _release(folder)
            free(pidl)

    def build(self, actions=(), extended=False):
        self.menu = win32gui.CreatePopupMenu()
        # CMF_CANRENAME, plus extended verbs for Shift+right-click.
        _check(_method(self.context, 3, HRESULT, W.HMENU, W.UINT, W.UINT, W.UINT, W.UINT)(
            self.context, self.menu, 0, FIRST, LAST, 0x10 | (0x100 if extended else 0)))
        self.callbacks = {}
        if actions:
            submenu = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(self.menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(self.menu, win32con.MF_POPUP, submenu, "DESKFLOW 整理")
            for index, (label, callback) in enumerate(actions):
                command = CUSTOM + index
                win32gui.AppendMenu(submenu, win32con.MF_STRING, command, label)
                self.callbacks[command] = callback
        return self.menu

    def verb(self, command):
        if not FIRST <= command <= LAST:
            return ""
        buffer = C.create_unicode_buffer(256)
        hr = _method(self.context, 5, HRESULT, C.c_size_t, W.UINT, PTR, PTR, W.UINT)(
            self.context, command - FIRST, 4, None, C.cast(buffer, PTR), len(buffer))
        return buffer.value.lower() if hr >= 0 else ""

    def window_proc(self, hwnd, message, wparam, lparam):
        if self.extended and message in (0x117, 0x2B, 0x2C, 0x120):
            if self.version == 3:
                result = C.c_ssize_t()
                hr = _method(self.extended, 7, HRESULT, W.UINT, C.c_size_t, C.c_ssize_t, C.POINTER(C.c_ssize_t))(
                    self.extended, message, wparam, lparam, C.byref(result))
                if hr == 0:
                    return result.value
            elif message != 0x120:
                hr = _method(self.extended, 6, HRESULT, W.UINT, C.c_size_t, C.c_ssize_t)(self.extended, message, wparam, lparam)
                if hr == 0:
                    return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def invoke(self, command, hwnd, x, y):
        info = InvokeInfo()
        info.cbSize = C.sizeof(info)
        info.fMask = 0x4000 | 0x20000000  # Unicode and invocation point
        import win32api
        if win32api.GetKeyState(win32con.VK_SHIFT) & 0x8000:
            info.fMask |= 0x10000000
        if win32api.GetKeyState(win32con.VK_CONTROL) & 0x8000:
            info.fMask |= 0x40000000
        info.hwnd = hwnd
        info.lpVerb = info.lpVerbW = command - FIRST
        info.lpDirectoryW = str(Path(self.path).parent)
        info.nShow = win32con.SW_SHOWNORMAL
        info.ptInvoke = W.POINT(x, y)
        _check(_method(self.context, 4, HRESULT, C.POINTER(InvokeInfo))(self.context, C.byref(info)))

    def close(self):
        if self.menu:
            win32gui.DestroyMenu(self.menu)
            self.menu = None
        _release(self.extended)
        _release(self.context)
        self.extended, self.context = PTR(), PTR()


def show_file_menu(path, x, y, actions, rename):
    """Return a callback for custom/rename commands after the native loop exits.

    A dedicated native owner avoids subclassing Qt/Tk or activating Explorer's
    non-activating desktop child windows. Always release menus, window and COM.
    """
    pythoncom.CoInitialize()
    session = None
    hwnd = None
    name = "DeskflowShellMenu" + uuid.uuid4().hex
    registered = False
    try:
        session = ShellMenu(path)
        import win32api
        session.build(actions, bool(win32api.GetKeyState(win32con.VK_SHIFT) & 0x8000))
        cls = win32gui.WNDCLASS()
        cls.lpszClassName = name
        cls.lpfnWndProc = session.window_proc
        cls.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        cls.hInstance = win32api.GetModuleHandle(None)
        win32gui.RegisterClass(cls)
        registered = True
        hwnd = win32gui.CreateWindowEx(win32con.WS_EX_TOOLWINDOW, name, "", win32con.WS_POPUP,
                                      x, y, 0, 0, 0, 0, cls.hInstance, None)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except pywintypes.error:
            # Windows can deny foreground activation to a background caller.
            # The menu can still run; denial is not a Shell loading failure.
            pass
        command = win32gui.TrackPopupMenu(session.menu, win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON,
                                         x, y, 0, hwnd, None)
        win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        if command in session.callbacks:
            return session.callbacks[command]
        if command and FIRST <= command <= LAST:
            if session.verb(command) == "rename":
                return rename
            session.invoke(command, hwnd, x, y)
        return None
    finally:
        if hwnd:
            win32gui.DestroyWindow(hwnd)
        if registered:
            win32gui.UnregisterClass(name, cls.hInstance)
        if session:
            session.close()
        pythoncom.CoUninitialize()
        try:
            refresh_pointer()
        except pywintypes.error:
            # The hovered window may disappear between hit-test and dispatch.
            pass
