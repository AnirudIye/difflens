@echo off
title DiffLens Web
cd /d "%~dp0web"
set NEXT_TELEMETRY_DISABLED=1
npm run dev
pause
