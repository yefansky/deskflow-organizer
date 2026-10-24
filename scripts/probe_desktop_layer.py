"""Opt-in Windows desktop-layer integration probe; never uses the input desktop.

Run with --run-private-desktop. Creates its own noninput desktop, two synthetic
application/modal windows and a synthetic Explorer-like root/DefView. Imports
actual product UI/Shell methods without instrumenting or replacing them. The
Explorer identity predicate alone is bypassed for this synthetic host. Fixture
catalog/store/icon providers and native-icon attribute writes are replaced by
memory-only fakes; actual HWND PID, DPI, child parent, Z order, visibility,
geometry and modal WindowFromPoint checks remain real. No SwitchDesktop,
production process startup, real Explorer changes, desktop file access or HTTP
writes occur. A timeout terminates only the child process created by this probe.

Twelve phases cover creation, repeated show/raise/activate, controller/MCP move,
collision rollback, synthetic drag events, manual child resize, appearance
rebuild, settings cancellation, footprint layout, unsafe-host fail-closed,
safe recovery, and native HWND recreation. This does not prove actual Explorer
identity, DWM alpha compositing, real pointer input or mixed-monitor DPI.
Logs and JSON evidence go under --output-dir; TEMP/TMP use its tmp directory.
Not part of automatic CI because CreateDesktop/Win32 Qt support are required.
"""
import os, sys, json, ctypes, subprocess, queue
from pathlib import Path
REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('DESKFLOW_PROBE_OUTPUT', str(REPOSITORY / 'build' / 'desktop-layer-probe'))).resolve()
sys.path.insert(0, str(REPOSITORY / 'scripts'))

