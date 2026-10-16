@echo off
rem QorCap controller check: confirms this laptop reads the DualSense over USB HID and shows live presses.
title QorCap - Input Test
"%~dp0qorcap.exe" input-test %*
echo.
pause
