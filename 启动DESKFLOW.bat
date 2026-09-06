@echo off
chcp 65001 >nul
set "DESKFLOW_HOME=%LOCALAPPDATA%\Programs\DeskflowOrganizer"
set "DESKFLOW_PYTHON=%DESKFLOW_HOME%\.venv\Scripts\pythonw.exe"
if not exist "%DESKFLOW_PYTHON%" (
  echo DESKFLOW 尚未安装。请先运行“安装DESKFLOW.bat”。
  pause
  exit /b 1
)
start "" "%DESKFLOW_PYTHON%" "%DESKFLOW_HOME%\scripts\deskflow_manager.py" --start
