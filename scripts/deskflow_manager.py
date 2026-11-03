"""Human-friendly lifecycle manager for the local DESKFLOW organizer."""
from __future__ import annotations
import ctypes
import json, os, subprocess, sys, time, winreg, argparse, queue, threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, colorchooser, ttk
from urllib.request import Request, urlopen
import pystray
from pystray._util import win32 as tray_win32
from PIL import Image, ImageDraw, ImageTk
from win32com.client import Dispatch
import win32api
from release_updater import current_version, download_and_verify, is_newer_version, latest_release, spawn_update
from app_runtime import APP_ROOT, RESOURCE_ROOT, FROZEN, command, watch_update
from diagnostics import configure_logging, get_logger, logging_arguments, timed_operation, traced

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DeskflowOrganizer"
STATE = STATE_DIR / "manager.json"
STARTUP = Path(os.environ.get("APPDATA", Path.home())) / "Microsoft/Windows/Start Menu/Programs/Startup/DESKFLOW 启动器.bat"
ASSET_DIR = RESOURCE_ROOT / "assets"
ICON_PNG = ASSET_DIR / "deskflow-icon-v1.png"
ICON_ICO = ASSET_DIR / "deskflow-icon-v1.ico"

def gui_python() -> str:
    """Use pythonw when available so desktop entry points never open a console."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else sys.executable)


# Windows 会把「强制重定向信任」策略继承给子进程；带该策略的实例启动的
# Bambu Studio 等程序读取跨盘 junction 时会报「不受信任的装入点」[system:448]。
# 复位只能由不继承该策略的父上下文完成，所以这里用 WMI（父进程 WmiPrvSE）重建实例。
PROCESS_REDIRECTION_TRUST_POLICY = 16
REDIRECTION_TRUST_ENFORCE = 0x1
CLEAN_RESTART_SCRIPT = """$ErrorActionPreference = 'Stop'
$planPath = Join-Path $PSScriptRoot 'clean-restart-plan.json'
$plan = Get-Content -Raw -Encoding UTF8 $planPath | ConvertFrom-Json
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Process -Id $plan.wait_pid -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {
    Start-Sleep -Milliseconds 250
}
Start-Sleep -Milliseconds 500
$commandLine = '"' + $plan.executable + '"'
foreach ($argument in $plan.arguments) { $commandLine += ' "' + $argument + '"' }
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $commandLine; CurrentDirectory = $plan.cwd }
if ($result.ReturnValue -ne 0) { exit 1 }
"""


def process_redirection_flags(pid: int) -> int | None:
    """Return PROCESS_MITIGATION_REDIRECTION_TRUST_POLICY flags, or None when unreadable."""
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetProcessMitigationPolicy.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        kernel.GetProcessMitigationPolicy.restype = ctypes.c_bool
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle: return None
        try:
            value = ctypes.c_uint32(0)
            if not kernel.GetProcessMitigationPolicy(handle, PROCESS_REDIRECTION_TRUST_POLICY, ctypes.byref(value), ctypes.sizeof(value)):
                return None
            return int(value.value)
        finally:
            kernel.CloseHandle(handle)
    except (AttributeError, OSError):
        return None

def windows_desktop() -> Path:
    """Return the user's actual Desktop, including redirected Desktop folders."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            return Path(os.path.expandvars(value))
    except OSError:
        return Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"

DESKTOP_SHORTCUT = windows_desktop() / "DESKFLOW 管理器.lnk"

def load():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try: return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {"installed": False, "pid": None, "autostart": False, "minimize_to_tray": True}
def save(data): STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def notify_tray(code, data):
    """Keep the BOOL and thread-local error from this exact Shell call."""
    api = ctypes.WinDLL("shell32", use_last_error=True).Shell_NotifyIconW
    api.argtypes = (ctypes.wintypes.DWORD, ctypes.POINTER(tray_win32.NOTIFYICONDATAW))
    api.restype = ctypes.wintypes.BOOL
    ctypes.set_last_error(0)
    result = bool(api(code, ctypes.byref(data)))
    error = ctypes.get_last_error()
    return result, error


