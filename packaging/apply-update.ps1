param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][int]$WaitPid,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedHash,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$log = Join-Path (Split-Path -Parent $Installer) 'update-handoff.log'
try {
    $parent = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
    if ($parent -and -not $parent.WaitForExit(120000)) { throw 'DESKFLOW did not exit within 120 seconds.' }
    # Windows PowerShell launched from pwsh/agent hosts can inherit a module
    # path that makes Get-FileHash unavailable. SHA-256 needs only .NET.
    $stream = [System.IO.File]::OpenRead($Installer)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $actualHash = [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $sha.Dispose() }
    if ($actualHash -ne $ExpectedHash) { throw 'Installer checksum changed.' }
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $InstallRoot + '"'), ('/LOG="' + $log + '.installer.log"'))
    if (-not $NoLaunch) { $arguments += '/RESTARTAPP=1' }
    $process = Start-Process -FilePath $Installer -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Installer failed with exit code $($process.ExitCode). See $log.installer.log" }
    'Update installed successfully.' | Set-Content -LiteralPath $log
} catch {
    $_ | Out-String | Set-Content -LiteralPath $log
    [Console]::Error.WriteLine($_.Exception.Message)
    if (-not $NoLaunch) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show("DESKFLOW update failed. Your settings have been preserved. Log: $log", 'DESKFLOW') | Out-Null
    }
    exit 1
}
