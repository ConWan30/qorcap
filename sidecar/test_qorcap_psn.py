"""PSN name templates had no recorded match id and were removed.

021003 crops in sidecar/fixtures are held-out evaluation. Grading them waits
until a PSN pack is allowlisted. Harvesting from 021003 stays refused.
A missing pack emits no handle.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_psn as P  # noqa: E402
from qorcap_template_guard import TEMPLATES_MISSING_REASON  # noqa: E402


def test_missing_pack_emits_no_name():
    pack = P.load_pack()
    assert pack.get("missing") is True
    assert pack["glyphs"] == {}
    blank = np.zeros((720, 1280, 3), np.uint8)
    assert P.read_frame(blank, pack) == []
    assert P.locate(blank, pack) == []
    doc = P.consensus([], pack)
    assert doc["handle"] is None
    assert doc["why"] == TEMPLATES_MISSING_REASON


def test_refuses_while_recording(tmp_path):
    run = tmp_path / ".qorcap-run"
    run.mkdir()
    (run / "recording.lock").write_text("x")
    os.utime(run / "recording.lock", (time.time(), time.time()))
    with pytest.raises(P.Refuse) as e:
        P.read_bundle(tmp_path, frames=[])
    assert e.value.code == 3


def test_local_021003_both_players(tmp_path):
    """Opponent frames of 021003 are not committed. The committed crops are graded separately."""
    held = json.loads((HERE.parent / "templates" / "heldout.json").read_text(encoding="utf-8"))
    assert "021003" in held["match_ids"]
    assert P.load_pack().get("missing") is True
    assert not (tmp_path / "psn_names.json").exists()


FX = HERE / "fixtures"
TOP = cv2.imread(str(FX / "m27_psn_021003_top_left.png"))
PAUSE = cv2.imread(str(FX / "m27_psn_021003_pause_left.png"))
END = cv2.imread(str(FX / "m27_psn_021003_end_left.png"))
ME = "Qortrola30"


def _need_psn():
    if P.load_pack().get("missing"):
        pytest.skip("templates_missing: PSN name templates are not loaded; 021003 evaluation waits")


def _frame(crop, at_left, at_right):
    img = np.zeros((720, 1280, 3), np.uint8)
    for y, x in (at_left, at_right):
        img[y:y + crop.shape[0], x:x + crop.shape[1]] = crop
    return img


def _top_frame():
    return _frame(TOP, (0, 150), (0, 150 + (974 - 215)))


def _menu_frame(crop):
    return _frame(crop, (560, 380), (560, 380 + (921 - 456)))


def test_fixtures_are_the_real_banner():
    _need_psn()
    assert TOP is not None and PAUSE is not None and END is not None
    b = P.locate(_top_frame())
    assert {(x["surface"], x["side"]) for x in b} == {("top", "left"), ("top", "right")}
    assert all(x["logo"] >= 0.95 for x in b)


@pytest.mark.parametrize("img", [_top_frame(), _menu_frame(PAUSE), _menu_frame(END)], ids=["play-call", "pause", "end"])
def test_reads_the_name_on_every_surface(img):
    _need_psn()
    reads = P.read_frame(img)
    assert len(reads) == 2
    assert [r["text"] for r in reads] == [ME, ME]
    assert all(min(s for _, s, _ in r["glyphs"]) >= P.CHAR_MIN for r in reads)


def test_consensus_and_receipt_safe_output(tmp_path):
    _need_psn()
    (tmp_path / "bundle.json").write_text(json.dumps({"chain_head": "ab" * 32}))
    (tmp_path / "manifest.jsonl").write_text("{}\n")
    frames = [(f"f{i}", img) for i, img in enumerate([_top_frame(), _top_frame(), _menu_frame(PAUSE), _menu_frame(END)])]
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
    assert no_floats(oh)
    (tmp_path / "psn_names.json").write_text(json.dumps(doc))
    assert P.load_bound(tmp_path)["sides"]["left"]["handle"] == ME
    (tmp_path / "manifest.jsonl").write_text('{"changed": 1}\n')
    assert P.load_bound(tmp_path) is None


def test_fail_closed():
    _need_psn()
    assert P.read_frame(np.zeros((720, 1280, 3), np.uint8)) == []
    one = np.zeros((720, 1280, 3), np.uint8)
    one[0:45, 150:420] = TOP
    assert P.read_frame(one) == []
    reads = P.read_frame(_top_frame())
    c = P.consensus([r for r in reads if r["side"] == "left"])
    assert c["handle"] is None and c["sightings"] == 1
    bad = TOP.copy()
    bad[12:22, 124:128] = 0
    reads = []
    for _ in range(3):
        reads += [r for r in P.read_frame(_frame(bad, (0, 150), (0, 150 + 759))) if r["side"] == "left"]
    c = P.consensus(reads)
    assert c["handle"] is None
    assert c["pattern"] is None or c["pattern"] != ME


def test_split_touching_letters():
    """'la' in Qortrola30 touches on this banner: it must not read as one glyph."""
    _need_psn()
    reads = P.read_frame(_top_frame())
    assert reads[0]["text"].endswith("la30")


def test_pack_marks_font_glyphs():
    _need_psn()
    pack = P.load_pack()
    assert pack["source"]["Q"] == "seen" and pack["source"]["E"] == "seen"
    assert pack["source"]["Z"] == "font"
    assert set(pack["glyphs"]) >= set("abcxyzABCXYZ0123456789_-")
