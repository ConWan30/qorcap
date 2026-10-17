@echo off
rem QorCap: send your last match to the QorTime official referee for an independent check (VERIFIED receipt).
title QorCap - Submit for Verification
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-match.ps1" submit %*
