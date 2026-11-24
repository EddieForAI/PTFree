# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

param(
    [switch]$Lan,
    [ValidateRange(1, 65535)][int]$Port = 8787,
    [switch]$NoBrowser,
    [switch]$LocalOnly,
    [switch]$Foreground
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Lan -and $LocalOnly) { throw '-Lan and -LocalOnly cannot be used together.' }

$data = Join-Path $env:LOCALAPPDATA 'MTFree'
$wantLan = -not $LocalOnly
$bindAddress = if ($wantLan) { '0.0.0.0' } else { '127.0.0.1' }
$url = "http://127.0.0.1:$Port"

function Test-MTFreeHealth {
    try {
        $reply = Invoke-WebRequest -Uri ($url + '/healthz') -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
        return ($reply.StatusCode -eq 200 -and $reply.Headers['Server'] -match '^(PTFree|MTFree)' -and
            ($reply.Content | ConvertFrom-Json).alive -eq $true)
    } catch {
        return $false
    }
}

function Test-MTFreeLanListener {
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    foreach ($listener in $listeners) {
        if ($listener.Port -eq $Port -and $listener.Address.Equals([System.Net.IPAddress]::Any)) {
            return $true
        }
    }
    return $false
}

if (Test-MTFreeHealth) {
    $lanListener = Test-MTFreeLanListener
    if ($lanListener -ne $wantLan) {
        throw 'PTFree is already running with a different bind address. Run 0-Stop_Service.bat first.'
    }
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

$runtimeDir = Join-Path $PSScriptRoot 'runtime'
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'python.exe') -PathType Leaf)) {
    $runtimeDir = Join-Path $PSScriptRoot '.venv\Scripts'
}
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'python.exe') -PathType Leaf)) {
    throw 'Run 1-Install_Service.bat first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $data 'auth.json') -PathType Leaf)) {
    & (Join-Path $PSScriptRoot 'install-windows.ps1')
}

if ($Foreground) {
    if (-not $NoBrowser) { Start-Process $url }
    & (Join-Path $runtimeDir 'python.exe') -m ptfree --data $data --host $bindAddress --port $Port run
    exit $LASTEXITCODE
}

$runtime = Join-Path $runtimeDir 'pythonw.exe'
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
    throw 'pythonw.exe is missing. Extract the complete PTFree Windows package or use -Foreground to diagnose.'
}
$argsText = '-m ptfree --data "{0}" --host {1} --port {2} run' -f $data, $bindAddress, $Port
$stdout = Join-Path $data 'launcher.stdout.log'
$stderr = Join-Path $data 'launcher.stderr.log'
$process = Start-Process -FilePath $runtime -ArgumentList $argsText -WorkingDirectory $PSScriptRoot `
    -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$deadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    if (Test-MTFreeHealth) {
        if ($wantLan -and -not (Test-MTFreeLanListener)) {
            throw 'PTFree answered locally but did not open the LAN listener. Run 0-Stop_Service.bat and try again.'
        }
        if (-not $NoBrowser) { Start-Process $url }
        exit 0
    }
    if ($process.HasExited) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)
throw "PTFree did not become ready. Check $stderr and $stdout. Run 0-Stop_Service.bat before retrying."
