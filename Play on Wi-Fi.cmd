@echo off
rem James' Radio on Wi-Fi: phones and tablets on the same home network can play it too.
rem The sound still comes out of this PC, and closing the radio's window here stops it for everyone.
title James' Radio (Wi-Fi)
pushd "%~dp0"
where uv >nul 2>nul || (echo James' Radio needs uv: https://docs.astral.sh/uv/getting-started/installation/ & pause & exit /b 1)
uv run radio.py --share %*
if errorlevel 1 pause
