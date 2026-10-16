$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service.ps1')
$mutex = $null
$startupClock = [Diagnostics.Stopwatch]::StartNew()
$phaseClock = [Diagnostics.Stopwatch]::StartNew()

function Write-StartupTiming([string]$Phase) {
    $line = '[startup] {0}: {1:N3}s; total {2:N3}s' -f $Phase, $phaseClock.Elapsed.TotalSeconds, $startupClock.Elapsed.TotalSeconds
    Write-Host $line
    try {
        New-Item -ItemType Directory -Force -Path $serviceData | Out-Null
        Add-Content -LiteralPath (Join-Path $serviceData 'startup.log') -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $line)
    } catch { Write-Warning 'Could not write startup timings; startup will continue.' }
    $phaseClock.Restart()
}

try {
    $mutex = Enter-ServiceLock
    $state = Read-ServiceState
    $portOpen = Test-ServicePort
    $health = if ($state -and $portOpen) { Get-ServiceHealth } else { $null }
    if ($state -and $health -and $health.service -eq 'travel-planner' -and $health.instance -eq $state.instance) {
        Write-StartupTiming 'Existing service ready'
        Write-Host ('Already running. Opening ' + $serviceUrl)
        Start-Process $serviceUrl
        exit 0
    }
    if ($portOpen) {
        throw 'Port 8000 is occupied. Close the old backend terminal or stop the other service, then retry.'
    }
    Write-StartupTiming 'Existing service and port check'

    Write-Host 'Checking local dependencies...'
    foreach ($command in @('node.exe', 'npm.cmd')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw 'Node.js and npm are required. Install Node.js 22.19+, then reopen this script.'
        }
    }
    $python = Join-Path $projectRoot 'backend\.venv\Scripts\pythonw.exe'
    $vite = Join-Path $projectRoot 'frontend\node_modules\.bin\vite.cmd'
    $sdk = Join-Path $projectRoot 'model-runtime\node_modules\@earendil-works\pi-ai\package.json'
    $flyai = Join-Path $projectRoot 'model-runtime\node_modules\@fly-ai\flyai-cli\package.json'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $vite) -or -not (Test-Path -LiteralPath $sdk) -or -not (Test-Path -LiteralPath $flyai)) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See the error above.' }
    }
    $envFile = Join-Path $projectRoot 'backend\.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        Copy-Item -LiteralPath (Join-Path $projectRoot 'backend\.env.example') -Destination $envFile
    }
    Write-StartupTiming 'Dependency preparation'

    $frontend = Join-Path $projectRoot 'frontend'
    $index = Join-Path $frontend 'dist\index.html'
    $inputs = @(Get-ChildItem -LiteralPath (Join-Path $frontend 'src') -File -Recurse)
    $inputs += @(Get-ChildItem -LiteralPath $frontend -File)
    $needsBuild = -not (Test-Path -LiteralPath $index)
    if (-not $needsBuild) {
        $builtAt = (Get-Item -LiteralPath $index).LastWriteTimeUtc
        $needsBuild = @($inputs | Where-Object { $_.LastWriteTimeUtc -gt $builtAt }).Count -gt 0
    }
    if ($needsBuild) {
        Write-Host 'Frontend sources changed. Building the updated page...'
        Build-Frontend
    }
    Write-StartupTiming 'Frontend freshness check and any required build'

    New-Item -ItemType Directory -Force -Path $serviceData | Out-Null
    $instance = [guid]::NewGuid().ToString('N')
    $runner = Join-Path $projectRoot 'backend\serve.py'
    Write-Host 'Starting the backend. Detailed timings will be written to backend/data/server.log...'
    $backendProcess = Start-Process -FilePath $python -WindowStyle Hidden -PassThru -WorkingDirectory (Join-Path $projectRoot 'backend') -ArgumentList @(
        ('"{0}"' -f $runner), $instance
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        if ($backendProcess.HasExited) { throw ('The backend exited before becoming ready. See ' + $serviceLog) }
        $health = Get-ServiceHealth -TimeoutMilliseconds 500
        if ($health -and $health.service -eq 'travel-planner' -and $health.instance -eq $instance) {
            Write-StartupTiming 'Backend ready; opening browser'
            Write-Host ('Running in the background: ' + $serviceUrl)
            Write-Host ('Log: ' + $serviceLog)
            Write-Host 'Use stop.bat to stop the service. This window may now close.'
            Start-Process $serviceUrl
            exit 0
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ('The service did not become ready. See ' + $serviceLog)
} catch {
    Write-StartupTiming 'Startup failed'
    Write-Host ('Startup failed: {0}' -f $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
}
