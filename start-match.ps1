# QorCap one-click match launcher (Windows). Started by "Start Match.cmd".
# Checks ffmpeg, Python 3.11+ and its packages, then runs sidecar\qorcap_match.py.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $env:LOCALAPPDATA "QorCap"
function Done($code) { Write-Host ""; Read-Host "Press Enter to close this window" | Out-Null; exit $code }
function Fail($msg) { Write-Host ""; Write-Host "PROBLEM: $msg" -ForegroundColor Red; Done 1 }

# Match folder this run just signed. An explicit bundle path wins; otherwise the newest match-* whose
# receipt.json was written at or after $since (so a previous game is not attested by mistake).
function Find-ThisRunBundle($argList, $since) {
  $valueFlags = @(
    "--out", "--qorcap", "--ffmpeg", "--qortime", "--side", "--role", "--key-dir", "--device",
    "--encoder", "--res", "--fps", "--input-format", "--audio", "--referee-url", "--port",
    "--player-id", "--test-secs", "--hz", "--role-reason", "--pre", "--post", "--samples"
  )
  $root = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "QorCap Matches"
  $explicit = $null
  $skipNext = $false
  for ($i = 0; $i -lt $argList.Count; $i++) {
    $a = [string]$argList[$i]
    if ($skipNext) { $skipNext = $false; continue }
    if ($a -eq "--out" -and ($i + 1) -lt $argList.Count) { $root = [string]$argList[$i + 1]; $skipNext = $true; continue }
    if ($a.StartsWith("--out=")) { $root = $a.Substring(6); continue }
    if ($valueFlags -contains $a) { $skipNext = $true; continue }
    if ($a.StartsWith("-")) { continue }
    if ($a -in @("finalize", "highlights", "submit", "psn-names")) { continue }
    $explicit = $a
  }
  if ($explicit) {
    $receipt = Join-Path $explicit "receipt.json"
    if (Test-Path -LiteralPath $receipt) { return (Resolve-Path -LiteralPath $explicit).Path }
  }
  if (-not (Test-Path -LiteralPath $root)) { return $null }
  $bestPath = $null
  $bestT = $null
  foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory -Filter "match-*" -ErrorAction SilentlyContinue)) {
    $rp = Join-Path $d.FullName "receipt.json"
    if (-not (Test-Path -LiteralPath $rp)) { continue }
    $t = (Get-Item -LiteralPath $rp).LastWriteTime
    if ($t -lt $since) { continue }
    if ($null -eq $bestT -or $t -gt $bestT) { $bestPath = $d.FullName; $bestT = $t }
  }
  return $bestPath
}

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
# Highlight clips plus content credentials run after the receipt is signed (Start Match and Finalize Last Match).
# Pass --no-auto-highlights to opt out. This script does not set QORCAP_CONTENT_CREDENTIALS, so qorcap.exe
# record and clip export stay default-off and a recording is unchanged. A highlights failure cannot stop finalize or submit.
$fwd = @($args)
# finalize is an offline sign of a finished bundle. Live-only flags (side prompt, official challenges)
# must not be forwarded or argparse rejects the whole finalize before any clip is cut.
$isFinalize = $fwd -contains "finalize"
$hasChallengeFlag = $false
foreach ($x in $fwd) {
  if ($x -eq "--official-challenges" -or $x -eq "--no-official-challenges") { $hasChallengeFlag = $true }
}
if (-not $hasChallengeFlag -and -not $isFinalize) { $fwd = @("--official-challenges") + $fwd }

# Side pick (v0.3.26): your side is signed into the first checkpoint, so it is chosen before recording starts.
# "Start Match.cmd --side away" (or home) skips the prompt; so does a one-shot next-side.txt in %LOCALAPPDATA%\QorCap
# (renamed after use; ignored if older than 4 h). Otherwise qorcap_match.py shows the big H / A prompt and waits.
$hasSide = $false
foreach ($x in $fwd) { if ($x -eq "--side" -or $x -eq "--role" -or $x -like "--side=*" -or $x -like "--role=*") { $hasSide = $true } }
if (-not $hasSide -and -not $isFinalize) { $fwd = @("--side", "ask") + $fwd }

