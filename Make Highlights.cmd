@echo off
rem QorCap: cut highlight clips (scorebug reads) from your last FINISHED match. The panel title is SCOREBUG LOCK. Never runs while recording.
title QorCap - Make Highlights
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-match.ps1" highlights %*
