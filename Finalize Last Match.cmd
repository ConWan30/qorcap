@echo off
rem QorCap: read the score from your last recorded match (after the fact) and sign its receipt.
title QorCap - Finalize Last Match
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-match.ps1" finalize %*
