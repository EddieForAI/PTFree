@echo off
rem SPDX-FileCopyrightText: 2026 EddieForAI
rem SPDX-License-Identifier: Apache-2.0

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-windows.ps1" %*
if errorlevel 1 (
    pause
    exit /b 1
)
