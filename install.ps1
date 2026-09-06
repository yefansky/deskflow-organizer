[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$NoLaunch,
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "DESKFLOW 目前只支持 Windows。"
}

$sourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\DeskflowOrganizer"
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)

function Find-CompatiblePython {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidates += ,@($py.Source, "-3.12")
        $candidates += ,@($py.Source, "-3.11")
    }
    foreach ($name in @("python.exe", "python3.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += ,@($command.Source)
        }
    }

    foreach ($candidate in $candidates) {
        $executable = $candidate[0]
        $prefix = @($candidate | Select-Object -Skip 1)
        & $executable @prefix -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Executable = $executable; Prefix = $prefix }
        }
    }
    throw "未找到 Python 3.11 或 3.12。请从 https://www.python.org/downloads/windows/ 安装后重试。"
}

$python = Find-CompatiblePython
$venvRoot = Join-Path $InstallRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$venvPythonw = Join-Path $venvRoot "Scripts\pythonw.exe"

Write-Host "[1/4] 安装目录：$InstallRoot"
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallRoot "scripts") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallRoot "assets") -Force | Out-Null
Copy-Item -Path (Join-Path $sourceRoot "scripts\*.py") -Destination (Join-Path $InstallRoot "scripts") -Force
Copy-Item -Path (Join-Path $sourceRoot "assets\*") -Destination (Join-Path $InstallRoot "assets") -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "requirements.txt") -Destination $InstallRoot -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "uninstall.ps1") -Destination $InstallRoot -Force

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[2/4] 创建独立 Python 运行环境"
    & $python.Executable @($python.Prefix) -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "创建 Python 运行环境失败。" }
} else {
    Write-Host "[2/4] 复用已有 Python 运行环境"
}

if (-not $SkipDependencies) {
    Write-Host "[3/4] 安装 DESKFLOW 依赖"
    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败。" }
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $InstallRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "安装 Python 依赖失败。" }
} else {
    Write-Host "[3/4] 已跳过依赖安装（验证模式）"
}

$launcher = @"
@echo off
start "" "$venvPythonw" "$InstallRoot\scripts\deskflow_manager.py" --start
"@
Set-Content -LiteralPath (Join-Path $InstallRoot "启动DESKFLOW.bat") -Value $launcher -Encoding UTF8

Write-Host "[4/4] 完成安装"
if (-not $NoLaunch) {
    Start-Process -FilePath $venvPythonw -ArgumentList @("$InstallRoot\scripts\deskflow_manager.py", "--install")
    Write-Host "DESKFLOW 已启动，并将创建桌面快捷方式和桌面右键入口。"
} else {
    Write-Host "验证模式未启动桌面程序。"
}
