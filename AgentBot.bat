@echo off
rem AgentBot launcher — PySide6 GUI (agent_gui_qt.py).
rem Pin this file to the taskbar if you want a one-click start.
setlocal
set "PYW=C:\Users\USER\.pyenv\pyenv-win\versions\3.11.9\pythonw.exe"
if not exist "%PYW%" (
    echo Python not found: %PYW%
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0agent_gui_qt.py"
endlocal