# Glimpse defaults (v0.3.28). The default lives in this launcher. qorcap.exe stays default-off:
# this script does not pass the record frame flag, and the flag-off tests are unchanged.
# A live Start Match sets QORCAP_GLIMPSE_FRAME=1 so GET /frame/scorebug is on, starts the advisory
# watcher, and turns on QORCAP_GLIMPSE_WITNESS, QORCAP_GLIMPSE_BURST and QORCAP_GLIMPSE_ATTEST.
# --no-glimpse turns all of that off. --no-glimpse-witness, --no-glimpse-burst and --no-glimpse-attest
# each turn one switch off. Caps are not set here: the watcher keeps $0.50/game and $10/week.
# QORCAP_GLIMPSE_BURST_RING, QORCAP_GLIMPSE_CLIP_AUDIT and QORCAP_GLIMPSE_TD_VETO stay off.
# Start Match never turns the touchdown veto on.
# The watcher is detached, minimized, below normal priority. It waits up to 30 min (covering the side
# prompt) for the frame tap. Any problem is one line and the match runs. The API key is never printed.
# A missing OpenRouter key skips witness and burst calls (logged) and does not block or slow recording.
# A missing observer key is created once with --init-key; that failure is also one line and recording continues.
$env:QORCAP_GLIMPSE_BURST_RING = "0"
$env:QORCAP_GLIMPSE_CLIP_AUDIT = "0"
$env:QORCAP_GLIMPSE_TD_VETO = "0"
$noGlimpse = $fwd -contains "--no-glimpse"
$noWitness = $noGlimpse -or ($fwd -contains "--no-glimpse-witness")
$noBurst = $noGlimpse -or ($fwd -contains "--no-glimpse-burst")
$noAttest = $noGlimpse -or ($fwd -contains "--no-glimpse-attest")
$fwd = @($fwd | Where-Object { $_ -ne "--no-glimpse" -and $_ -ne "--no-glimpse-witness" -and $_ -ne "--no-glimpse-burst" -and $_ -ne "--no-glimpse-attest" })
$offlineCmd = $false
foreach ($c in @("finalize", "highlights", "submit", "psn-names")) { if ($fwd -contains $c) { $offlineCmd = $true } }
$glimpseLive = -not $noGlimpse -and -not $offlineCmd
if ($noGlimpse) {
  $env:QORCAP_GLIMPSE_FRAME = "0"
  $env:QORCAP_GLIMPSE_WITNESS = "0"
  $env:QORCAP_GLIMPSE_BURST = "0"
  $env:QORCAP_GLIMPSE_ATTEST = "0"
} elseif ($glimpseLive) {
  $env:QORCAP_GLIMPSE_FRAME = "1"            # qorcap.exe (started by qorcap_match.py) inherits the frame tap
  if ($noWitness) { $env:QORCAP_GLIMPSE_WITNESS = "0" } else { $env:QORCAP_GLIMPSE_WITNESS = "1" }
  if ($noBurst) { $env:QORCAP_GLIMPSE_BURST = "0" } else { $env:QORCAP_GLIMPSE_BURST = "1" }
  if ($noAttest) { $env:QORCAP_GLIMPSE_ATTEST = "0" } else { $env:QORCAP_GLIMPSE_ATTEST = "1" }
} elseif ($isFinalize -and -not $noAttest) {
  $env:QORCAP_GLIMPSE_ATTEST = "1"
} elseif ($isFinalize -and $noAttest) {
  $env:QORCAP_GLIMPSE_ATTEST = "0"
} elseif ($offlineCmd) {
  $env:QORCAP_GLIMPSE_ATTEST = "0"
}
if ($env:QORCAP_GLIMPSE_ATTEST -eq "1") {
  try {
    $obsKey = Join-Path $data "keys\glimpse-observer.key"
    if (-not (Test-Path -LiteralPath $obsKey)) {
      $prevEap = $ErrorActionPreference
      $ErrorActionPreference = "Continue"
      & $vpy (Join-Path $here "sidecar\qorcap_glimpse_attest.py") --init-key
      $initCode = $LASTEXITCODE
      $ErrorActionPreference = $prevEap
      if ($initCode -ne 0 -or -not (Test-Path -LiteralPath $obsKey)) {
        Write-Host "  Glimpse key .... not created; attest will be unsigned. Recording is unaffected"
      } else {
        Write-Host "  Glimpse key .... created once"
      }
    }
  } catch {
    Write-Host "  Glimpse key .... not created ($($_.Exception.Message)); recording is unaffected"
  }
}
try {
  $gWatch = Join-Path $here "sidecar\qorcap_glimpse_watch.py"
  if ($glimpseLive -and (Test-Path $gWatch)) {
    $orKey = $env:OPENROUTER_API_KEY
    if (-not $orKey) { $orKey = [Environment]::GetEnvironmentVariable("OPENROUTER_API_KEY", "User") }
    if (-not $orKey) { $orKey = [Environment]::GetEnvironmentVariable("OPENROUTER_API_KEY", "Machine") }
    if (-not $orKey -and ($env:QORCAP_GLIMPSE_WITNESS -eq "1" -or $env:QORCAP_GLIMPSE_BURST -eq "1")) {
      Write-Host "  Glimpse witness .. skipped (no OpenRouter key); recording is unaffected"
      try {
        $glogDir = Join-Path $env:LOCALAPPDATA "QorCapGlimpse"
        New-Item -ItemType Directory -Force -Path $glogDir | Out-Null
        Add-Content -Path (Join-Path $glogDir "start-match.log") -Value "$(Get-Date -Format o) witness skipped: no OpenRouter key"
      } catch {}
    }
    $hadKey = [bool]$env:OPENROUTER_API_KEY
    if (-not $hadKey -and $orKey) { $env:OPENROUTER_API_KEY = $orKey }
    $gLayout = $env:QORCAP_GLIMPSE_LAYOUT; if (-not $gLayout) { $gLayout = "auto" }
    $gp = Start-Process -FilePath $vpy -WindowStyle Minimized -PassThru -ArgumentList @(
      "`"$gWatch`"", "--layout", $gLayout, "--wait-start-s", "1800", "--idle-exit-s", "60")
    if (-not $hadKey) { Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue }  # the recorder does not need it
    try { $gp.PriorityClass = "BelowNormal" } catch {}
    Write-Host "  Glimpse watch .. started (advisory only, minimized; report in $env:LOCALAPPDATA\QorCapGlimpse)"
  }
} catch { Write-Host "  Glimpse watch .. not started ($($_.Exception.Message)); the match is unaffected" }
$noAutoHighlights = $fwd -contains "--no-auto-highlights"
$fwd = @($fwd | Where-Object { $_ -ne "--no-auto-highlights" })
if (-not $noAutoHighlights -and -not ($fwd -contains "--auto-highlights")) { $fwd = @("--auto-highlights") + $fwd }
$glimpseSince = Get-Date
& $vpy (Join-Path $here "sidecar\qorcap_match.py") --qorcap (Join-Path $here "qorcap.exe") --ffmpeg (Join-Path $here "ffmpeg.exe") @qtArgs @fwd
$matchCode = $LASTEXITCODE
if ($env:QORCAP_GLIMPSE_ATTEST -eq "1") {
  try {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $bundle = Find-ThisRunBundle $fwd $glimpseSince
    if ($bundle) {
      & $vpy (Join-Path $here "sidecar\qorcap_glimpse_attest.py") --match-dir $bundle
      if ($LASTEXITCODE -ne 0) {
        Write-Host "  Glimpse attest .. not signed (exit $LASTEXITCODE); the match is unaffected"
      } else {
        Write-Host "  Glimpse attest .. signed (sidecar only; the receipt is unchanged)"
      }
    } else {
      Write-Host "  Glimpse attest .. skipped (no receipt from this run); the match is unaffected"
    }
    $ErrorActionPreference = $prevEap
  } catch {
    Write-Host "  Glimpse attest .. not signed ($($_.Exception.Message)); the match is unaffected"
  }
}
Done $matchCode
