param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference = 'Stop'
try {
    $event = [System.Threading.EventWaitHandle]::new($false, [System.Threading.EventResetMode]::ManualReset, 'Local\DeskflowOrganizer.Update')
    $event.Set() | Out-Null
    $root = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $running = @(Get-Process -Name DESKFLOW,DeskflowMCP -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
        })
        $held = $false
        foreach ($name in @('Local\DeskflowOrganizer.Manager', 'Local\SmartDesktopOrganizer.Overlay')) {
            try {
                $mutex = [System.Threading.Mutex]::OpenExisting($name)
                $held = $true
                $mutex.Dispose()
            } catch [System.Threading.WaitHandleCannotBeOpenedException] { }
        }
        if (-not $running -and -not $held) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($running -or $held) { throw 'Please exit DESKFLOW and disconnect its MCP server before installing.' }
} catch {
    Write-Error $_
    exit 1
} finally {
    if ($event) { $event.Reset() | Out-Null; $event.Dispose() }
}
