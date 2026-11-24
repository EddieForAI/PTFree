@echo off
rem SPDX-FileCopyrightText: 2026 EddieForAI
rem SPDX-License-Identifier: Apache-2.0

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-autostart-windows.ps1" %*
if errorlevel 1 echo AUTOSTART SETUP FAILED. Read the error above.
pause
