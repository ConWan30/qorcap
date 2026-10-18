@echo off
rem QorCap: cut verified highlight clips (locked score changes) from your last FINISHED match. Never runs while recording.
title QorCap - Make Highlights
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-match.ps1" highlights %*
