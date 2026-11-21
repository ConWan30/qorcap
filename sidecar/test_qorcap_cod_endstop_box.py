"""Practice CoD auto stop on sampled frames that live only on the build box (not committed).

The frames come from earlier practice matches and show other players' gamertags, so they stay
out of the repo and out of both CI sidecar suites (see tools/assert_pytest_skips.py). Each test
skips when its folder is missing. Set QORCAP_COD_PROFILE_FRAMES to point somewhere else.
"""
import os
from pathlib import Path

import cv2
import pytest

import qorcap_cod_endstop as ES

ROOT = Path(os.environ.get("QORCAP_COD_PROFILE_FRAMES", "/workspace/cod-profile/frame-pass-v2/frames"))
ARMED = 200_000


def _frames():
    if not ROOT.is_dir():
        pytest.skip(f"box-only frames not present: {ROOT}")
    files = sorted(ROOT.glob("*.jpg"))
    if not files:
        pytest.skip(f"no frames in {ROOT}")
    return files


def test_no_mid_match_sample_reads_as_an_end_word():
    from qorcap_cod_hud import read_end
    mids = [f for f in _frames() if f.stem.rsplit("_", 1)[-1] in ("0", "1", "2")]
    assert mids
    hits = [f.name for f in mids if ES.end_word(read_end(cv2.imread(str(f)))) is not None]
    assert hits == []


def test_every_end_word_sample_held_on_screen_confirms_its_own_word():
    from qorcap_cod_hud import read_end, read_owner_row
    seen = 0
    for f in _frames():
        img = cv2.imread(str(f))
        end = read_end(img)
        w = ES.end_word(end)
        if w is None:
            continue
        seen += 1
        owner = read_owner_row(img)
        es = ES.EndStop(ES.Config())
        es.observe(0, None)
        for i in range(20):
            es.observe(ARMED + i * 500, end, owner)
        assert es.confirmed is not None, f.name
        assert es.confirmed["result"] == w.upper()
    assert seen > 0


BEST_PLAY_ROOT = Path(os.environ.get("QORCAP_COD_BEST_PLAY_FRAMES", "/workspace/cod-1113b/all"))
BEST_PLAY_LABELS = BEST_PLAY_ROOT.parent / "cal" / "labels.json"
BEST_PLAY_TUNING = {"145809", "154154"}


def test_best_play_title_on_held_out_tails():
    """4 fps frames of local practice tails (other players' names on screen, so not committed).

    Labels are cal/labels.json: pos = full BEST PLAY title, amb = slide-in or fade (not scored).
    Held-out = every clip not used to build the mask.
    """
    import json
    from qorcap_cod_bestplay import read_best_play
    if not BEST_PLAY_ROOT.is_dir() or not BEST_PLAY_LABELS.is_file():
        pytest.skip(f"box-only frames not present: {BEST_PLAY_ROOT}")
    labels = json.loads(BEST_PLAY_LABELS.read_text(encoding="utf-8"))
    counts = {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    fired = set()
    for clip in sorted(p for p in BEST_PLAY_ROOT.iterdir() if p.is_dir()):
        if clip.name in BEST_PLAY_TUNING or clip.name not in labels:
            continue
        lab = labels[clip.name]
        for i, f in enumerate(sorted(clip.glob("*.jpg"))):
            t = i * 0.25
            if any(a - 1e-6 <= t <= b + 1e-6 for a, b in lab["amb"]):
                continue
            pos = any(a - 1e-6 <= t <= b + 1e-6 for a, b in lab["pos"])
            seen = read_best_play(cv2.imread(str(f))).seen
            counts[("tp" if seen else "fn") if pos else ("fp" if seen else "tn")] += 1
            if seen:
                fired.add(clip.name)
    assert counts["fp"] == 0, counts
    assert counts["tp"] >= 20 and counts["fn"] <= 1, counts
    assert fired == {"133025", "183311", "194037"}