def worker():
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    import win32gui, win32con, win32api
    from PySide6.QtCore import Qt, QPoint, QRect, QPointF
    from PySide6.QtGui import QPixmap, QIcon, QColor, QWindow
    from PySide6.QtWidgets import QApplication, QMessageBox, QSpinBox
    from qt_desktop import QtOrganizerUI, PanelWindow
    from smart_desktop import DesktopShell
    from types import SimpleNamespace
    from diagnostics import configure_logging, get_logger
    user = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    user.GetThreadDesktop.restype = ctypes.c_void_p
    desktop = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    buf = ctypes.create_unicode_buffer(256)
    n = ctypes.c_ulong()
    user.GetUserObjectInformationW(ctypes.c_void_p(desktop), 2, buf, ctypes.sizeof(buf), ctypes.byref(n))
    if buf.value != sys.argv[-1] or not buf.value.startswith('DeskflowProbePrivate'):
        raise RuntimeError('refusing to create windows outside the private fixture desktop')
    app = QApplication([])
    configure_logging('debug', ROOT, role='desktop-layer-probe')
    logger = get_logger('independent')
    instance = win32api.GetModuleHandle(None)
    cls = win32gui.WNDCLASS()
    cls.hInstance = instance
    cls.lpszClassName = 'DeskflowPrivateIntegrationHost'
    cls.lpfnWndProc = win32gui.DefWindowProc
    win32gui.RegisterClass(cls)
    handles = []

    def make(title, x, y, w, h, parent=0, child=False):
        h = win32gui.CreateWindowEx(win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW, cls.lpszClassName, title, win32con.WS_VISIBLE | (win32con.WS_CHILD if child else win32con.WS_POPUP), x, y, w, h, parent, 0, instance, None)
        handles.append(h)
        return h
    host = make('synthetic-root', -700, 90, 1600, 950)
    defview = make('synthetic-defview', 0, 0, 1600, 950, host, True)
    ordinary = make('synthetic-app', -670, 110, 500, 450)
    modal = make('synthetic-modal', -650, 130, 420, 340, ordinary)
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER
    win32gui.SetWindowPos(host, win32con.HWND_BOTTOM, 0, 0, 0, 0, flags)
    win32gui.SetWindowPos(modal, win32con.HWND_TOP, 0, 0, 0, 0, flags)
    shell = object.__new__(DesktopShell)
    shell.user32 = user
    shell.kernel32 = kernel
    shell.host = host
    shell.panel_windows = {}
    shell.desktop_available = True
    shell._host_identity_valid = lambda: True
    shell._find_overlay_host = lambda: host
    monitor = {'left': -700, 'top': 90, 'right': 900, 'bottom': 1040}
    shell.target_monitor = lambda _: monitor
    shell.reconcile_target_monitor = lambda _: False
    shell.retry_icon_attributes = lambda: None
    restores = []
    hides = []
    shell.restore_icons = lambda: restores.append('restore')
    shell.hide_icons = lambda paths: hides.append(len(paths))
    profile = {'auto_layout': False, 'panel_positions': {'a': '+-650+150', 'b': '+-300+150'}, 'panel_sizes': {'a': {'width': 280, 'height': 230}, 'b': {'width': 280, 'height': 230}}}
    data = {'ui': {'background': '#101827', 'alpha': 0.22, 'layout_profiles': {'1600x950@-700,90': profile}}, 'category_modes': {}, 'tip_summaries': {}}
    saved = []
    categories = [SimpleNamespace(id=x, title=x, color='#55bbaa', mode='list') for x in ['a', 'b']]
    groups = {x: [{'name': 'synthetic-' + x + '.txt', 'path': str(ROOT / ('synthetic-' + x + '.txt')), 'kind': 'txt', 'modified': 'synthetic'}] for x in ['a', 'b']}
    ui = object.__new__(QtOrganizerUI)
    ui.app = app
    ui.shell = shell
    ui.model = SimpleNamespace(store=SimpleNamespace(data=data, save=lambda: saved.append('save')), scan=lambda: groups, sorted_items=lambda _, items: items, desktop_signature=lambda: ())
    ui.categories_provider = lambda _: categories
    ui.panels = {}
    ui.selected_item = None
    ui.group_snapshot = {}
    ui._groups = groups
    ui.last_work_bounds = None
    ui.last_desktop_signature = ()
    ui.events = queue.Queue()
    ui._settings_preview = False
    ui._settings_dialog = None
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor('#55bbaa'))
    ui.icon_for = lambda *a: QIcon(pixmap)
    ui.invalidate_icon = lambda _: None
    stages = []
    warnings = []
    initial_foreground = win32gui.GetForegroundWindow()
    QMessageBox.warning = lambda *a: warnings.append('warning')

    def check(label):
        app.processEvents()
        order = []
        win32gui.EnumWindows(lambda h, _: order.append(h), None)
        records = []
        for name, p in ui.panels.items():
            hwnd = int(p.winId())
            screen = p.screen_geometry()
            native = win32gui.GetWindowRect(hwnd)
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 4294967295
            records.append({'id': name, 'real_child': shell.panel_is_desktop_child(hwnd), 'visible': p.isVisible() and bool(win32gui.IsWindowVisible(hwnd)), 'screen_matches_native': [screen.x(), screen.y(), screen.width(), screen.height()] == [native[0], native[1], native[2] - native[0], native[3] - native[1]], 'noactivate': bool(ex & win32con.WS_EX_NOACTIVATE), 'non_topmost': not bool(ex & win32con.WS_EX_TOPMOST)})
        result = {'stage': label, 'panels': records, 'host_below_modal': order.index(host) > order.index(modal), 'modal_hit': win32gui.WindowFromPoint((-500, 250)) == modal, 'foreground_unchanged': win32gui.GetForegroundWindow() == initial_foreground, 'restore_count': len(restores), 'hide_requests': len(hides)}
        stages.append(result)
        assert result['foreground_unchanged'], 'fixture changed foreground'
        if label != 'unsafe-host-failclosed':
            assert result['host_below_modal'] and result['modal_hit'], 'panel host occludes modal'
            assert all((all((value for key, value in row.items() if key != 'id')) for row in records)), 'panel invariant failed'
        logger.info('candidate.stage %s', json.dumps(result))
        return result
    try:
        ui.render()
        initial = check('first-render')
        assert len(ui.panels) == 2 and all((all((v for k, v in r.items() if k != 'id')) for r in initial['panels'])) and initial['host_below_modal']
        a, b = (ui.panels['a'], ui.panels['b'])
        b_handle = int(b.winId())
        b_rect = b.screen_geometry()
        a.hide()
        a.show()
        a.raise_()
        a.activateWindow()
        repeat = check('repeat-show-raise-activate')
        assert repeat['host_below_modal'] and all((r['visible'] and r['real_child'] and r['screen_matches_native'] for r in repeat['panels']))
        ui.move_panel('a', -640, 170)
        assert a.screen_geometry().topLeft() == QPoint(-640, 170)
        assert profile['panel_positions']['a'] == '+-640+170'
        assert b.screen_geometry() == b_rect
        check('MCP-screen-move')
        ui.move_panel('a', -300, 150)
        assert a.screen_geometry().topLeft() == QPoint(-640, 170)
        assert b.screen_geometry() == b_rect
        check('collision-rollback')

        class Event:

            def __init__(self, globalpoint, local=QPoint(10, 10)):
                self.g = QPointF(globalpoint)
                self.p = QPointF(local)

            def button(self):
                return Qt.MouseButton.LeftButton

            def globalPosition(self):
                return self.g

            def position(self):
                return self.p

            def accept(self):
                pass
        press = a.screen_geometry().topLeft() + QPoint(10, 10)
        a.header.mousePressEvent(Event(press))
        a.header.mouseMoveEvent(Event(press + QPoint(10, 15)))
        a.header.mouseReleaseEvent(Event(press + QPoint(10, 15)))
        assert a.screen_geometry().topLeft() == QPoint(-630, 185)
        assert profile['panel_positions']['a'] == '+-630+185'
        check('header-drag-screen-save')
        start = a.screen_geometry()
        a._manual_resize = (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, start.bottomRight(), start)
        a.mouseMoveEvent(Event(start.bottomRight() + QPoint(20, 25)))
        a._manual_resize = None
        assert a.screen_geometry().size().width() == 300 and a.screen_geometry().height() == 255
        assert ui.finish_panel_geometry('a', start)
        assert b.screen_geometry() == b_rect
        check('manual-resize-screen-save')
        data['category_modes']['a'] = 'cards'
        ui.render('a')
        a = ui.panels['a']
        assert int(ui.panels['b'].winId()) == b_handle and shell.panel_is_desktop_child(b_handle) and win32gui.IsWindow(host)
        check('mode-rebuild-survivor-preserved')
        before_settings = json.loads(json.dumps(data['ui']))
        rects = {k: p.screen_geometry() for k, p in ui.panels.items()}
        ui.settings()
        dialog = ui._settings_dialog
        density, font = dialog.findChildren(QSpinBox)
        font.setValue(12)
        ui.preview_settings_layout({'font_size': 12})
        assert win32gui.GetParent(int(dialog.winId())) != defview
        dialog.reject()
        app.processEvents()
        assert data['ui'] == before_settings
        assert all((p.screen_geometry() == rects[k] for k, p in ui.panels.items()))
        check('settings-cancel-screen-restore')
        ui.preview_footprint_layout(0.75)
        check('footprint-screen-layout')
        win32gui.SetWindowPos(host, win32con.HWND_TOP, 0, 0, 0, 0, flags)
        before_restore = len(restores)
        shell.keep_panels_above_desktop_backdrop()
        assert all((not win32gui.IsWindowVisible(int(p.winId())) for p in ui.panels.values()))
        assert len(restores) > before_restore
        DesktopShell.hide_icons(shell, [str(ROOT / 'synthetic-must-not-hide.txt')])
        assert not hasattr(shell, 'organized_native_paths')
        check('unsafe-host-failclosed')
        win32gui.SetWindowPos(host, win32con.HWND_BOTTOM, 0, 0, 0, 0, flags)
        win32gui.SetWindowPos(modal, win32con.HWND_TOP, 0, 0, 0, 0, flags)
        ui.tick()
        recovered = check('safe-host-recovery')
        assert recovered['host_below_modal'] and all((r['visible'] and r['real_child'] for r in recovered['panels']))
        a = ui.panels['a']
        old = int(a.winId())
        a.windowHandle().destroy()
        app.processEvents()
        a.winId()
        app.processEvents()
        ui.tick()
        app.processEvents()
        ui.tick()
        check('HWND-rebuild-recovery')
        assert all((shell.panel_is_desktop_child(int(p.winId())) for p in ui.panels.values()))
        assert win32gui.IsWindow(host)
        result = {'private_desktop': buf.value, 'identity_validation_only_bypass': '_host_identity_valid=lambda:True for synthetic non-Explorer host', 'other_real_checks': ['native HWND PID', 'DPI context preflight', 'WS_CHILD/parent', 'actual Z order', 'native geometry', 'Win32 visibility'], 'production_or_input_desktop_used': False, 'assertions_passed': True, 'stages': stages, 'warnings': warnings, 'saved_count': len(saved)}
        (ROOT / 'desktop-layer-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    finally:
        ui._destroy_all()
        app.processEvents()
        for h in reversed(handles):
            if win32gui.IsWindow(h):
                win32gui.DestroyWindow(h)
        configure_logging(None)

def parent():
    import win32process, win32event, win32api
    u = ctypes.WinDLL('user32', use_last_error=True)
    u.CreateDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    u.CreateDesktopW.restype = ctypes.c_void_p
    u.CloseDesktop.argtypes = [ctypes.c_void_p]
    name = 'DeskflowProbePrivate' + str(os.getpid())
    desktop = u.CreateDesktopW(name, None, None, 0, 268435456, None)
    if not desktop:
        raise RuntimeError('private desktop unavailable; no fallback')
    try:
        si = win32process.STARTUPINFO()
        si.lpDesktop = 'WinSta0\\' + name
        p, t, pid, tid = win32process.CreateProcess(None, subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve()), '--worker', name]), None, None, False, 134217728, None, str(ROOT), si)
        try:
            if win32event.WaitForSingleObject(p, 30000) != 0:
                win32api.TerminateProcess(p, 2)
                raise RuntimeError('private fixture timeout')
            assert win32process.GetExitCodeProcess(p) == 0, 'candidate fixture failed; inspect synthetic diagnostic log'
        finally:
            t.Close()
            p.Close()
        print('PASS: 12 private-desktop integration stages; evidence: ' + str(ROOT / 'desktop-layer-result.json'))
    finally:
        u.CloseDesktop(desktop)

