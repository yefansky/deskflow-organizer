param([string]$Python = 'python', [string]$ISCC = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not $ISCC) {
        $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($compiler) { $ISCC = $compiler.Source }
        else { $ISCC = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
    }
    if (-not (Test-Path -LiteralPath $ISCC)) { throw 'Install Inno Setup 6 or pass -ISCC with its compiler path.' }
    & $Python -m PyInstaller --noconfirm --clean packaging/deskflow.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    $version = (Get-Content VERSION -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid VERSION.' }
    & $ISCC "/DAppVersion=$version" packaging/installer.iss
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
    $output = Join-Path $root 'dist\installer'
    $exe = Join-Path $output 'DESKFLOW-windows-x64-Setup.exe'
    (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() + '  DESKFLOW-windows-x64-Setup.exe' | Set-Content -LiteralPath ($exe + '.sha256') -Encoding utf8
    # Keep v0.2's ZIP updater able to migrate to the frozen installer.
    $stage = Join-Path $root 'build\legacy-release'
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Copy-Item -LiteralPath $exe -Destination $stage -Force
    Copy-Item -LiteralPath 'packaging\legacy-install.ps1' -Destination (Join-Path $stage 'install.ps1') -Force
    $zip = Join-Path $output 'DESKFLOW-windows-x64.zip'
    Compress-Archive -LiteralPath (Join-Path $stage 'install.ps1'),(Join-Path $stage 'DESKFLOW-windows-x64-Setup.exe') -DestinationPath $zip -Force
    (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() + '  DESKFLOW-windows-x64.zip' | Set-Content -LiteralPath ($zip + '.sha256') -Encoding utf8
} finally { Pop-Location }
