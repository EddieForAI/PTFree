# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

param([switch]$Lan, [ValidateRange(1, 65535)][int]$Port = 8787, [switch]$Remove, [switch]$AtBoot, [switch]$LocalOnly)
$ErrorActionPreference = 'Stop'
$taskName = 'MTFree-AutoDownload'
if ($Lan -and $LocalOnly) { throw '-Lan and -LocalOnly cannot be used together.' }
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Autostart removed. Program and data are preserved.'
    exit
}
$runtime = Join-Path $PSScriptRoot 'runtime\pythonw.exe'
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
    $runtime = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
}
$data = Join-Path $env:LOCALAPPDATA 'MTFree'
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $data 'auth.json') -PathType Leaf)) {
    throw 'Run 1-Install_Service.bat first.'
}
$bindAddress = if ($LocalOnly) { '127.0.0.1' } else { '0.0.0.0' }
$argsText = '-m ptfree --data "{0}" --host {1} --port {2} run' -f $data, $bindAddress, $Port
$action = New-ScheduledTaskAction -Execute $runtime -Argument $argsText -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
if ($AtBoot) {
    $credential = Get-Credential -UserName ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -Message 'Enter the Windows account password for unattended startup (not a PIN).'
    if (-not $credential) { throw 'A Windows account password is required for boot startup.' }
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $networkCredential = $credential.GetNetworkCredential()
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -User $credential.UserName -Password $networkCredential.Password -Settings $settings -RunLevel Limited -Force | Out-Null
    Write-Host 'Boot startup enabled. Windows Task Scheduler stores the account credential.'
} else {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host 'Autostart enabled at user logon.'
}
Write-Host "Listen address: ${bindAddress}:$Port"
Write-Host 'Run 0-Stop_Service.bat before starting this task if PTFree is already running.'
