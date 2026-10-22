@echo off
setlocal
chcp 65001 >nul
set "APP=%LOCALAPPDATA%\FeishuCodexBridge"
if not exist "%APP%\.venv\Scripts\pythonw.exe" (
  echo 尚未安装，请先双击 Install-Feishu-Codex.cmd
  pause
  exit /b 1
)
start "" "%APP%\.venv\Scripts\pythonw.exe" "%APP%\setup_gui.py"

