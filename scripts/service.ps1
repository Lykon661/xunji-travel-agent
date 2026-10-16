$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$serviceData = Join-Path $projectRoot 'backend\data'
$serviceState = Join-Path $serviceData 'server.json'
$serviceStop = Join-Path $serviceData 'server.stop'
$serviceLog = Join-Path $serviceData 'server.log'
$serviceUrl = 'http://127.0.0.1:8000'

function Enter-ServiceLock {
    $bytes = [Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $name = ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-', '') }
    finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\TravelPlanner-' + $name))
    try { $acquired = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) {
        $mutex.Dispose()
        throw 'Another start, build or stop operation is running. Please wait.'
    }
    return $mutex
}

function Read-ServiceState {
    if (Test-Path -LiteralPath $serviceState) {
        try { return (Get-Content -LiteralPath $serviceState -Raw | ConvertFrom-Json) }
        catch { return $null }
    }
    return $null
}

function Test-ServiceLifetimeLock {
    $path = Join-Path $serviceData 'server.lock'
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $stream = $null
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
        $stream.Lock(0, 1)
        $stream.Unlock(0, 1)
        return $false
    } catch [IO.IOException] {
        return $true
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Get-ServiceHealth([int]$TimeoutMilliseconds = 2000) {
    $response = $null
    $reader = $null
    try {
        $request = [Net.HttpWebRequest]::Create($serviceUrl + '/api/health')
        $request.Proxy = $null
        $request.Timeout = $TimeoutMilliseconds
        $request.ReadWriteTimeout = $TimeoutMilliseconds
        $response = $request.GetResponse()
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        return ($reader.ReadToEnd() | ConvertFrom-Json)
    }
    catch { return $null }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Test-ServicePort {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', 8000); return $true }
    catch { return $false }
    finally { $client.Dispose() }
}

function Build-Frontend {
    Push-Location (Join-Path $projectRoot 'frontend')
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend packaging failed. See the error above.' }
    } finally { Pop-Location }
}
