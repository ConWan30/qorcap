"""Readers abstain when no allowlisted template is on disk. They do not guess a score.

These tests point at an empty provenance file. A later harvest committed into
``templates/provenance.json`` must not turn a blank frame into ``layout_unknown``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/tmp/qortime"))

from qorcap_endmenu import EndMenuReader  # noqa: E402
from qorcap_mnp import AutoProfileReader, MondayNightReader  # noqa: E402
from qorcap_reader import ReaderCore  # noqa: E402
from qorcap_snp import SundayNightReader  # noqa: E402
from qorcap_standard import reader_model_id, standard_reader  # noqa: E402
from qorcap_template_guard import TEMPLATES_MISSING, TEMPLATES_MISSING_REASON  # noqa: E402
from qorcap_tnp import ThursdayNightReader  # noqa: E402
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader  # noqa: E402

OLD_MODEL = "local_scorebug_v1:madden27-standard-v1:ac836591d618adb8"
BLANK = np.zeros((360, 640, 3), np.uint8)


def _empty_provenance(monkeypatch, tmp_path):
    """The committed file may later hold a harvest. These tests stay on an empty one."""
    import qorcap_template_guard as guard

    prov = tmp_path / "provenance.json"
    prov.write_text(json.dumps({
        "schema": "qorcap-template-provenance/v0",
        "harvest_script": "tools/harvest_templates.py",
        "harvest_version": "3",
        "templates": [],
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "PROVENANCE_PATH", prov)
    return guard


def test_standard_reader_abstains_and_the_model_id_changed(monkeypatch, tmp_path):
    _empty_provenance(monkeypatch, tmp_path)
    reader = standard_reader()
    # Computed from the reader, not a literal, so a rebase onto #81 does not rot the pin.
    expected = reader_model_id(reader.inner, reader.grammar, reader.clocks)
    assert reader.model_id == expected
    assert reader.model_id.startswith("local_scorebug_v1:madden27-standard-v1:")
    assert reader.model_id != OLD_MODEL
    assert reader.model_id != ScorebugReader().model_id
    fr = reader.read(BLANK)
    assert fr.ok is False and fr.reason == TEMPLATES_MISSING
    assert fr.left_score is None and fr.right_score is None and fr.quarter is None
    assert fr.clock_seconds is None
    assert fr.detail["templates_missing"] == TEMPLATES_MISSING_REASON


def test_primetime_and_end_menu_abstain_on_a_synthetic_frame(monkeypatch, tmp_path):
    _empty_provenance(monkeypatch, tmp_path)
    for reader in (MondayNightReader(), SundayNightReader(), ThursdayNightReader()):
        fr = reader.read(np.zeros((720, 1280, 3), np.uint8))
        assert fr.reason == TEMPLATES_MISSING
        assert fr.left_score is None and fr.right_score is None
    end = EndMenuReader().read(BLANK)
    assert end.ok is False and end.reason == TEMPLATES_MISSING
    assert end.away is None and end.home is None


def test_auto_profile_posts_no_score_when_templates_are_missing(monkeypatch, tmp_path):
    _empty_provenance(monkeypatch, tmp_path)
    auto = AutoProfileReader(standard_reader())
    fr = auto.read(np.zeros((720, 1280, 3), np.uint8))
    assert fr.reason == TEMPLATES_MISSING
    assert fr.left_score is None and fr.right_score is None


def test_reader_core_logs_the_reason_and_posts_no_score(capsys):
    posts = []

    class Sneak:
        accepts_time = True

        def read(self, _img, t_ms=None):
            from types import SimpleNamespace
            return SimpleNamespace(
                ok=False, reason=TEMPLATES_MISSING, left_score=9, right_score=8,
                quarter=4, clock_seconds=0, pair=(9, 8),
            )

        def reference_band(self, _img):
            raise AssertionError("crop hashed after abstain")

    import qorcap_template_guard as guard
    guard._LOGGED = False  # the line is once per process; this test must see it in either order
    core = ReaderCore(posts.append, Sneak(), None, None, lambda _rows: {"basis": "should-not-run"})
    core.on_frame(1000, BLANK)
    assert core.stats.get("agreed", 0) == 0
    assert core.stats.get("templates_missing") == 1
    assert all(row.get("type") != "score" for row in posts)
    assert any(row.get("obs") == "templates_missing" and row.get("reason") == TEMPLATES_MISSING_REASON
               for row in core.timeline)
    assert "home" not in posts[0] and "away" not in posts[0]
    assert TEMPLATES_MISSING_REASON in capsys.readouterr().err


def test_relabeling_a_template_changes_the_model_id(monkeypatch, tmp_path):
    """Same bytes, different role, label, or profile. The id is not a byte hash alone."""
    import qorcap_template_guard as guard

    npy = tmp_path / "score" / "102332__1__score_left__f000000__g0.npy"
    npy.parent.mkdir()
    vec = np.zeros(4, np.float32)
    vec[0] = 1.0
    np.save(npy, vec)
    digest = hashlib.sha256(npy.read_bytes()).hexdigest()
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({
        "match_ids": ["102332"],
        "sources": [{
            "match_id": "102332",
            "folder": "match-20261024-102332",
            "video_sha256": "ab" * 32,
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "SOURCES_PATH", sources)
    monkeypatch.setattr(guard, "HELDOUT_PATH", HERE.parent / "templates" / "heldout.json")

    def write(label: str, role: str = "score", profile: str = "standard") -> None:
        row = {
            "path": "score/102332__1__score_left__f000000__g0.npy",
            "sha256": digest,
            "role": role,
            "label": label,
            "profile": profile,
            "source_match_id": "102332",
            "source_match_folder": "match-20261024-102332",
            "video_sha256": "ab" * 32,
        }
        path = tmp_path / "provenance.json"
        path.write_text(json.dumps({"templates": [row]}), encoding="utf-8")
        monkeypatch.setattr(guard, "PROVENANCE_PATH", path)

    reader = standard_reader()
    write("1")
    base = reader_model_id(reader.inner, reader.grammar, reader.clocks)
    write("7")
    assert reader_model_id(reader.inner, reader.grammar, reader.clocks) != base
    write("1", role="clock")
    assert reader_model_id(reader.inner, reader.grammar, reader.clocks) != base
    write("1", profile="end_menu")
    assert reader_model_id(reader.inner, reader.grammar, reader.clocks) != base
    write("1")
    assert reader_model_id(reader.inner, reader.grammar, reader.clocks) == base
