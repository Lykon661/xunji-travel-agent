$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service.ps1')
$mutex = $null
try {
    $mutex = Enter-ServiceLock
    if (Test-ServicePort) { throw 'Stop the running backend before packaging, then start it again afterward.' }
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'backend\.venv\Scripts\pythonw.exe')) -or
        -not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\node_modules\.bin\vite.cmd')) -or
        -not (Test-Path -LiteralPath (Join-Path $projectRoot 'model-runtime\node_modules\@earendil-works\pi-ai\package.json'))) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See the error above.' }
    }
    & (Join-Path $projectRoot 'backend\.venv\Scripts\python.exe') -m pip install -r (Join-Path $projectRoot 'backend\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency update failed. See the error above.' }
    Build-Frontend
    Write-Host 'Frontend packaged. The backend will serve it with the API at port 8000.'
    Write-Host 'Use start.bat to run in the background.'
} catch {
    Write-Host ('Packaging failed: {0}' -f $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
}
