"""125430 is a held-out evaluation match.

This module used to download that match's public highlights and grade locks
against the labelled 7-14 final. Those clips are not fetched and are not an
evaluation fixture. The reader must abstain.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/tmp/qortime"))

from qorcap_standard import standard_reader  # noqa: E402
from qorcap_template_guard import TEMPLATES_MISSING  # noqa: E402


def test_125430_is_held_out_and_is_not_scored():
    held = json.loads((HERE.parent / "templates" / "heldout.json").read_text(encoding="utf-8"))
    assert "125430" in held["match_ids"]
    fr = standard_reader().read(np.zeros((360, 640, 3), np.uint8))
    assert fr.reason == TEMPLATES_MISSING
    assert fr.left_score is None and fr.right_score is None