class RegisteredTrayIcon(pystray.Icon):
    """Small Windows adapter: preserve pystray's loop/menu, audit registration.

    Uses the installed pystray Windows backend's private message/ready hooks.
    No process-global patch and no third-party files are modified.
    """
    RETRY_MESSAGE = tray_win32.WM_USER + 43

    def __init__(self, *args, report, **kwargs):
        self.registered = False
        self._report = report
        self._worker = None
        self._loop_entered = False
        super().__init__(*args, **kwargs)
        self._message_handlers[self.RETRY_MESSAGE] = self._retry_message

    def _message(self, code, flags, **kwargs):
        data = tray_win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(tray_win32.NOTIFYICONDATAW),
            hWnd=self._hwnd, uID=0, uFlags=flags, **kwargs)
        result, error = notify_tray(code, data)
        logger.info("tray.notify code=%s hwnd=%s uid=0 ok=%s error=%s",
                    code, self._hwnd, result, error)
        if code == tray_win32.NIM_ADD:
            self.registered = result
        elif code == tray_win32.NIM_DELETE or not result:
            self.registered = False
        self._report(self, self.registered, f"Shell code={code}, ok={result}, error={error}")
        return result

    def _mark_ready(self):
        super()._mark_ready()
        # No default setup thread: a pre-ready window failure must not leave
        # an invisible setup thread waiting forever on pystray's ready queue.
        self.visible = True

    def _create_window(self, atom):
        # The backend assigns _hwnd only after this method returns.  Preserve
        # its top-level popup/filter contract, but own the half-created window
        # if the filter fails before that assignment can happen.
        hwnd = tray_win32.CreateWindowEx(
            0, atom, None, tray_win32.WS_POPUP, 0, 0, 0, 0,
            0, None, tray_win32.GetModuleHandle(None), None)
        try:
            tray_win32.ChangeWindowMessageFilterEx(
                hwnd, tray_win32.WM_TASKBARCREATED, tray_win32.MSGFLT_ALLOW, None)
        except Exception:
            try: tray_win32.DestroyWindow(hwnd)
            except Exception: logger.exception("tray.partial_window_cleanup failed")
            raise
        return hwnd

    def run_detached(self, setup=None):
        if setup is not None:
            raise ValueError("RegisteredTrayIcon manages its own readiness")
        if self.worker_alive():
            return
        def run():
            try:
                super(RegisteredTrayIcon, self)._run()
            except Exception as exc:
                logger.exception("tray.thread failed")
                self._report(self, False, f"托盘线程启动失败：{exc}")
            finally:
                if not self._loop_entered:
                    # _run can fail while creating its first/second window,
                    # before pystray's normal mainloop cleanup takes ownership.
                    self._HWND_TO_ICON.pop(self._hwnd, None)
                    for hwnd in (self._hwnd, self._menu_hwnd):
                        if hwnd:
                            try: tray_win32.DestroyWindow(hwnd)
                            except Exception: logger.exception("tray.window_cleanup failed")
                    try: self._unregister_class(self._atom)
                    except Exception: logger.exception("tray.class_cleanup failed")
                self.registered = False
                self._running = False
                self._report(self, False, "托盘线程已停止")
        self._worker = threading.Thread(target=run, name="deskflow-tray", daemon=True)
        self._worker.start()

    def _mainloop(self):
        self._loop_entered = True
        return super()._mainloop()

    def worker_alive(self):
        return self._worker is not None and self._worker.is_alive()

    def retry_registration(self):
        if not self.worker_alive() or not self._hwnd:
            return False
        # Execute on the existing tray message thread, never spawn another.
        tray_win32.PostMessage(self._hwnd, self.RETRY_MESSAGE, 0, 0)
        return True

    def _retry_message(self, _wparam, _lparam):
        self._hide()
        self._show()

    def stop(self):
        # pystray's base stop assumes its optional setup thread was created.
        if self.worker_alive() and self._hwnd:
            self._stop()


