@echo off
pushd "%~dp0"
where uv >nul 2>nul || (echo Super Sound Pad needs uv: https://docs.astral.sh/uv/getting-started/installation/ & pause & exit /b 1)
uv run soundboard.py %*
if errorlevel 1 pause
