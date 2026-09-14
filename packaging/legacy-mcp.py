"""Keep existing v0.2 stdio MCP configurations working after EXE migration."""
from pathlib import Path
import subprocess
raise SystemExit(subprocess.call([str(Path(__file__).resolve().parent.parent / "DeskflowMCP.exe")]))
