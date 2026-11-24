# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

$ErrorActionPreference = 'Stop'
$data = Join-Path $env:LOCALAPPDATA 'MTFree'
$escapedData = [regex]::Escape($data)
$dataArg = '(?i)(?:^|\s)--data\s+(?:"' + $escapedData + '"|' + $escapedData + ')(?=\s|$)'

function Get-MTFreeProcess {
    Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object {
            $_.CommandLine -match '(?i)(?:^|\s)-m\s+(?:ptfree|mtfree)(?=\s|$)' -and $_.CommandLine -match $dataArg
        }
}

$task = Get-ScheduledTask -TaskPath '\' -TaskName 'MTFree-AutoDownload' -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') {
    Stop-ScheduledTask -InputObject $task
}

$deadline = [DateTime]::UtcNow.AddSeconds(8)
do {
    $instances = @(Get-MTFreeProcess)
    if (-not $instances.Count) {
        Write-Host 'PTFree is stopped for this Windows account.'
        exit 0
    }
    # Stop the supervisor first so that it cannot restart a worker we are stopping.
    $instances = $instances | Sort-Object -Property @{Expression = { if ($_.CommandLine -match '\srun\s*$') { 0 } else { 1 } }}
    foreach ($instance in $instances) {
        try {
            Stop-Process -Id $instance.ProcessId -Force -ErrorAction Stop
        } catch {
            if (Get-Process -Id $instance.ProcessId -ErrorAction SilentlyContinue) { throw }
        }
    }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)

throw 'PTFree is still running. Run this under the same Windows account used to start it.'
