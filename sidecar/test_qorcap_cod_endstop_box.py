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
