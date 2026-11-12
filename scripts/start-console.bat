@echo off
rem qbkb console launcher: double-click to start. See docs/usage.md
rem (ASCII only on purpose: cmd.exe reads .bat files in the system code page.)
setlocal
rem UTF-8 so the Chinese messages from qbkb show correctly in this window
chcp 65001 >nul
cd /d "%~dp0.."
where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo [qbkb] uv was not found. Install it first: see docs/usage.md, step 1.
  echo.
  pause
  exit /b 1
)
uv run qbkb console
if errorlevel 1 (
  echo.
  echo [qbkb] The console stopped with an error. See the messages above.
  pause
)
endlocal
