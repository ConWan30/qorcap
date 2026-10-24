"""PSN name templates had no recorded match id and were removed.

021003 crops in sidecar/fixtures are held-out frames. This module does not score
them. A missing pack emits no handle.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

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
    """Held-out match. Frames are not an evaluation fixture, even if a path is set."""
    held = json.loads((HERE.parent / "templates" / "heldout.json").read_text(encoding="utf-8"))
    assert "021003" in held["match_ids"]
    assert P.load_pack().get("missing") is True
    assert not (tmp_path / "psn_names.json").exists()
