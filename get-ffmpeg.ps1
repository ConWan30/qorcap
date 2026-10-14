# Downloads a Windows ffmpeg build (gyan.dev "release essentials", GPL) and puts
# ffmpeg.exe next to qorcap.exe. ffmpeg is a separate program with its own license;
# QorCap only runs it as a subprocess.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$zip  = Join-Path $env:TEMP "ffmpeg-essentials.zip"
Write-Host "Downloading ffmpeg (~110 MB)..."
Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
$dst = Join-Path $env:TEMP "ffmpeg-essentials"
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Expand-Archive -Path $zip -DestinationPath $dst
$exe = Get-ChildItem -Path $dst -Recurse -Filter ffmpeg.exe | Select-Object -First 1
Copy-Item $exe.FullName (Join-Path $here "ffmpeg.exe") -Force
Copy-Item (Join-Path $exe.Directory.Parent.FullName "LICENSE") (Join-Path $here "FFMPEG-LICENSE.txt") -ErrorAction SilentlyContinue
& (Join-Path $here "ffmpeg.exe") -hide_banner -encoders 2>$null | Select-String "h264_mf|libx264"
Write-Host "ffmpeg.exe installed next to qorcap.exe"
