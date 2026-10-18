@echo off
rem QorCap Glimpse shadow watcher: ADVISORY_AI, shadow-only. It never feeds a score lock, the receipt or the timeline.
rem Start it after Start Match is recording, with QORCAP_GLIMPSE_FRAME=1 set before Start Match.
rem Usage: "Glimpse Watch.cmd" regular ^| primetime ^| monday_night ^| london
set "GLAYOUT=%~1"
if "%GLAYOUT%"=="" set "GLAYOUT=unknown"
set "GPY=%LOCALAPPDATA%\QorCap\venv\Scripts\python.exe"
if not exist "%GPY%" set "GPY=python"
start "QorCap Glimpse watch (advisory)" /min /belownormal "%GPY%" "%~dp0sidecar\qorcap_glimpse_watch.py" --layout %GLAYOUT%
echo Glimpse watch started minimized (layout %GLAYOUT%). Report: %LOCALAPPDATA%\QorCapGlimpse\^<match^>\REPORT.md
