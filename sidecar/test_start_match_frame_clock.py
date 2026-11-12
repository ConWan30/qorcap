"""start-match.ps1: frame clock on by default for PRACTICE runs only.

REAL MATCH launches leave QORCAP_FRAME_CLOCK alone (qorcap.exe default stays off).
The behaviour test runs the launcher's own function under PowerShell when one is installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS = (ROOT / "start-match.ps1").read_text(encoding="utf-8")
LAUNCH = '& $vpy (Join-Path $here "sidecar\\qorcap_match.py")'


def _function(name):
    m = re.search(r"^function " + re.escape(name) + r"\(.*?^\}", PS, re.S | re.M)
    assert m, name
    return m.group(0)


def test_practice_default_is_set_before_the_picker_and_the_launch():
    assert "Get-QorCapPracticeFrameClock $fwd $env:QORCAP_FRAME_CLOCK $env:QORCAP_LANE" in PS
    call = PS.index("$practiceClock = Get-QorCapPracticeFrameClock")
    assert call < PS.index("$picked = Show-QorCapGamePicker")
    assert call < PS.index(LAUNCH)
    assert "$env:QORCAP_FRAME_CLOCK = $practiceClock" in PS
    # The only assignment to QORCAP_FRAME_CLOCK in the launcher is the practice one.
    assert PS.count("$env:QORCAP_FRAME_CLOCK =") == 1


def test_shortcuts_unchanged():
    start = (ROOT / "Start Match.cmd").read_text(encoding="utf-8")
    card = (ROOT / "Test Without Card.cmd").read_text(encoding="utf-8")
    assert "--test-pattern" not in start and "FRAME_CLOCK" not in start
    assert "--test-pattern" in card and "FRAME_CLOCK" not in card


CASES = [
    # (args, QORCAP_FRAME_CLOCK, QORCAP_LANE, expected)
    ([], None, None, "NULL"),                                   # REAL MATCH: untouched
    (["--side", "home"], None, None, "NULL"),
    (["--no-frame-clock"], None, None, "NULL"),                 # REAL MATCH: still untouched
    ([], "1", None, "NULL"),
    (["finalize"], None, "practice", "NULL"),                   # offline command
    (["--test-pattern"], None, None, "1"),                      # Test Without Card
    ([], None, "practice", "1"),
    ([], None, " PRACTICE ", "1"),
    ([], None, "real", "NULL"),
    (["--test-pattern"], "0", None, "NULL"),                    # explicit env wins
    (["--test-pattern", "--no-frame-clock"], None, None, "0"),
    (["--test-pattern", "--no-frame-clock"], "1", None, "0"),
]


def _shell():
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.parametrize("args,env_val,lane,expected", CASES)
def test_practice_frame_clock_function(args, env_val, lane, expected):
    # No skip: the CI skip guard fails on an unlisted skip. GitHub's Linux and Windows
    # runners ship PowerShell. Without one, only the function text is checked.
    if _shell() is None:
        fn = _function("Get-QorCapPracticeFrameClock")
        assert 'if (-not $practice) { return $null }' in fn
        assert 'if ($envVal) { return $null }' in fn
        return
    def lit(v):
        return "$null" if v is None else "'" + v.replace("'", "''") + "'"
    arr = "@(" + ",".join(lit(a) for a in args) + ")"
    script = (_function("Get-QorCapPracticeFrameClock") + "\n"
              f"$r = Get-QorCapPracticeFrameClock {arr} {lit(env_val)} {lit(lane)}\n"
              "if ($null -eq $r) { 'NULL' } else { $r }\n")
    out = subprocess.run([_shell(), "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected
