@echo off
rem Rebuild dist\LineChatTool.exe (the desktop shortcut points here; no need to recreate it).
rem Close any running LineChatTool.exe before building.
cd /d "%~dp0"
.venv\Scripts\pyinstaller.exe --noconfirm LineChatTool.spec
pause
