$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service.ps1')
$mutex = $null
try {
    $mutex = Enter-ServiceLock
    $state = Read-ServiceState
    if (-not $state) {
        if (Test-ServiceLifetimeLock) { throw 'A backend is still releasing its lifetime lock. Wait for it to exit, then retry.' }
        Write-Host 'No managed background service is running.'
        exit 0
    }
    # Target this launch rather than killing an unrelated reused PID.
    [IO.File]::WriteAllText($serviceStop, $state.instance, [Text.Encoding]::UTF8)
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $current = Read-ServiceState
        if ($current -and $current.instance -ne $state.instance) {
            Write-Host 'Background service stopped.'
            exit 0
        }
        $locked = Test-ServiceLifetimeLock
        if (-not $current -and -not $locked) {
            Write-Host 'Background service stopped.'
            exit 0
        }
        $process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
        if (-not $process -and -not $locked) {
            Remove-Item -LiteralPath $serviceState, $serviceStop -ErrorAction SilentlyContinue
            Write-Host 'Removed state from a service that has already exited.'
            exit 0
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ('Shutdown is taking longer than expected. See ' + $serviceLog)
} catch {
    Write-Host ('Stop failed: {0}' -f $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
}