class Manager:
    def __init__(self):
        self.data=load(); self.root=tk.Tk(); self.root.title("DESKFLOW · 智能桌面整理管理器")
        if self.data.get("autostart"): self.write_autostart_entry()
        try: self.root.iconbitmap(default=str(ICON_ICO))
        except tk.TclError: pass
        self.root.geometry("660x500"); self.root.configure(bg="#101827"); self.root.resizable(False,False)
        self.brand_icon = self.load_brand_icon(46)
        self.configure_theme()
        # The tray window is also used through small RDP desktops.  Keep the
        # controls in a scrollable body so a newly added setting can never be
        # pushed below the bottom edge of the current work area.
        self.canvas=tk.Canvas(self.root,bg="#101827",highlightthickness=0,bd=0)
        self.scrollbar=ttk.Scrollbar(self.root,orient="vertical",command=self.canvas.yview,style="Deskflow.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body=tk.Frame(self.canvas,bg="#101827")
        self.body_window=self.canvas.create_window((0,0),window=self.body,anchor="nw")
        self.body.bind("<Configure>",self.update_scroll_region)
        self.canvas.bind("<Configure>",lambda event: self.canvas.itemconfigure(self.body_window,width=event.width))
        self.root.bind_all("<MouseWheel>", lambda event: self.canvas.yview_scroll(-1 * (event.delta // 120), "units"), add="+")
        self.canvas.pack(side="left",fill="both",expand=True)
        self.scrollbar.pack(side="right",fill="y")
        self.status=tk.StringVar(); self.update_status=tk.StringVar(value=f"当前版本 {current_version()}")
        self.update_busy=False; self.tray_icon=None; self.tray_actions=queue.Queue(); self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._tray_ready=False; self._tray_hide_pending=True
        self.tray_status=tk.StringVar(value="正在连接通知区域…")
        self.draw(); self.refresh()
        # Stay visible until Shell acknowledges the tray icon.  A background
        # thread or a Python Icon object alone is not a usable control entry.
        self.ensure_tray()
        self.root.after(80, self.process_tray_actions)
        self.root.after_idle(self.fit_to_work_area)
        self.update_silent = False
        watch_update(lambda: self.tray_actions.put(("exit", None)))
        if FROZEN:
            self.root.after(12000, lambda: self.check_for_updates(silent=True))
    @staticmethod
    def load_brand_icon(size: int):
        try:
            image = Image.open(ICON_PNG).convert("RGBA")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(image)
        except (OSError, tk.TclError):
            return None
    def configure_theme(self):
        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except tk.TclError: pass
        style.configure("Deskflow.TCombobox", fieldbackground="#17243a", background="#20324c", foreground="#eef6ff", arrowcolor="#7ee6ff", padding=5)
        style.map("Deskflow.TCombobox", fieldbackground=[("readonly", "#17243a")], foreground=[("readonly", "#eef6ff")], selectbackground=[("readonly", "#263d5b")])
        style.configure("Deskflow.Vertical.TScrollbar", background="#263a57", troughcolor="#101827", bordercolor="#101827", arrowcolor="#7ee6ff")
    def action_button(self, parent, text, command, *, tone="secondary", width=None):
        palette = {
            "primary": ("#16bfd4", "#071824", "#48e7f4"),
            "secondary": ("#22344e", "#eef6ff", "#304a6c"),
            "violet": ("#5d52b8", "#ffffff", "#786ee8"),
            "danger": ("#4b2a3c", "#ffdce6", "#734158"),
        }[tone]
        return tk.Button(parent, text=text, command=command, width=width, cursor="hand2", relief="flat", bd=0,
                         bg=palette[0], fg=palette[1], activebackground=palette[2], activeforeground=palette[1],
                         font=("Microsoft YaHei UI", 10, "bold"), padx=11, pady=7, highlightthickness=0)
    def update_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
    def fit_to_work_area(self):
        self.root.update_idletasks()
        requested_width=self.body.winfo_reqwidth(); requested_height=self.body.winfo_reqheight()
        available_width=max(420,self.root.winfo_screenwidth()-40)
        available_height=max(320,self.root.winfo_screenheight()-70)
        width=min(max(620,requested_width),available_width)
        height=min(requested_height,available_height)
        self.root.geometry(f"{width}x{height}")
        self.update_scroll_region()
    def draw(self):
        bg="#101827"; fg="#eef6ff"; accent="#7ee6ff"; body=self.body
        header=tk.Frame(body,bg=bg); header.pack(fill="x",padx=28,pady=(18,2))
        if self.brand_icon: tk.Label(header,image=self.brand_icon,bg=bg).pack(side="left",padx=(0,12))
        heading=tk.Frame(header,bg=bg); heading.pack(side="left")
        tk.Label(heading,text="DESKFLOW",font=("Segoe UI",24,"bold"),fg=accent,bg=bg).pack(anchor="w")
        tk.Label(heading,text=f"桌面整理 · AI 协作 · v{current_version()}",font=("Microsoft YaHei UI",10),fg="#9eb1c9",bg=bg).pack(anchor="w")
        box=tk.Frame(body,bg="#17243a",highlightbackground="#2c4a70",highlightthickness=1); box.pack(fill="x",padx=28,pady=12)
        tk.Label(box,text="运行概览",anchor="w",font=("Microsoft YaHei UI",9,"bold"),fg="#82dff0",bg="#17243a").pack(fill="x",padx=16,pady=(10,0))
        tk.Label(box,textvariable=self.status,justify="left",anchor="w",font=("Microsoft YaHei UI",11),fg=fg,bg="#17243a").pack(fill="x",padx=16,pady=(3,11))
        actions=tk.Frame(body,bg=bg); actions.pack(fill="x",padx=28)
        self.install=self.action_button(actions,"安装",self.install_app,tone="secondary",width=11); self.install.grid(row=0,column=0,padx=4,pady=4)
        self.enable=self.action_button(actions,"启用并显示",self.enable_app,tone="primary",width=11); self.enable.grid(row=0,column=1,padx=4,pady=4)
        self.stop=self.action_button(actions,"暂停显示",self.stop_app,tone="violet",width=11); self.stop.grid(row=0,column=2,padx=4,pady=4)
        self.remove=self.action_button(actions,"卸载",self.uninstall,tone="danger",width=11); self.remove.grid(row=0,column=3,padx=4,pady=4)
        prefs=tk.LabelFrame(body,text="外观偏好",fg="#a9dff0",bg=bg,highlightbackground="#314564",highlightthickness=1,padx=10,pady=5,font=("Microsoft YaHei UI",9,"bold"))
        prefs.pack(fill="x",padx=28,pady=(3,6))
        current_ui = self.settings_data()
        # People adjust the space each desktop item occupies, not a separate
        # font and icon recipe.  Keep legacy values only for MCP compatibility.
        self.cell_size=tk.IntVar(value=max(44, min(72, int(current_ui.get("cell_size", int(current_ui.get("icon_size", 28)) + 30)))))
        self.font_size=tk.IntVar(value=max(8, min(14, int(current_ui.get("font_size", 9)))))
        self.layout_footprint=tk.IntVar(value=round(max(.45, min(.95, float(current_ui.get("layout_footprint", .82)))) * 100))
        self.alpha=tk.IntVar(value=round(float(current_ui.get("alpha", .22)) * 100))
        scale_style={"bg":bg,"fg":"#c9d7e8","troughcolor":"#20344f","activebackground":"#7ee6ff","highlightthickness":0}
        tk.Label(prefs,text="单元格大小",fg=fg,bg=bg).grid(row=0,column=0,sticky="w"); tk.Scale(prefs,from_=44,to=72,variable=self.cell_size,orient="horizontal",length=230,**scale_style).grid(row=0,column=1,columnspan=3,sticky="w")
        tk.Label(prefs,text="底板不透明度",fg=fg,bg=bg).grid(row=0,column=4,sticky="w"); tk.Scale(prefs,from_=8,to=70,variable=self.alpha,orient="horizontal",length=115,**scale_style).grid(row=0,column=5)
        tk.Label(prefs,text="桌面占用",fg=fg,bg=bg).grid(row=1,column=0,sticky="w"); tk.Scale(prefs,from_=45,to=95,variable=self.layout_footprint,orient="horizontal",length=230,**scale_style).grid(row=1,column=1,columnspan=3,sticky="w")
        tk.Label(prefs,text="文字大小",fg=fg,bg=bg).grid(row=1,column=4,sticky="w"); tk.Scale(prefs,from_=8,to=14,variable=self.font_size,orient="horizontal",length=115,**scale_style).grid(row=1,column=5)
        self.action_button(prefs,"选背景色",self.choose_background,tone="secondary").grid(row=2,column=0,columnspan=2,pady=(5,0),sticky="w")
        self.action_button(prefs,"应用偏好",self.apply_preferences,tone="primary").grid(row=2,column=4,columnspan=2,pady=(5,0),sticky="e")
        tk.Label(prefs,text="底板透明度只影响玻璃背景；文字与图标始终保持完全不透明。",fg="#8fdff3",bg=bg).grid(row=3,column=0,columnspan=6,pady=(4,0),sticky="w")
        lower=tk.Frame(body,bg=bg); lower.pack(fill="x",padx=28,pady=3)
        self.auto=tk.BooleanVar(value=self.data.get("autostart",False))
        tk.Checkbutton(lower,text="登录后自动启用",variable=self.auto,command=self.set_autostart,fg=fg,bg=bg,selectcolor="#19263a",activebackground=bg,activeforeground=fg).pack(side="left")
        self.tray=tk.BooleanVar(value=self.data.get("minimize_to_tray",True)); tk.Checkbutton(lower,text="关闭时收进托盘",variable=self.tray,command=self.set_tray,fg=fg,bg=bg,selectcolor="#19263a",activebackground=bg,activeforeground=fg).pack(side="left",padx=16)
        self.action_button(lower,"打开整理器设置",self.open_settings,tone="secondary").pack(side="right")
        self.update_button=self.action_button(lower,"检查更新",self.check_for_updates,tone="secondary")
        self.update_button.pack(side="right",padx=(0,8))
        tray_row=tk.Frame(body,bg=bg); tray_row.pack(fill="x",padx=28,pady=3)
        tk.Label(tray_row,textvariable=self.tray_status,fg="#8fdff3",bg=bg,anchor="w",wraplength=450).pack(side="left",fill="x",expand=True)
        self.action_button(tray_row,"重试托盘",self.retry_tray,tone="secondary").pack(side="right")
        tk.Label(body,textvariable=self.update_status,fg="#8fdff3",bg=bg).pack(anchor="e",padx=28,pady=(0,3))
        recovery=tk.LabelFrame(body,text="屏幕恢复与排版",fg="#a9dff0",bg=bg,highlightbackground="#314564",highlightthickness=1,padx=10,pady=5,font=("Microsoft YaHei UI",9,"bold"))
        recovery.pack(fill="x",padx=28,pady=(0,4))
        self.monitor_choices = self.monitor_choices_for_manager()
        self.monitor_label = tk.StringVar(value=next(iter(self.monitor_choices)))
        self.monitor_picker = ttk.Combobox(recovery,textvariable=self.monitor_label,values=list(self.monitor_choices),state="readonly",width=28,style="Deskflow.TCombobox")
        self.monitor_picker.pack(side="left")
        self.action_button(recovery,"移动全部并弹性排版",self.move_all_and_arrange,tone="violet").pack(side="right")
        tk.Label(body,text="安装仅建立本机入口和配置；卸载不会删除桌面文件。托盘菜单可重新打开管理器、启用整理或退出并恢复原生图标。",wraplength=590,justify="left",fg="#94a6bf",bg=bg).pack(anchor="w",padx=28,pady=(4,8))
    @traced("manager.health_request")
    def online(self):
        try: urlopen("http://127.0.0.1:8765/health",timeout=0.8).read(); return True
        except Exception:
            logger.debug("manager.health_unavailable", exc_info=True)
            return False
    @traced("manager.api_post")
    def api_post(self, path: str):
        request = Request(f"http://127.0.0.1:8765{path}", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        return urlopen(request, timeout=1).read()
    def settings_data(self):
        try: return json.loads(urlopen("http://127.0.0.1:8765/layout",timeout=.8).read()).get("config", {}).get("ui", {})
        except Exception: return {}
    @staticmethod
    def monitor_choices_for_manager():
        choices = {"主屏幕": "primary"}
        for screen_number, (handle, _dc, _rect) in enumerate(win32api.EnumDisplayMonitors(), start=1):
            info = win32api.GetMonitorInfo(handle); left, top, right, bottom = info["Monitor"]
            if info["Flags"] & 1:
                continue
            label = f"屏幕 {screen_number} · {right-left}×{bottom-top}"
            choices[label] = f"monitor:{int(handle)}"
        return choices
    def apply_monitor_and_arrange(self, target: str, notify_failure: bool = True) -> bool:
        self.enable_app()
        # Starting the overlay is asynchronous.  Wait for its localhost API
        # before issuing the move; the old immediate request was the reason
        # the manager's screen-recovery button appeared to do nothing.
        for _ in range(15):
            if self.online():
                break
            time.sleep(.1)
        try:
            req=Request("http://127.0.0.1:8765/settings",data=json.dumps({"target_monitor": target}).encode(),method="POST",headers={"Content-Type":"application/json"})
            applied=json.loads(urlopen(req,timeout=2).read()).get("ui", {}).get("target_monitor")
            # Never silently turn an unrecognised selection into the primary
            # display.  The button must either move to the requested monitor
            # or report a concrete failure to the user.
            if applied != target:
                raise RuntimeError(f"target monitor was not accepted: {applied!r}")
            self.api_post("/arrange-top-right")
            return True
        except Exception as exc:
            if notify_failure:
                messagebox.showerror("DESKFLOW", f"未能移动到所选屏幕，原布局未被替换。\n{exc}",parent=self.root)
            return False
    def selected_monitor_target(self) -> tuple[str | None, str]:
        """Resolve the picker against a fresh monitor enumeration.

        HMONITOR values can change after an RDP reconnect.  A combobox may
        still contain its old display label, so resolve its screen-number
        prefix against the current enumeration instead of falling back to the
        primary display.
        """
        selection = self.monitor_picker.get().strip()
        fresh = self.monitor_choices_for_manager()
        self.monitor_choices = fresh
        self.monitor_picker.configure(values=list(fresh))
        target = fresh.get(selection)
        if target is None:
            prefix = selection.split("·", 1)[0].strip()
            target = next((value for label, value in fresh.items() if label.split("·", 1)[0].strip() == prefix), None)
        return target, selection
    def move_all_and_arrange(self):
        target, selection = self.selected_monitor_target()
        if target is None:
            messagebox.showerror("DESKFLOW", f"找不到“{selection}”。远程桌面屏幕列表已变化，请重新选择后再试。",parent=self.root)
            return
        if self.apply_monitor_and_arrange(target):
            self.status.set(f"显示状态：已移动全部块到 {selection} 并完成弹性排版")
    def refresh(self):
        # Localhost can still time out. Never make the Tk input thread wait for
        # an HTTP request, and keep only one probe / scheduled refresh active.
        if not getattr(self, "_health_pending", False):
            self._health_pending = True
            def probe():
                try:
                    live = self.online()
                except Exception:
                    logger.exception("manager.health_probe.failed")
                    live = False
                self.tray_actions.put(("health-result", live))
            threading.Thread(target=probe, name="deskflow-health", daemon=True).start()
        self.update_status_controls(getattr(self, "_online_cached", False))
        previous = getattr(self, "_refresh_scheduled", None)
        if previous:
            self.root.after_cancel(previous)
        self._refresh_scheduled = self.root.after(1500,self.refresh)

    def update_status_controls(self, live):
        installed=self.data.get("installed",False)
        self.status.set(f"安装状态：{'已安装' if installed else '未安装'}\n显示状态：{'正在运行（本机服务已连接）' if live else '未运行'}\n自动启用：{'已开启' if self.data.get('autostart') else '未开启'}")
        self.install.configure(state="disabled" if installed else "normal"); self.enable.configure(state="normal" if installed else "disabled")
        self.stop.configure(state="normal" if live else "disabled"); self.remove.configure(state="normal" if installed else "disabled")
    def check_for_updates(self, silent=False):
        if not FROZEN:
            self.update_status.set("源码版请使用 Git 同步更新，不安装 Release")
            return
        if self.update_busy: return
        self.update_silent = silent
        self.update_busy=True; self.update_button.configure(state="disabled"); self.update_status.set("正在检查 GitHub Release…")
        def worker():
            try: self.tray_actions.put(("update-result", latest_release()))
            except Exception as exc: self.tray_actions.put(("update-error", str(exc)))
        threading.Thread(target=worker, name="deskflow-update-check", daemon=True).start()
    def handle_update_result(self, release):
        if not FROZEN:
            self.update_busy=False; self.update_button.configure(state="normal")
            self.update_status.set("源码版请使用 Git 同步更新，不安装 Release")
            return
        tag=str(release.get("tag_name", ""))
        try: available=is_newer_version(tag)
        except Exception as exc:
            self.handle_update_error(str(exc)); return
        if not available:
            self.update_busy=False; self.update_button.configure(state="normal")
            self.update_status.set(f"当前版本 {current_version()}，已经是最新版")
            return
        if not messagebox.askyesno("发现 DESKFLOW 更新",f"当前版本：{current_version()}\n最新版本：{tag}\n\n是否下载、校验并安装更新？",parent=self.root):
            self.update_busy=False; self.update_button.configure(state="normal"); self.update_status.set(f"发现 {tag}，尚未安装")
            return
        self.update_status.set(f"正在下载并校验 {tag}…")
        def worker():
            try: self.tray_actions.put(("update-ready", str(download_and_verify(release))))
            except Exception as exc: self.tray_actions.put(("update-error", str(exc)))
        threading.Thread(target=worker, name="deskflow-update-download", daemon=True).start()
    def handle_update_error(self, detail: str):
        self.update_busy=False; self.update_button.configure(state="normal"); self.update_status.set("更新检查失败")
        if not getattr(self, "update_silent", False):
            messagebox.showerror("DESKFLOW 更新失败",detail,parent=self.root)
    def apply_downloaded_update(self, package: str):
        if not FROZEN:
            self.update_busy=False; self.update_button.configure(state="normal")
            self.update_status.set("源码版请使用 Git 同步更新，不安装 Release")
            return
        try:
            spawn_update(Path(package), os.getpid())
        except Exception as exc:
            self.handle_update_error(str(exc)); return
        self.update_status.set("更新已校验，正在重启安装…")
        self.exit_desktopflow()
    def install_app(self):
        self.data["installed"]=True
        # Inno owns frozen shortcuts and registry entries (including uninstall).
        # Rewriting them through WScript during every startup can block on a
        # modal COM warning before the overlay has even been launched.
        if not FROZEN:
            self.register_context_menu()
            self.create_desktop_shortcut()
        save(self.data)
        self.refresh()

    def create_desktop_shortcut(self):
        """Create a discoverable desktop entry without exposing a cmd window."""
        try:
            DESKTOP_SHORTCUT.parent.mkdir(parents=True, exist_ok=True)
            shortcut = Dispatch("WScript.Shell").CreateShortcut(str(DESKTOP_SHORTCUT))
            launch = command("manager", "--start", "--show")
            shortcut.TargetPath = launch[0]
            shortcut.Arguments = subprocess.list2cmdline(launch[1:])
            shortcut.WorkingDirectory = str(APP_ROOT)
            shortcut.IconLocation = f"{ICON_ICO},0" if ICON_ICO.exists() else f"{gui_python()},0"
            shortcut.Description = "打开 DESKFLOW 智能桌面整理管理器"
            shortcut.Save()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            messagebox.showwarning("DESKFLOW", f"已完成安装，但未能创建桌面快捷方式：{exc}", parent=self.root)

    def register_context_menu(self):
        key = r"Software\Classes\DesktopBackground\Shell\DESKFLOW"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as menu:
            winreg.SetValueEx(menu, None, 0, winreg.REG_SZ, "DESKFLOW 图标整理管理器")
            winreg.SetValueEx(menu, "Icon", 0, winreg.REG_SZ, str(ICON_ICO if ICON_ICO.exists() else Path(sys.executable)))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as command:
            from app_runtime import command as launch_command
            winreg.SetValueEx(command, None, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_command("manager", "--start", "--show")))

    def remove_context_menu(self):
        key = r"Software\Classes\DesktopBackground\Shell\DESKFLOW"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key + r"\command"); winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError: pass
    @traced("manager.enable_app")
    def enable_app(self):
        if not self.data.get("installed"): self.install_app()
        if not self.online():
            proc=subprocess.Popen(command("overlay", *logging_arguments()),cwd=APP_ROOT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            self.data["pid"]=proc.pid; save(self.data)
        self.refresh()
    @traced("manager.open_settings")
    def open_settings(self):
        if getattr(self, "_settings_pending", False):
            return
        if not self.data.get("installed"):
            self.install_app()
        self._settings_pending = True
        def request_settings():
            pid = None
            try:
                if not self.online():
                    pid = subprocess.Popen(command("overlay", *logging_arguments()), cwd=APP_ROOT,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).pid
                for _ in range(15):
                    if self.online():
                        break
                    time.sleep(.1)
                self.api_post("/open-settings")
                self.tray_actions.put(("settings-result", (pid, None)))
            except Exception as error:
                logger.exception("manager.open_settings.failed")
                self.tray_actions.put(("settings-result", (pid, str(error))))
        threading.Thread(target=request_settings, name="deskflow-settings-request", daemon=True).start()
    @traced("manager.apply_preferences")
    def apply_preferences(self):
        payload={"cell_size":self.cell_size.get(),"font_size":self.font_size.get(),"layout_footprint":self.layout_footprint.get()/100,"alpha":self.alpha.get()/100}
        if self.data.get("background"): payload["background"]=self.data["background"]
        if getattr(self, "_preferences_pending", False):
            self._preferences_latest = payload
            return
        if not self.data.get("installed"):
            self.install_app()
        self.request_preferences(payload)

    def request_preferences(self, payload):
        self._preferences_pending = True
        self._preferences_latest = None
        def request():
            pid = None
            try:
                if not self.online():
                    pid = subprocess.Popen(command("overlay", *logging_arguments()), cwd=APP_ROOT,
                                           creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).pid
                for _ in range(15):
                    if self.online():
                        break
                    time.sleep(.1)
                with timed_operation("manager.preferences_request"):
                    req=Request("http://127.0.0.1:8765/settings",data=json.dumps(payload).encode(),method="POST",headers={"Content-Type":"application/json"})
                    with urlopen(req,timeout=2) as response:
                        response.read()
                self.tray_actions.put(("preferences-result", (pid, None)))
            except Exception as error:
                logger.exception("manager.apply_preferences.failed")
                self.tray_actions.put(("preferences-result", (pid, str(error))))
        threading.Thread(target=request, name="deskflow-preferences", daemon=True).start()
    def choose_background(self):
        color=colorchooser.askcolor(color=self.data.get("background","#101827"),parent=self.root)[1]
        if color: self.data["background"]=color; save(self.data)
    def stop_app(self):
        try: self.api_post("/show-native-icons"); time.sleep(.4)
        except Exception: pass
        pid=self.data.get("pid")
        if pid:
            try: subprocess.run(["taskkill","/PID",str(pid),"/T","/F"],capture_output=True)
            except OSError: pass
        self.data["pid"]=None; save(self.data); self.refresh()
    def clean_restart_paths(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return STATE_DIR / "clean-restart.ps1", STATE_DIR / "clean-restart-plan.json"
    def write_clean_restart_launcher(self) -> Path:
        """Write a detached launcher that rebuilds the manager from a clean parent."""
        script, plan = self.clean_restart_paths()
        launch = command("manager", "--start")
        payload = {"wait_pid": os.getpid(), "executable": launch[0], "arguments": list(launch[1:]), "cwd": str(APP_ROOT)}
        plan.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        script.write_text(CLEAN_RESTART_SCRIPT, encoding="utf-8-sig", newline="\r\n")
        return script
    def spawn_clean_restart(self) -> None:
        script = self.write_clean_restart_launcher()
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        subprocess.Popen(
            [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    def restart_clean(self):
        """Restart through a clean parent so an inherited RedirectionTrust policy is dropped."""
        flags = process_redirection_flags(os.getpid())
        if flags is None:
            state = "未能读取当前实例的启动策略。"
        elif flags & REDIRECTION_TRUST_ENFORCE:
            state = "当前实例带有「重定向信任」策略，这是第三方程序报 system:448 的根因，重启后会清除。"
        else:
            state = "当前实例的启动策略正常，重启只用于刷新。"
        prompt = (
            "将先恢复原生桌面图标，再用干净上下文重启 DESKFLOW。\n"
            "分类与布局不会改变，重启期间整理块会短暂消失。\n\n"
            + state + "\n\n是否继续？"
        )
        if not messagebox.askyesno("修复启动上下文并重启", prompt, parent=self.root):
            return
        try:
            self.spawn_clean_restart()
        except OSError as error:
            logger.exception("manager.restart_clean.launch_failed")
            messagebox.showerror("修复启动上下文", f"无法启动清理器：{error}", parent=self.root)
            return
        logger.info("manager.restart_clean requested flags=%s", flags)
        self.exit_desktopflow()
    def set_autostart(self):
        self.data["autostart"]=bool(self.auto.get())
        if self.auto.get():
            self.write_autostart_entry()
        elif STARTUP.exists(): STARTUP.unlink()
        save(self.data); self.refresh()
    @staticmethod
    def write_autostart_entry():
        """Login must enter through the manager so the tray is always alive."""
        STARTUP.parent.mkdir(parents=True,exist_ok=True)
        STARTUP.write_bytes(('@echo off\r\nchcp 65001 >nul\r\nstart "" ' + subprocess.list2cmdline(command("manager", "--start")) + '\r\n').encode("utf-8"))
    def set_tray(self):
        self.data["minimize_to_tray"]=bool(self.tray.get()); save(self.data)
    @staticmethod
    def tray_image():
        try:
            image = Image.open(ICON_PNG).convert("RGBA")
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            return image
        except OSError:
            pass
        image=Image.new("RGBA",(64,64),(0,0,0,0)); draw=ImageDraw.Draw(image)
        draw.rounded_rectangle((5,5,59,59),radius=15,fill="#101827",outline="#7ee6ff",width=3)
        draw.rectangle((17,18,47,23),fill="#7ee6ff"); draw.rectangle((17,29,41,34),fill="#a98bff"); draw.rectangle((17,40,35,45),fill="#68d8a7")
        return image
    def show_manager(self, *_args):
        self.tray_actions.put(("show", None))
    def tray_arrange_current(self, *_args):
        self.tray_actions.put(("arrange-current", None))
    def tray_move_and_arrange(self, target: str):
        return lambda *_args: self.tray_actions.put(("move-and-arrange", target))
    def enqueue_tray_action(self, action: str):
        return lambda *_args: self.tray_actions.put((action, None))
    def process_tray_actions(self):
        """Run all tray commands on Tk's main thread, never pystray's thread."""
        while True:
            try: action, target = self.tray_actions.get_nowait()
            except queue.Empty: break
            if action == "show":
                self._tray_hide_pending = False
                self.root.deiconify(); self.root.lift(); self.root.focus_force()
            elif action == "tray-result":
                icon, registered, detail = target
                if icon is not self.tray_icon:
                    continue
                self._tray_ready = bool(registered and icon.registered and icon.worker_alive())
                if self._tray_ready:
                    self.tray_status.set("通知区域图标已注册；可能位于折叠区")
                    if self._tray_hide_pending:
                        self._tray_hide_pending = False
                        self.root.withdraw()
                else:
                    logger.warning("tray.unavailable detail=%s", detail)
                    self.tray_status.set("通知区域暂不可用，窗口保持打开；可点击“重试托盘”")
                    self.root.deiconify()
            elif action == "enable":
                self.enable_app()
            elif action == "arrange-current":
                self.apply_monitor_and_arrange(self.settings_data().get("target_monitor", "primary"), notify_failure=True)
            elif action == "move-and-arrange":
                self.apply_monitor_and_arrange(target or "primary", notify_failure=True)
            elif action == "stop":
                self.stop_app()
            elif action == "check-update":
                self.root.deiconify(); self.root.lift(); self.check_for_updates()
            elif action == "repair-public-icons":
                try:
                    from public_desktop_icons import launch_elevated
                    launch_elevated()
                except Exception as exc:
                    messagebox.showerror("公共桌面权限", str(exc), parent=self.root)
            elif action == "update-result":
                self.handle_update_result(target)
            elif action == "health-result":
                self._health_pending = False
                self._online_cached = bool(target)
                self.update_status_controls(bool(target))
            elif action == "settings-result":
                self._settings_pending = False
                pid, error = target
                if pid:
                    self.data["pid"] = pid
                    save(self.data)
                if error:
                    messagebox.showinfo("DESKFLOW", f"未能打开设置，请重试。\n{error}", parent=self.root)
            elif action == "preferences-result":
                self._preferences_pending = False
                pid, error = target
                if pid:
                    self.data["pid"] = pid
                    save(self.data)
                if error:
                    messagebox.showinfo("DESKFLOW", f"未能应用偏好，请重试。\n{error}", parent=self.root)
                latest = self._preferences_latest
                if latest is not None:
                    self.request_preferences(latest)
            elif action == "update-error":
                self.handle_update_error(str(target))
            elif action == "update-ready":
                self.apply_downloaded_update(str(target)); return
            elif action == "restart-clean":
                self.restart_clean(); return
            elif action == "exit":
                self.exit_desktopflow(); return
        if self.root.winfo_exists(): self.root.after(80, self.process_tray_actions)
    def tray_menu_items(self):
        monitors = self.monitor_choices_for_manager()
        monitor_menu = pystray.Menu(*(
            pystray.MenuItem(label, self.tray_move_and_arrange(target))
            for label, target in monitors.items()
        ))
        return (
            pystray.MenuItem("打开 DESKFLOW 管理器",self.show_manager,default=True),
            pystray.MenuItem("启用桌面整理",self.enqueue_tray_action("enable")),
            pystray.MenuItem("当前屏幕弹性重排",self.tray_arrange_current),
            pystray.MenuItem("移动全部到指定屏幕并弹性排版",monitor_menu),
            pystray.MenuItem("暂停并显示原生图标",self.enqueue_tray_action("stop")),
            pystray.MenuItem("修复启动上下文并重启（system:448 时用）",self.enqueue_tray_action("restart-clean")),
            pystray.MenuItem("修复公共桌面重复图标（需要管理员确认）",self.enqueue_tray_action("repair-public-icons")),
            pystray.MenuItem("检查 GitHub Release 更新",self.enqueue_tray_action("check-update")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 DESKFLOW",self.enqueue_tray_action("exit")),
        )
    def ensure_tray(self):
        if self.tray_icon and self.tray_icon.worker_alive():
            return self._tray_ready and self.tray_icon.registered
        # Rebuild on every open, so monitors added during an RDP session are
        # available immediately without restarting the tray manager.
        menu=pystray.Menu(self.tray_menu_items)
        self._tray_ready = False
        try:
            self.tray_icon=RegisteredTrayIcon("DESKFLOW",self.tray_image(),"DESKFLOW 智能桌面整理",menu,
                report=lambda icon, ready, detail: self.tray_actions.put(("tray-result", (icon, ready, detail))))
            self.tray_icon.run_detached()
        except Exception as exc:
            logger.exception("tray.create failed")
            self.tray_icon = None
            self.tray_status.set(f"托盘创建失败，管理器保持可见：{exc}")
            self.root.deiconify()
        return False

    def retry_tray(self):
        """Explicit user retry; reuse a living thread, replace only a dead one."""
        self.tray_status.set("正在重试通知区域图标…")
        if self.tray_icon and self.tray_icon.worker_alive():
            if not self.tray_icon.retry_registration():
                self.tray_status.set("托盘线程尚未就绪，请稍后重试")
            return
        self.ensure_tray()

    def hide_to_tray(self):
        if self.tray.get():
            self._tray_hide_pending = True
            if self.ensure_tray():
                self._tray_hide_pending = False
                self.root.withdraw()
            else:
                self.root.deiconify()
                self.tray_status.set("托盘未就绪，窗口保持可见；可点击“重试托盘”")
        else: self.exit_desktopflow()
    def exit_desktopflow(self):
        self.stop_app()
        if self.tray_icon: self.tray_icon.stop(); self.tray_icon=None
        self.root.destroy()
    def uninstall(self):
        if FROZEN:
            subprocess.Popen([str(APP_ROOT / "unins000.exe")])
            self.exit_desktopflow()
            return
        if not messagebox.askyesno("确认卸载","将关闭整理器并删除它的本机配置和自动启动入口；不会删除桌面文件。是否继续？",parent=self.root): return
        self.stop_app()
        if STARTUP.exists(): STARTUP.unlink()
        if DESKTOP_SHORTCUT.exists(): DESKTOP_SHORTCUT.unlink()
        self.remove_context_menu()
        if STATE.exists(): STATE.unlink()
        self.data={"installed":False,"pid":None,"autostart":False}; save(self.data); self.auto.set(False); self.refresh()
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--install",action="store_true")
    parser.add_argument("--start",action="store_true",help="start the manager and desktop blocks")
    parser.add_argument("--show", action="store_true", help="show the manager window")
    parser.add_argument("--log-level", choices=("debug", "info", "warn", "error"), help="enable local 200 KiB rotating diagnostics beside the program")
    args,_unknown=parser.parse_known_args()
    configure_logging(args.log_level, role="manager")
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    show_event = kernel32.CreateEventW(None, False, False, "Local\\DeskflowOrganizer.ShowManager")
    if not show_event:
        raise ctypes.WinError()
    kernel32.SetLastError(0)
    instance_mutex = kernel32.CreateMutexW(None, False, "Local\\DeskflowOrganizer.Manager")
    if not instance_mutex or kernel32.GetLastError() == 183:
        # A tray manager already owns the lifecycle.  The organizer itself has
        # a second mutex, so a double click can never create duplicate blocks.
        if instance_mutex and args.show:
            kernel32.SetEvent(show_event)
        if instance_mutex:
            kernel32.CloseHandle(instance_mutex)
        kernel32.CloseHandle(show_event)
        raise SystemExit(0)
    try:
        manager=Manager()
        def poll_show_request():
            if kernel32.WaitForSingleObject(show_event, 0) == 0:
                manager.show_manager()
            if manager.root.winfo_exists():
                manager.root.after(80, poll_show_request)
        manager.root.after(80, poll_show_request)
        if args.show:
            manager.root.after_idle(manager.show_manager)
        if FROZEN: print('Manager initialized.', flush=True)
        if args.install or args.start:
            manager.install_app()
            if FROZEN: print('Manager installation entries registered.', flush=True)
        if args.install or args.start:
            manager.enable_app()
            if FROZEN: print('Overlay start requested.', flush=True)
        manager.root.mainloop()
    finally:
        kernel32.CloseHandle(instance_mutex)
        kernel32.CloseHandle(show_event)

if __name__ == "__main__":
    main()
