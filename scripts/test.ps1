$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$failed = @()

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "`n== $Name ==" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) { $script:failed += $Name }
}

Step 'backend unit tests' {
    Push-Location (Join-Path $projectRoot 'backend')
    try { & '.\.venv\Scripts\python.exe' -m unittest discover -s tests } finally { Pop-Location }
}
Step 'model-runtime tests' {
    npm.cmd test --prefix (Join-Path $projectRoot 'model-runtime')
}
Step 'frontend type check' {
    Push-Location (Join-Path $projectRoot 'frontend')
    try { npx.cmd vue-tsc --noEmit } finally { Pop-Location }
}

if ($failed.Count) {
    Write-Host ("`nFailed: " + ($failed -join ', ')) -ForegroundColor Red
    exit 1
}
Write-Host "`nAll checks passed." -ForegroundColor Green
