# Compatibility entry point for the v0.2 ZIP updater.
param([string]$InstallRoot = '', [switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
if (-not $InstallRoot) { $InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\DeskflowOrganizer' }
$arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $InstallRoot + '"'))
if (-not $NoLaunch) { $arguments += '/RESTARTAPP=1' }
$process = Start-Process -FilePath (Join-Path $PSScriptRoot 'DESKFLOW-windows-x64-Setup.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw "DESKFLOW installer failed: $($process.ExitCode)" }
