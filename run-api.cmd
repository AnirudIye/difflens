@echo off
title DiffLens API
cd /d "%~dp0"
api\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir api --reload --port 8000
pause
