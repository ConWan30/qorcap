@echo off
rem QorCap: double-click to record a Madden match and get a signed QorTime receipt.
title QorCap - Start Match
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-match.ps1" %*
