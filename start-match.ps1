# QorCap one-click match launcher (Windows). Started by "Start Match.cmd".
# Checks ffmpeg, Python 3.11+ and its packages, then runs sidecar\qorcap_match.py.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $env:LOCALAPPDATA "QorCap"
function Done($code) { Write-Host ""; Read-Host "Press Enter to close this window" | Out-Null; exit $code }
function Fail($msg) { Write-Host ""; Write-Host "PROBLEM: $msg" -ForegroundColor Red; Done 1 }

Get-ChildItem -Path $here -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Write-Host "Checking everything is ready..."

# 1. ffmpeg
if (-not (Test-Path (Join-Path $here "ffmpeg.exe"))) {
  Write-Host "ffmpeg is missing; downloading it once (about 110 MB)..."
  try { & (Join-Path $here "get-ffmpeg.ps1") } catch { Fail "Could not download ffmpeg. Check the internet and double-click Start Match again. ($_)" }
  if (-not (Test-Path (Join-Path $here "ffmpeg.exe"))) { Fail "ffmpeg download did not finish. Double-click Start Match again." }
}
Write-Host "  ffmpeg ........ OK"

# 2. Python 3.11+
function Find-Python {
  foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
    try {
      $v = & $c[0] $c[1..9] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.11") { return ,$c }
    } catch {}
  }
  return $null
}
$py = Find-Python
if (-not $py) {
  Write-Host "Python 3.11 or newer is not installed. Trying to install it with winget..."
  try { winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --scope user | Out-Host } catch {}
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
  $py = Find-Python
  if (-not $py) { Fail "Please install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then double-click Start Match again." }
}
Write-Host "  Python ........ OK"

# 3. private environment with the packages QorCap needs
$venv = Join-Path $data "venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
  Write-Host "Setting up QorCap's Python packages (first run only, a few minutes)..."
  New-Item -ItemType Directory -Force -Path $data | Out-Null
  & $py[0] $py[1..9] -m venv $venv
  if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment in $venv" }
}
# Native stderr must not trip $ErrorActionPreference=Stop (PS 7+ / MyTurn first-run crash).
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $vpy -c "import numpy, cv2, cryptography" 2>$null | Out-Null
$needPkgs = ($LASTEXITCODE -ne 0)
$ErrorActionPreference = $prevEap
if ($needPkgs) {
  & $vpy -m pip install --disable-pip-version-check -q numpy opencv-python-headless cryptography
  if ($LASTEXITCODE -ne 0) { Fail "Could not install Python packages (numpy, opencv-python-headless, cryptography). Check the internet and try again." }
}
Write-Host "  Packages ...... OK"

# 4. QorTime code: bundled in .\qortime (pinned), otherwise qorcap_match.py downloads the pinned zip.
$qt = Join-Path $here "qortime"
$qtArgs = @(); if (Test-Path (Join-Path $qt "qor_receipt\receipt.py")) { $qtArgs = @("--qortime", $qt) }
Write-Host ""

# Official challenges are on for a double-clicked match. Pass --no-official-challenges to opt out.
# Tests call qorcap_match.py directly and do not get this flag.
$fwd = @($args)
$hasChallengeFlag = $false
foreach ($x in $fwd) {
  if ($x -eq "--official-challenges" -or $x -eq "--no-official-challenges") { $hasChallengeFlag = $true }
}
if (-not $hasChallengeFlag) { $fwd = @("--official-challenges") + $fwd }

# Advisory Glimpse watcher (v0.3.22): ADVISORY_AI, shadow-only, never feeds a lock or the receipt. On when the user
# variable QORCAP_GLIMPSE_FRAME is 1 (opt out for one match with --no-glimpse). Started detached, minimized, below
# normal priority, before recording; it waits for QorCap's frame tap and stops when QorCap goes away. Any problem
# here is reported in one line and the match runs normally. The API key is never printed.
$noGlimpse = $fwd -contains "--no-glimpse"
$fwd = @($fwd | Where-Object { $_ -ne "--no-glimpse" })
try {
  $gFlag = $env:QORCAP_GLIMPSE_FRAME
  if (-not $gFlag) { $gFlag = [Environment]::GetEnvironmentVariable("QORCAP_GLIMPSE_FRAME", "User") }
  $gWatch = Join-Path $here "sidecar\qorcap_glimpse_watch.py"
  if ($gFlag -eq "1" -and -not $noGlimpse -and (Test-Path $gWatch)) {
    $env:QORCAP_GLIMPSE_FRAME = "1"            # QorCap (started by qorcap_match.py) inherits the opt-in frame tap
    $hadKey = [bool]$env:OPENROUTER_API_KEY
    if (-not $hadKey) { $env:OPENROUTER_API_KEY = [Environment]::GetEnvironmentVariable("OPENROUTER_API_KEY", "User") }
    $gLayout = $env:QORCAP_GLIMPSE_LAYOUT; if (-not $gLayout) { $gLayout = "auto" }
    $gp = Start-Process -FilePath $vpy -WindowStyle Minimized -PassThru -ArgumentList @(
      "`"$gWatch`"", "--layout", $gLayout, "--wait-start-s", "900", "--idle-exit-s", "60")
    if (-not $hadKey) { Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue }  # the recorder does not need it
    try { $gp.PriorityClass = "BelowNormal" } catch {}
    Write-Host "  Glimpse watch .. started (advisory only, minimized; report in $env:LOCALAPPDATA\QorCapGlimpse)"
  }
} catch { Write-Host "  Glimpse watch .. not started ($($_.Exception.Message)); the match is unaffected" }
& $vpy (Join-Path $here "sidecar\qorcap_match.py") --qorcap (Join-Path $here "qorcap.exe") --ffmpeg (Join-Path $here "ffmpeg.exe") @qtArgs @fwd
Done $LASTEXITCODE
