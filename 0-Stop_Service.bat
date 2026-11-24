@echo off
rem SPDX-FileCopyrightText: 2026 EddieForAI
rem SPDX-License-Identifier: Apache-2.0

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-windows.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
pause
