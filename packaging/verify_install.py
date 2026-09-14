"""Production installer acceptance on a disposable Windows Actions runner.

This intentionally uses the real installer/shortcuts/registry, so it refuses
to run on a developer desktop. User data retention is checked byte-for-byte.
"""
import hashlib
import json
import base64
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
from win32com.client import Dispatch
from win32com.shell import shell, shellcon
import pythoncom

from verify_bundle import verify


def run(args, timeout=180):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    assert result.returncode == 0, (args, result.returncode, result.stdout[-3000:], result.stderr[-3000:])
    return result


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('This test modifies real per-user installation entries. Run only on a disposable GitHub Actions Windows runner.')
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    evidence = root / 'build/verification'
    evidence.mkdir(parents=True, exist_ok=True)
    install = Path(os.environ['RUNNER_TEMP']) / 'Deskflow install with spaces'
    production = root / 'dist/installer/DESKFLOW-windows-x64-Setup.exe'
    frozen_version = root / 'dist/DESKFLOW/_internal/VERSION'
    original = frozen_version.read_bytes()
    old_output = evidence / 'old'
    old_output.mkdir(exist_ok=True)
    try:
        frozen_version.write_text('0.0.1', encoding='utf-8')
        iscc = Path(os.environ['ProgramFiles(x86)']) / 'Inno Setup 6/ISCC.exe'
        run([iscc, '/DAppVersion=0.0.1', '/O' + str(old_output), 'packaging/installer.iss'])
    finally:
        frozen_version.write_bytes(original)
    old_setup = old_output / production.name
    run([old_setup, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/DIR=' + str(install), '/LOG=' + str(evidence / 'install-old.log')])
    verify(install)
    old_version = json.loads((evidence / 'gui-smoke.json').read_text())['version']
    assert old_version == '0.0.1'
    # Preserve realistic settings across update and uninstall; never use real
    # desktop files. The smoke test does not start the organizer overlay.
    state = Path(os.environ['APPDATA']) / 'SmartDesktopOrganizer/layout.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b'{"test":"preserve classifications and user preferences"}'
    state.write_bytes(sentinel)
    desktop = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    link = desktop / 'DESKFLOW 管理器.lnk'
    assert link.is_file(), {'desktop': str(desktop), 'entries': [p.name for p in desktop.iterdir()]}
    (evidence / 'desktop-shortcut.lnk').write_bytes(link.read_bytes())
    shell_link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    shell_link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(link))
    shell_link.Resolve(0, shell.SLR_NO_UI | shell.SLR_NOSEARCH | shell.SLR_NOTRACK)
    shortcut = shell_link.GetPath(shell.SLGP_RAWPATH)[0]
    assert Path(shortcut).resolve() == (install / 'DESKFLOW.exe').resolve(), {'link': str(link), 'target': shortcut, 'lnk_base64': base64.b64encode(link.read_bytes()).decode()}
    shell_link = None
    # Hold a live stdio MCP process: installer must tell it to exit before
    # replacing the shared Python DLL, rather than leave a partial install.
    mcp = subprocess.Popen([str(install / 'DeskflowMCP.exe')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])
    try:
        mcp.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'); mcp.stdin.flush()
        assert json.loads(mcp.stdout.readline())['id'] == 1
        helper = root / 'packaging/apply-update.ps1'
        started = time.monotonic()
        update = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper),
            '-Installer', str(production), '-WaitPid', str(parent.pid), '-InstallRoot', str(install),
            '-ExpectedHash', hashlib.sha256(production.read_bytes()).hexdigest(), '-NoLaunch'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(2)
        assert parent.poll() is None and mcp.poll() is None, 'Update ran before the old parent exited.'
        stdout, stderr = update.communicate(timeout=180)
        assert update.returncode == 0, (stdout, stderr)
        assert mcp.wait(timeout=10) == 0, 'MCP did not release its files gracefully.'
        elapsed = time.monotonic() - started
    finally:
        for proc in (parent, mcp):
            if proc.poll() is None: proc.terminate(); proc.wait(timeout=10)
    verify(install)
    new_version = json.loads((evidence / 'gui-smoke.json').read_text())['version']
    assert new_version == original.decode('utf-8-sig').strip() and new_version != old_version
    assert state.read_bytes() == sentinel
    # Old clients still targeting scripts/mcp_server.py must reach the new MCP.
    legacy = subprocess.run([sys.executable, str(install / 'scripts/mcp_server.py')], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert legacy.returncode == 0 and len(json.loads(legacy.stdout)['result']['tools']) >= 17
    # Exercise the actual installed manager -> overlay -> HTTP/MCP lifecycle,
    # not just dependency imports. This runs only on the disposable CI desktop.
    manager = subprocess.Popen([str(install / 'DESKFLOW.exe'), '--start'])
    live = False
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urlopen('http://127.0.0.1:8765/health', timeout=1) as response:
                    live = response.status == 200
                if live: break
            except OSError:
                time.sleep(.5)
        assert live and manager.poll() is None, 'Installed manager/overlay failed to start.'
        catalog_request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'get_desktop_catalog', 'arguments': {'limit': 1}}}) + '\n'
        catalog = subprocess.run([str(install / 'DeskflowMCP.exe')], input=catalog_request, capture_output=True, text=True, encoding='utf-8', timeout=20)
        assert catalog.returncode == 0 and 'result' in json.loads(catalog.stdout), catalog.stderr
    finally:
        run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', root / 'packaging/prepare-install.ps1', '-InstallRoot', install], timeout=60)
        manager.wait(timeout=15)
    before_uninstall = state.read_bytes()
    run([install / 'unins000.exe', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LOG=' + str(evidence / 'uninstall.log')])
    assert not (install / 'DESKFLOW.exe').exists()
    assert state.read_bytes() == before_uninstall
    (evidence / 'installation.json').write_text(json.dumps({'ok': True, 'old_version': old_version, 'new_version': new_version,
        'shortcut': shortcut, 'handoff_seconds': elapsed, 'mcp_lock_released': True, 'legacy_mcp': True,
        'installed_manager_overlay_mcp': live, 'settings_retained_after_update_and_uninstall': True}, indent=2), encoding='utf-8')
    print('Real installer, two-version update, MCP release, legacy MCP and uninstall verified.')


if __name__ == '__main__':
    main()
