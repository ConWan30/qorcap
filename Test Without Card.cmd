@echo off
rem QorCap dry run: no capture card needed (test pattern video + a scripted 20-second game).
title QorCap - Test Without Card
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-match.ps1" --test-pattern %*
