param([switch]$KeepSettings)
# The EXE uninstaller always preserves user settings and desktop files.
$process = Start-Process -FilePath (Join-Path $PSScriptRoot 'unins000.exe') -PassThru
$process.WaitForExit()
exit $process.ExitCode
