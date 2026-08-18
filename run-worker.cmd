@echo off
title DiffLens Worker
cd /d "%~dp0"
set PYTHONPATH=api
api\.venv\Scripts\python.exe -m worker
pause
