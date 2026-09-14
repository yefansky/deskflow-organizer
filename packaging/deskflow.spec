# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

# Third-party tools on a developer PATH can supply DLLs with Windows system
# names (for example Poppler's icuuc.dll). Qt expects the Windows ICU ABI.
# Resolve dependencies only from this Python/venv, Qt's hooks and Windows.
windows = Path(os.environ['SystemRoot'])
os.environ['PATH'] = os.pathsep.join(map(str, [Path(sys.executable).parent, Path(sys.base_prefix), windows / 'System32', windows]))

root = Path(SPECPATH).parent
common = dict(pathex=[str(root / 'scripts')], excludes=['PyQt5', 'PyQt6', 'PySide2', 'matplotlib', 'numpy', 'IPython', 'pytest'])
gui = Analysis([str(root / 'scripts/frozen_entry.py')],
    datas=[(str(root / 'assets'), 'assets'), (str(root / 'VERSION'), '.'),
           (str(root / 'packaging/apply-update.ps1'), 'packaging')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
                   'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on'], **common)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name='DESKFLOW', console=False,
              icon=str(root / 'assets/deskflow-icon-v1.ico'), upx=False)
mcp = Analysis([str(root / 'scripts/mcp_server.py')], datas=[], hiddenimports=[], **common)
mcp_exe = EXE(PYZ(mcp.pure), mcp.scripts, [], exclude_binaries=True, name='DeskflowMCP', console=True,
              icon=str(root / 'assets/deskflow-icon-v1.ico'), upx=False)
COLLECT(gui_exe, mcp_exe, gui.binaries, gui.datas, mcp.binaries, mcp.datas, name='DESKFLOW', upx=False)
