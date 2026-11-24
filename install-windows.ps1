# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

param([string]$Python = '')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$bundledRuntime = Join-Path $PSScriptRoot 'runtime\python.exe'
if (Test-Path $bundledRuntime) {
    $runtime = $bundledRuntime
    & $runtime -c 'import ptfree, cryptography, sqlite3'
    if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime verification failed. Extract the complete ZIP again.' }
} else {
function Check-Python([string]$Candidate) {
    if (-not $Candidate) { return $false }
    try { & $Candidate -c 'import sys,venv; assert sys.version_info >= (3,11)' 2>$null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
if ($Python -and -not (Check-Python $Python)) { throw 'The specified Python is not a full Python 3.11+ installation.' }
if (-not $Python) {
    try { $candidate = (& py -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1); if (Check-Python $candidate) { $Python = $candidate } } catch {}
}
if (-not $Python) {
    foreach ($cmd in @('python','python3')) {
        $candidate = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
        if (Check-Python $candidate) { $Python = $candidate; break }
    }
}
if (-not $Python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'Install a full Python 3.11+ from https://www.python.org/downloads/windows/ and run this installer again.' }
    Write-Host 'Installing Python 3.14 for the current user...'
    & winget install --id Python.Python.3.14 --exact --source winget --scope user --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed. Install Python manually and retry.' }
    $Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'
    if (-not (Check-Python $Python)) { throw 'Python was installed but could not be found. Reopen this installer or use -Python C:\path\python.exe.' }
}
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the isolated Python environment failed.' }
}
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path 'wheels\*.whl') { & $runtime -m pip install --no-index --find-links wheels -r requirements.txt }
else { & $runtime -m pip install --index-url https://pypi.mirrors.ustc.edu.cn/simple --timeout 30 --retries 2 -r requirements.txt }
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network access to the USTC PyPI mirror.' }
}
$data = Join-Path $env:LOCALAPPDATA 'MTFree'
New-Item -ItemType Directory -Path $data -Force | Out-Null
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls $data /inheritance:r /grant:r "*$($sid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the data directory permissions.' }
& $runtime -m ptfree --data $data init
if ($LASTEXITCODE -ne 0) { throw 'Application initialization failed.' }
Write-Host "Installed. Data: $data"
Write-Host 'Initial password: see the .txt file in the data directory. Change it after login.'
Write-Host 'Double-click 3-Start_Service.bat. Open http://127.0.0.1:8787'
Start-Process explorer.exe $data