def main():
    global ROOT
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private-desktop', action='store_true', help='Explicitly permit creation of a noninput desktop and synthetic fixture windows; no production GUI or Explorer changes.')
    parser.add_argument('--output-dir', type=Path, default=ROOT, help='Evidence/log/TEMP directory (default: repository build/desktop-layer-probe).')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('desktop_name', nargs='?', help=argparse.SUPPRESS)
    args = parser.parse_args()
    ROOT = args.output_dir.resolve()
    if not args.worker and (not args.run_private_desktop):
        parser.print_help()
        return
    if not __debug__:
        parser.error('Do not use python -O: regression assertions must remain enabled.')
    if os.name != 'nt':
        parser.error('Windows is required; no offscreen or input-desktop fallback.')
    ROOT.mkdir(parents=True, exist_ok=True)
    temp = ROOT / 'tmp'
    temp.mkdir(exist_ok=True)
    os.environ['DESKFLOW_PROBE_OUTPUT'] = str(ROOT)
    os.environ['TEMP'] = str(temp)
    os.environ['TMP'] = str(temp)
    if '--worker' in sys.argv:
        try:
            worker()
        except BaseException:
            import traceback
            (ROOT / 'desktop-layer-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
            raise
    else:
        parent()
if __name__ == '__main__':
    main()
