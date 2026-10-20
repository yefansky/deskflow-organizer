"""Windowless application dispatcher; MCP uses its own console executable."""
import sys


def main():
    if "--smoke-test" in sys.argv:
        # Load actual frozen dependencies and create both UI runtimes without
        # starting the overlay, changing desktop attributes or user settings.
        import json
        from pathlib import Path
        import tkinter
        import deskflow_manager
        import smart_desktop
        import qt_desktop
        import native_icon_guard
        from PySide6.QtWidgets import QApplication, QWidget
        from app_runtime import RESOURCE_ROOT
        from release_updater import current_version
        root = tkinter.Tk(); root.withdraw(); root.update(); root.destroy()
        # Construct the real manager and tray too. Keep its state isolated and
        # disable HTTP reads; never stop or alter the user's existing overlay.
        import tempfile
        with tempfile.TemporaryDirectory() as state:
            deskflow_manager.STATE_DIR = Path(state)
            deskflow_manager.STATE = Path(state) / 'manager.json'
            deskflow_manager.Manager.online = lambda self: False
            deskflow_manager.Manager.settings_data = lambda self: {}
            manager = deskflow_manager.Manager()
            try:
                manager.root.after(300, manager.root.quit)
                manager.root.mainloop()
            finally:
                if manager.tray_icon: manager.tray_icon.stop()
                manager.root.destroy()
        app = QApplication([]); widget = QWidget(); widget.resize(120, 80); app.processEvents(); widget.close()
        assert (RESOURCE_ROOT / "assets" / "deskflow-icon-v1.ico").is_file()
        assert current_version() != "0.0.0"
        output = Path(sys.argv[sys.argv.index("--smoke-test") + 1])
        output.write_text(json.dumps({"ok": True, "version": current_version(), "tk": True, "qt": True, "manager_and_tray": True}), encoding="utf-8")
        return
    if "--overlay" in sys.argv:
        sys.argv.remove("--overlay")
        from smart_desktop import main as run
    elif "--public-icons" in sys.argv:
        sys.argv.remove("--public-icons")
        from public_desktop_icons import main as run
    elif "--guard" in sys.argv:
        sys.argv.remove("--guard")
        from native_icon_guard import main as run
    else:
        from deskflow_manager import main as run
    run()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if "--smoke-test" in sys.argv:
        try:
            main()
        except Exception:
            import traceback
            from pathlib import Path
            Path(sys.argv[sys.argv.index("--smoke-test") + 1]).with_suffix('.error.txt').write_text(traceback.format_exc(), encoding='utf-8')
            raise SystemExit(1)
    else:
        import os
        import traceback
        from pathlib import Path
        state = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'DeskflowOrganizer'
        state.mkdir(parents=True, exist_ok=True)
        log = (state / 'runtime.log').open('a', encoding='utf-8', buffering=1)
        if sys.stderr is None: sys.stderr = log
        if sys.stdout is None: sys.stdout = log
        try:
            print('Starting DESKFLOW:', sys.argv[1:], flush=True)
            main()
        except Exception:
            traceback.print_exc(file=log)
            log.flush()
            raise SystemExit(1)
