@echo off
rem James' Radio: double-click to play. Needs uv, which fetches Python and the two libraries on the first run.
title James' Radio
pushd "%~dp0"
where uv >nul 2>nul || (echo James' Radio needs uv: https://docs.astral.sh/uv/getting-started/installation/ & pause & exit /b 1)
uv run radio.py %*
if errorlevel 1 pause
