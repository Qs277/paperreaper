@echo off
title PaperReaper
color 0F
cd /d "%~dp0"

echo ============================================================
echo                    PaperReaper
echo         homework in -^> handwritten answers out
echo ============================================================
echo.

where node >nul 2>nul
if %errorlevel%==0 goto run
echo [ERROR] Node.js not found. Install Node.js 18+ first.
pause
exit /b 1

:run
echo [INFO] Starting local server, browser will open automatically.
echo.
node server.js
pause >nul
