@echo off
rem Starts the RONIN Deck server hidden (port 8805). The phone link is written to data\url.txt.
cd /d "%~dp0"
start "" /b "%~dp0.venv\Scripts\pythonw.exe" "%~dp0deck_server.py"
