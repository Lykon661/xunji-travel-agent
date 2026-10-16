param([switch]$WithCrawler, [switch]$CrawlerOnly, [string]$CrawlerDirectory = '')
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendDir = Join-Path $projectRoot 'backend'
$frontendDir = Join-Path $projectRoot 'frontend'
$modelRuntimeDir = Join-Path $projectRoot 'model-runtime'

function Invoke-Required([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Program" }
}

function New-PythonEnvironment {
    $candidates = @(
        @{ Program = 'py'; Arguments = @('-3.11') },
        @{ Program = 'py'; Arguments = @('-3') },
        @{ Program = 'python'; Arguments = @() }
    )
    $versionCheck = 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Program -ErrorAction SilentlyContinue)) { continue }
        $pythonArguments = $candidate.Arguments
        try {
            $version = & $candidate.Program @pythonArguments -c $versionCheck 2>&1
            $available = $LASTEXITCODE -eq 0
        } catch { $available = $false }
        if (-not $available) { continue }
        Write-Host ("Creating virtual environment with Python {0}" -f ($version -join ' '))
        Invoke-Required $candidate.Program ($pythonArguments + @('-m', 'venv', '.venv'))
        return
    }
    throw 'No usable Python 3.11+ runtime found. With Python Install Manager, run: py install 3.11. Otherwise install Python 3.11 from python.org with the launcher/PATH enabled, then reopen start.bat.'
}

function Find-CrawlerPython {
    $candidates = @(
        @{ Program = 'py'; Arguments = @('-3.11') },
        @{ Program = 'python'; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Program -ErrorAction SilentlyContinue)) { continue }
        $pythonArguments = $candidate.Arguments
        try {
            $null = & $candidate.Program @pythonArguments -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>&1
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    return $null
}

function Ensure-CrawlerEnvironment([string]$Checkout) {
    $environment = Join-Path $Checkout '.venv'
    $configuration = Join-Path $environment 'pyvenv.cfg'
    if ((Test-Path -LiteralPath (Join-Path $environment 'Scripts\python.exe')) -and
        (Test-Path -LiteralPath $configuration) -and
        ((Get-Content -LiteralPath $configuration -Raw) -match '(?m)^version\s*=\s*3\.11\.')) {
        return
    }
    $candidate = Find-CrawlerPython
    if (-not $candidate) {
        Write-Host 'Preparing Python 3.11 with Python Install Manager...'
        try {
            Invoke-Required 'py' @('install', '3.11')
        } catch {
            throw 'TRAVEL_CRAWLER_PYTHON_311: Install Python Install Manager or Python 3.11, then retry the crawler installation.'
        }
        $candidate = Find-CrawlerPython
        if (-not $candidate) {
            throw 'TRAVEL_CRAWLER_PYTHON_311: Python 3.11 is unavailable after installation. Reopen the backend and retry.'
        }
    }
    if (Test-Path -LiteralPath $environment) {
        # Preserve the incompatible environment; move only the named child of this checkout.
        $checkoutRoot = (Resolve-Path -LiteralPath $Checkout).Path.TrimEnd('\', '/')
        $source = (Resolve-Path -LiteralPath $environment).Path
        $backup = [System.IO.Path]::GetFullPath((Join-Path $checkoutRoot ('.venv-incompatible-' + [guid]::NewGuid().ToString('N'))))
        if (([System.IO.Path]::GetDirectoryName($source) -ne $checkoutRoot) -or
            ([System.IO.Path]::GetFileName($source) -ne '.venv') -or
            ([System.IO.Path]::GetDirectoryName($backup) -ne $checkoutRoot)) {
            throw 'Crawler environment path is outside the expected checkout.'
        }
        Move-Item -LiteralPath $source -Destination $backup
        Write-Host ("Previous environment preserved at {0}" -f $backup)
    }
    Write-Host 'Creating isolated crawler environment with Python 3.11...'
    $pythonArguments = $candidate.Arguments
    Invoke-Required $candidate.Program ($pythonArguments + @('-m', 'venv', $environment))
}

if (-not $CrawlerOnly) {
Push-Location $backendDir
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        New-PythonEnvironment
    }
    Invoke-Required '.\.venv\Scripts\python.exe' @('-m', 'pip', 'install', '-r', 'requirements.txt')
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
} finally { Pop-Location }

Push-Location $frontendDir
try { Invoke-Required 'npm.cmd' @('install') } finally { Pop-Location }

Push-Location $modelRuntimeDir
try { Invoke-Required 'npm.cmd' @('ci') } finally { Pop-Location }

}

if ($WithCrawler -or $CrawlerOnly) {
    $externalDir = Join-Path $projectRoot 'external'
    New-Item -ItemType Directory -Force -Path $externalDir | Out-Null
    $crawlerDir = if ($CrawlerDirectory) { [System.IO.Path]::GetFullPath($CrawlerDirectory) } else { Join-Path $externalDir 'MediaCrawler' }
    if (-not (Test-Path -LiteralPath $crawlerDir)) {
        Invoke-Required 'git' @('clone', 'https://github.com/NanmiCoder/MediaCrawler.git', $crawlerDir)
    }
    Push-Location $crawlerDir
    try {
        Invoke-Required 'git' @('checkout', '--detach', 'd6f7c5bb906b6dac40ddf343ef9e26438a3de092')
        Ensure-CrawlerEnvironment $crawlerDir
        $marker = Join-Path $crawlerDir '.venv\travel-agent-ready.json'
        if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
        $profileScript = Join-Path $backendDir 'crawler_profile.py'
        $profileOutput = & '.\.venv\Scripts\python.exe' $profileScript $crawlerDir --apply
        if ($LASTEXITCODE -ne 0) { throw 'Cannot apply the pinned Xiaohongshu lite profile; existing login data was preserved.' }
        $profile = ($profileOutput -join "`n") | ConvertFrom-Json
        # Keep the existing venv and login profile; install core dependencies before removing optional features.
        Invoke-Required '.\.venv\Scripts\python.exe' @('-m', 'pip', 'install', '--no-cache-dir', '-r', (Join-Path $backendDir 'crawler-requirements.txt'))
        $optionalPackages = @(& '.\.venv\Scripts\python.exe' $profileScript $crawlerDir --remove-packages)
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read optional crawler package list' }
        Invoke-Required '.\.venv\Scripts\python.exe' (@('-m', 'pip', 'uninstall', '-y') + $optionalPackages)
        Invoke-Required '.\.venv\Scripts\python.exe' @('-m', 'playwright', 'install', 'chromium')
        Invoke-Required '.\.venv\Scripts\python.exe' @((Join-Path $backendDir 'crawler_health.py'), $crawlerDir)
        @{
            complete = $true
            revision = 'd6f7c5bb906b6dac40ddf343ef9e26438a3de092'
            requirements_sha256 = (Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash.ToLowerInvariant()
            profile = $profile.profile
            profile_sha256 = $profile.profile_sha256
            source_sha256 = $profile.source_sha256
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $marker -Encoding UTF8
    } finally { Pop-Location }
}
Write-Host 'Dependencies installed. Choose models in the web settings; configure external services in backend/.env.'
