@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
if errorlevel 1 (
  echo.
  echo DESKFLOW 卸载失败，请保留本窗口中的错误信息。
  pause
)
