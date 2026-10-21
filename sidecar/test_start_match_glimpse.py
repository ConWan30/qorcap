"""Launcher defaults for the cheap-tier Glimpse witness.

The default lives in start-match.ps1. qorcap.exe stays default-off, and the flag-off byte-identical
tests in test_qorcap_glimpse_witness.py stay the description of a process that was not given the env.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = (ROOT / "start-match.ps1").read_text(encoding="utf-8")
LAUNCH = '& $vpy (Join-Path $here "sidecar\\qorcap_match.py")'


def _between(start: str, end: str) -> str:
    return PS[PS.index(start):PS.index(end)]


def test_live_match_turns_witness_burst_attest_and_frame_on():
    live = _between("} elseif ($glimpseLive)", "} elseif ($isFinalize -and -not $noAttest)")
    for name in ("FRAME", "WITNESS", "BURST", "ATTEST"):
        assert f'$env:QORCAP_GLIMPSE_{name} = "1"' in live
    assert '$glimpseLive = -not $noGlimpse -and -not $offlineCmd' in PS


def test_opt_outs_and_no_glimpse_turns_every_switch_off():
    assert '$noGlimpse = $fwd -contains "--no-glimpse"' in PS
    assert '$noWitness = $noGlimpse -or ($fwd -contains "--no-glimpse-witness")' in PS
    assert '$noBurst = $noGlimpse -or ($fwd -contains "--no-glimpse-burst")' in PS
    assert '$noAttest = $noGlimpse -or ($fwd -contains "--no-glimpse-attest")' in PS
    for flag in ("--no-glimpse", "--no-glimpse-witness", "--no-glimpse-burst", "--no-glimpse-attest"):
        assert f'$_ -ne "{flag}"' in PS
    off = _between("if ($noGlimpse)", "} elseif ($glimpseLive)")
    for name in ("FRAME", "WITNESS", "BURST", "ATTEST"):
        assert f'$env:QORCAP_GLIMPSE_{name} = "0"' in off
        assert '"1"' not in off
    # A single opt-out forces that one variable off inside the live branch.
    live = _between("} elseif ($glimpseLive)", "} elseif ($isFinalize -and -not $noAttest)")
    assert 'if ($noWitness) { $env:QORCAP_GLIMPSE_WITNESS = "0" }' in live
    assert 'if ($noBurst) { $env:QORCAP_GLIMPSE_BURST = "0" }' in live
    assert 'if ($noAttest) { $env:QORCAP_GLIMPSE_ATTEST = "0" }' in live


def test_veto_ring_and_clip_audit_are_forced_off_and_caps_stay_at_the_watcher_defaults():
    assert '$env:QORCAP_GLIMPSE_TD_VETO = "0"' in PS
    assert '$env:QORCAP_GLIMPSE_BURST_RING = "0"' in PS
    assert '$env:QORCAP_GLIMPSE_CLIP_AUDIT = "0"' in PS
    for name in ("TD_VETO", "BURST_RING", "CLIP_AUDIT"):
        assert not re.search(rf'QORCAP_GLIMPSE_{name}\s*=\s*["\']1["\']', PS)
    # Caps are the watcher CLI defaults ($0.50 / game, $10 / week). The launcher does not override them.
    for name in ("WEEKLY_LIMIT", "GAME_CAP", "WITNESS_CAP", "BURST_CAP", "CLIP_CAP"):
        assert f"QORCAP_GLIMPSE_{name}" not in PS
    assert "--game-cap" not in PS and "--weekly-limit" not in PS
    assert "$0.50/game" in PS and "$10/week" in PS


def test_observer_key_is_created_once_before_recording_and_fails_soft():
    before = PS[:PS.index(LAUNCH)]
    assert "--init-key" in before
    assert "glimpse-observer.key" in before
    key = _between('if ($env:QORCAP_GLIMPSE_ATTEST -eq "1")', "try {\n  $gWatch")
    assert "try {" in key and "} catch {" in key
    assert "Recording is unaffected" in key
    assert "Fail " not in key
    assert "exit " not in key


def test_missing_openrouter_key_is_logged_and_does_not_block_the_match():
    assert "Glimpse witness .. skipped (no OpenRouter key); recording is unaffected" in PS
    assert "start-match.log" in PS
    assert "witness skipped: no OpenRouter key" in PS
    # The key value is never printed. The variable name appears only on reads and on removing it.
    for ln in PS.splitlines():
        if "Write-Host" in ln:
            assert "OPENROUTER_API_KEY" not in ln
    assert "Remove-Item Env:OPENROUTER_API_KEY" in PS
    assert "Start-Process" in PS and "-Wait" not in PS and "Wait-Process" not in PS
    # Attest cannot change the match exit code, and it runs after recording has returned.
    assert PS.index(LAUNCH) < PS.index("--match-dir") < PS.index("Done $matchCode")
    assert "Done $LASTEXITCODE" not in PS


def test_qorcap_cli_and_flag_off_witness_tests_stay_default_off():
    cli = (ROOT / "src" / "cli.rs").read_text(encoding="utf-8")
    assert "#[arg(long)]\n    pub glimpse_frame: bool," in cli
    assert "--glimpse-frame" not in PS
    eng = (ROOT / "src" / "engine.rs").read_text(encoding="utf-8")
    assert "args.glimpse_frame || crate::glimpse::env_enabled()" in eng
    witness = (ROOT / "sidecar" / "test_qorcap_glimpse_witness.py").read_text(encoding="utf-8")
    for name in (
        "test_witness_flag_off_advisory_bytes_match",
        "test_burst_flag_off_creates_nothing",
        "test_attest_flag_off_writes_nothing",
    ):
        assert f"def {name}" in witness
