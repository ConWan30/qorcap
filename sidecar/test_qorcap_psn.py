"""On-screen PSN names: reader on real 021003 banner crops (Con's own banner only; no opponent footage in the repo).

The full-frame check of both players (names given in QORCAP_PSN_EXPECT) runs only when QORCAP_PSN_FRAMES points at a local
folder of 1280x720 frames / 1280x148 strips from that game (they include the opponent, so they are not committed)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qorcap_psn as P

FX = Path(__file__).resolve().parent / "fixtures"
TOP = cv2.imread(str(FX / "m27_psn_021003_top_left.png"))      # rows 0-45, cols 150-420 of a play-call frame
PAUSE = cv2.imread(str(FX / "m27_psn_021003_pause_left.png"))  # rows 560-650, cols 380-620 of the pause menu
END = cv2.imread(str(FX / "m27_psn_021003_end_left.png"))      # same box on the end-of-game menu
ME = "Qortrola30"


def frame(crop, at_left, at_right):
    """A 1280x720 frame with Con's own banner crop at the left banner spot and a copy at the right banner spot."""
    img = np.zeros((720, 1280, 3), np.uint8)
    for y, x in (at_left, at_right):
        img[y:y + crop.shape[0], x:x + crop.shape[1]] = crop
    return img


def top_frame():
    return frame(TOP, (0, 150), (0, 150 + (974 - 215)))


def menu_frame(crop):
    return frame(crop, (560, 380), (560, 380 + (921 - 456)))


def test_fixtures_are_the_real_banner():
    assert TOP is not None and PAUSE is not None and END is not None
    b = P.locate(top_frame())
    assert {(x["surface"], x["side"]) for x in b} == {("top", "left"), ("top", "right")}
    assert all(x["logo"] >= 0.95 for x in b)


@pytest.mark.parametrize("img", [top_frame(), menu_frame(PAUSE), menu_frame(END)], ids=["play-call", "pause", "end"])
def test_reads_the_name_on_every_surface(img):
    reads = P.read_frame(img)
    assert len(reads) == 2
    assert [r["text"] for r in reads] == [ME, ME]
    assert all(min(s for _, s, _ in r["glyphs"]) >= P.CHAR_MIN for r in reads)


def test_consensus_and_receipt_safe_output(tmp_path):
    (tmp_path / "bundle.json").write_text(json.dumps({"chain_head": "ab" * 32}))
    (tmp_path / "manifest.jsonl").write_text("{}\n")
    frames = [(f"f{i}", img) for i, img in enumerate([top_frame(), top_frame(), menu_frame(PAUSE), menu_frame(END)])]
    doc = P.read_bundle(tmp_path, frames=frames)
    for side in ("left", "right"):
        c = doc["sides"][side]
        assert c["handle"] == ME and c["pattern"] == ME and c["agree"] == 4 and c["confidence_permille"] >= 900
    assert doc["sides"]["left"]["team"] == "away" and doc["sides"]["right"]["team"] == "home"
    oh = P.observed_handles(doc)
    assert oh["away"]["handle"] == ME and oh["source"] == "on_screen" and "not account proof" in oh["claims"]

    def no_floats(o):
        if isinstance(o, float):
            return False
        if isinstance(o, dict):
            return all(no_floats(v) for v in o.values())
        if isinstance(o, list):
            return all(no_floats(v) for v in o)
        return True
    assert no_floats(oh)  # the receipt is JCS-signed and allows no floats
    (tmp_path / "psn_names.json").write_text(json.dumps(doc))
    assert P.load_bound(tmp_path)["sides"]["left"]["handle"] == ME
    (tmp_path / "manifest.jsonl").write_text('{"changed": 1}\n')  # different video chain -> stale file is ignored
    assert P.load_bound(tmp_path) is None


def test_fail_closed():
    assert P.read_frame(np.zeros((720, 1280, 3), np.uint8)) == []
    one = np.zeros((720, 1280, 3), np.uint8)
    one[0:45, 150:420] = TOP  # a single banner (no opponent banner on that surface) is not a name surface
    assert P.read_frame(one) == []
    # too few sightings
    reads = P.read_frame(top_frame())
    c = P.consensus([r for r in reads if r["side"] == "left"])
    assert c["handle"] is None and c["sightings"] == 1
    # a damaged character becomes '?' and the handle is withheld
    bad = TOP.copy()
    bad[12:22, 124:128] = 0  # blank the right half of the 'o' after "Q" (banner x ~274-278)
    reads = []
    for _ in range(3):
        reads += [r for r in P.read_frame(frame(bad, (0, 150), (0, 150 + 759))) if r["side"] == "left"]
    c = P.consensus(reads)
    assert c["handle"] is None
    assert c["pattern"] is None or c["pattern"] != ME


def test_split_touching_letters():
    """'la' in Qortrola30 touches on this banner (lightning art behind it): it must not read as one glyph."""
    reads = P.read_frame(top_frame())
    assert reads[0]["text"].endswith("la30")


def test_refuses_while_recording(tmp_path):
    run = tmp_path / ".qorcap-run"
    run.mkdir()
    (run / "recording.lock").write_text("x")
    os.utime(run / "recording.lock", (time.time(), time.time()))
    with pytest.raises(P.Refuse) as e:
        P.read_bundle(tmp_path, frames=[])
    assert e.value.code == 3


def test_pack_marks_font_glyphs():
    pack = P.load_pack()
    assert pack["source"]["Q"] == "seen" and pack["source"]["E"] == "seen"
    assert pack["source"]["Z"] == "font"  # never seen in footage: rendered look-alike, weaker evidence
    assert set(pack["glyphs"]) >= set("abcxyzABCXYZ0123456789_-")


FRAMES = os.environ.get("QORCAP_PSN_FRAMES")


@pytest.mark.skipif(not FRAMES, reason="local-only: frames of 021003 include the opponent and are not committed")
def test_local_021003_both_players(tmp_path):
    files = sorted(Path(FRAMES).glob("*.png"))
    frames = [(f.name, cv2.imread(str(f))) for f in files]
    (tmp_path / "bundle.json").write_text("{}")
    doc = P.read_bundle(tmp_path, frames=frames)
    left, right = os.environ["QORCAP_PSN_EXPECT"].split(",")
    assert doc["sides"]["left"]["handle"] == left
    assert doc["sides"]["right"]["handle"] == right
