"""Forward pre-EXE shortcuts and startup entries to the installed application."""
from pathlib import Path
import subprocess
subprocess.Popen([str(Path(__file__).resolve().parent.parent / "DESKFLOW.exe"), "--start"],
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
