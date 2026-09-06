[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$KeepSettings,
    [switch]$SkipIntegrations,
    [switch]$Finalize
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\DeskflowOrganizer"
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$localPrograms = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
if (-not $InstallRoot.StartsWith($localPrograms, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "为避免误删，卸载目录必须位于 $localPrograms 内。"
}

if (-not $Finalize) {
    if (-not $SkipIntegrations) {
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/shutdown" -TimeoutSec 2 | Out-Null
        } catch {
            # The local service is optional and may already be stopped.
        }

        Remove-Item -LiteralPath "HKCU:\Software\Classes\DesktopBackground\Shell\DESKFLOW\command" -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath "HKCU:\Software\Classes\DesktopBackground\Shell\DESKFLOW" -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\DESKFLOW 启动器.bat") -Force -ErrorAction SilentlyContinue

        $desktop = [Environment]::GetFolderPath("Desktop")
        Remove-Item -LiteralPath (Join-Path $desktop "DESKFLOW 管理器.lnk") -Force -ErrorAction SilentlyContinue

        if (-not $KeepSettings) {
            Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA "DeskflowOrganizer") -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath (Join-Path $env:APPDATA "DeskflowOrganizer") -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $tempScript = Join-Path $env:TEMP ("deskflow-uninstall-" + [guid]::NewGuid().ToString("N") + ".ps1")
    Copy-Item -LiteralPath $MyInvocation.MyCommand.Path -Destination $tempScript -Force
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$tempScript`" -InstallRoot `"$InstallRoot`" -Finalize"
    Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden
    Write-Host "DESKFLOW 正在卸载。"
    exit 0
}

Start-Sleep -Seconds 2
Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
$currentScript = [System.IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
$tempRoot = [System.IO.Path]::GetFullPath($env:TEMP)
if ($currentScript.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
    ([System.IO.Path]::GetFileName($currentScript) -like "deskflow-uninstall-*.ps1")) {
    Remove-Item -LiteralPath $currentScript -Force -ErrorAction SilentlyContinue
}
