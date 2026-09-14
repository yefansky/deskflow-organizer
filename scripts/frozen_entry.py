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
        app = QApplication([]); widget = QWidget(); widget.resize(120, 80); app.processEvents(); widget.close()
        assert (RESOURCE_ROOT / "assets" / "deskflow-icon-v1.ico").is_file()
        assert current_version() != "0.0.0"
        output = Path(sys.argv[sys.argv.index("--smoke-test") + 1])
        output.write_text(json.dumps({"ok": True, "version": current_version(), "tk": True, "qt": True}), encoding="utf-8")
        return
    if "--overlay" in sys.argv:
        sys.argv.remove("--overlay")
        from smart_desktop import main as run
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
        main()
