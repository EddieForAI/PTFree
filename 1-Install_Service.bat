@echo off
rem SPDX-FileCopyrightText: 2026 EddieForAI
rem SPDX-License-Identifier: Apache-2.0

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
if errorlevel 1 echo INSTALL FAILED. Read the error above.
pause
