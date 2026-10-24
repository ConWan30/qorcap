"""The harvest script refuses held-out and unknown matches and round-trips glyphs.

Synthetic pixels only. 555001 is not a real template-source game and not a held-out game.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "sidecar"))
sys.path.insert(0, os.environ.get("QORTIME", "/tmp/qortime"))
import harvest_templates as H  # noqa: E402
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader  # noqa: E402

NOW = datetime(2026, 10, 24, 17, 31, tzinfo=timezone.utc)
HELD = ROOT / "templates" / "heldout.json"
HASHES = ROOT / "sidecar" / "packs" / "held_out_sha256.json"
SCHEMA = json.loads((ROOT / "schema" / "template_provenance.v0.json").read_text(encoding="utf-8"))


def _logo():
    return np.clip(ScorebugReader().pack.logo, 0, 255).astype(np.uint8)


def _bar(logo) -> np.ndarray:
    """A fake standard bar: stock logo, white panel, dark clock, font digits at the real ROIs."""
    frame = np.zeros((360, 640, 3), np.uint8)
    frame[341:357, 200:300] = (255, 255, 255)
    frame[341:356, 428:484] = (12, 12, 12)
    frame[340:358, 8:80] = cv2.cvtColor(logo, cv2.COLOR_GRAY2BGR)
    cv2.putText(frame, "1", (224, 355), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_8)
    cv2.putText(frame, "7", (262, 355), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_8)
    cv2.putText(frame, "5:23", (447, 352), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (240, 240, 240), 1, cv2.LINE_8)
    cv2.putText(frame, "1", (430, 353), cv2.FONT_HERSHEY_SIMPLEX, 0.28, (230, 230, 230), 1, cv2.LINE_8)
    return frame


def _match(tmp: Path, short: str, *, video: bool = True, day: str = "20261024") -> Path:
    folder = tmp / f"match-{day}-{short}"
    folder.mkdir()
    if video:
        writer = cv2.VideoWriter(
            str(folder / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 360),
        )
        assert writer.isOpened()
        frame = _bar(_logo())
        for _ in range(4):
            writer.write(frame)
        writer.release()
    (folder / "timeline.jsonl").write_text('{"t_ms": 0}\n', encoding="utf-8")
    (folder / "anchors.jsonl").write_text('{"t_ms": 0}\n', encoding="utf-8")
    return folder


def _allow(path: Path, folder: Path) -> None:
    digest = H._sha256(folder / "video.mp4") if (folder / "video.mp4").is_file() else "ab" * 32
    path.write_text(json.dumps({"sources": [{
        "match_id": folder.name[-6:],
        "folder": folder.name,
        "video_sha256": digest,
    }]}), encoding="utf-8")


def _labels(path: Path, items=None) -> None:
    if items is None:
        items = [{
            "role": "score", "label": "1", "roi": "score_left", "frame_index": 0,
            "start_frame_index": 0, "end_frame_index": 0,
        }]
    path.write_text(json.dumps({"items": items}), encoding="utf-8")


def _snap(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): H._sha256(p) for p in folder.rglob("*") if p.is_file()}


def _validate(doc: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(doc, SCHEMA)


def test_held_out_match_is_refused_and_the_match_folder_is_unchanged(tmp_path):
    folder = _match(tmp_path, "084523")
    before = _snap(folder)
    out = tmp_path / "out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    # Even a matching video pin does not override the held-out list.
    try:
        H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "084523" in str(exc)
    else:
        raise AssertionError("held-out match was harvested")
    assert not out.exists()
    assert _snap(folder) == before


def test_match_outside_the_allowlist_is_refused(tmp_path):
    folder = _match(tmp_path, "999999")
    before = _snap(folder)
    out = tmp_path / "out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    try:
        H.harvest(folder, out, labels, ROOT / "templates" / "template_sources.json", HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "999999" in str(exc)
    else:
        raise AssertionError("unlisted match was harvested")
    assert not out.exists()
    assert _snap(folder) == before


def test_renamed_folder_is_refused_when_the_video_hash_does_not_match(tmp_path):
    """A held-out video dropped into an allowlisted folder name does not pass."""
    folder = _match(tmp_path, "102332")
    before = _snap(folder)
    out = tmp_path / "out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    try:
        H.harvest(folder, out, labels, ROOT / "templates" / "template_sources.json", HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "video_sha256" in str(exc) or "sha256" in str(exc)
    else:
        raise AssertionError("unpinned video was harvested")
    assert not out.exists()
    assert _snap(folder) == before
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"sources": [{
        "match_id": "102332",
        "folder": folder.name,
        "video_sha256": "a" * 64,
    }]}), encoding="utf-8")
    try:
        H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("wrong video hash was harvested")
    assert _snap(folder) == before


def test_output_inside_the_match_folder_is_refused(tmp_path):
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    out = folder / "templates-out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    try:
        H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("wrote inside the match folder")
    assert _snap(folder) == before
    assert not out.exists()


def test_a_bad_label_is_refused_before_any_write(tmp_path):
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels, [{
        "role": "clock", "label": "x5", "roi": "clock", "frame_index": 0,
        "start_frame_index": 0, "end_frame_index": 0,
    }])
    try:
        H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "x5" in str(exc)
    else:
        raise AssertionError("clock label x5 was accepted")
    _labels(labels, [{
        "role": "score", "label": "14", "roi": "score_left", "frame_index": 0,
        "start_frame_index": 0, "end_frame_index": 0,
    }])
    try:
        H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "14" in str(exc)
    else:
        raise AssertionError("a two-digit label was saved as one glyph")
    assert not (tmp_path / "out").exists()
    assert _snap(folder) == before


def test_a_time_range_that_misses_the_frame_is_refused(tmp_path):
    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels, [{
        "role": "score", "label": "1", "roi": "score_left", "frame_index": 0,
        "start_t_ms": 5000, "end_t_ms": 6000,
    }])
    try:
        H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("a time range that misses the frame was accepted")
    assert not (tmp_path / "out").exists()


def test_dry_run_lists_crops_and_writes_nothing(tmp_path):
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    out = tmp_path / "out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    doc = H.harvest(folder, out, labels, allow, HELD, HASHES, dry_run=True, now=NOW)
    assert not out.exists()
    assert _snap(folder) == before
    assert doc["templates"]
    assert {row["label"] for row in doc["templates"]} == {"1"}
    assert all(row["harvest_date"] == "2026-10-24T17:31:00Z" for row in doc["templates"])
    code = H.main([
        "--dry-run", "--match-dir", str(folder), "--labels", str(labels),
        "--out", str(out), "--allowlist", str(allow),
    ])
    assert code == 0
    assert not out.exists()
    assert _snap(folder) == before


def test_allowlisted_synthetic_match_writes_per_glyph_files_outside_and_is_deterministic(tmp_path):
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    first = tmp_path / "out-a"
    second = tmp_path / "out-b"
    kwargs = dict(
        labels_path=labels, allow_path=allow, held_path=HELD, hash_path=HASHES, now=NOW,
    )
    doc = H.harvest(folder, first, **kwargs)
    again = H.harvest(folder, second, **kwargs)
    assert _snap(folder) == before
    assert doc == again
    _validate(doc)
    labels_out = {row["label"] for row in doc["templates"]}
    assert labels_out == {"1"}
    assert {row["source_match_id"] for row in doc["templates"]} == {"555001"}
    assert {row["source_match_folder"] for row in doc["templates"]} == {folder.name}
    assert {row["harvest_date"] for row in doc["templates"]} == {"2026-10-24T17:31:00Z"}
    npy_rows = [row for row in doc["templates"] if row["path"].endswith(".npy")]
    assert npy_rows and all("__g0" in row["path"] for row in npy_rows)
    for row in doc["templates"]:
        blob = (first / row["path"]).read_bytes()
        assert H.hashlib.sha256(blob).hexdigest() == row["sha256"]
        assert blob == (second / row["path"]).read_bytes()
        if row["path"].endswith(".npy"):
            vec = np.load(io_bytes(blob))
            assert vec.ndim == 1 and vec.size == 10 * 14
    assert H.POWERSHELL_ONELINER in (first / "HARVEST_WINDOWS.txt").read_text(encoding="utf-8")
    assert "102332" in H.POWERSHELL_ONELINER and "\\" in H.POWERSHELL_ONELINER
    assert "GetFolderPath" in H.POWERSHELL_ONELINER
    assert "QorCap\\venv" in H.POWERSHELL_ONELINER


def io_bytes(blob: bytes):
    import io
    return io.BytesIO(blob)


def test_windows_doc_flag_writes_nothing(tmp_path, capsys):
    assert H.main(["--windows-doc"]) == 0
    assert "102332" in capsys.readouterr().out


def test_harvested_templates_round_trip_through_the_reader(tmp_path, monkeypatch):
    """Synthesize a bar, harvest it, install it, and read the same frame back.

    CI fails when the harvester's vectors are not what the reader matches.
    """
    folder = _match(tmp_path, "555001")
    labels = tmp_path / "labels.json"
    _labels(labels, [
        {"role": "score", "label": "1", "roi": "score_left", "frame_index": 0,
         "start_frame_index": 0, "end_frame_index": 3},
        {"role": "score", "label": "7", "roi": "score_right", "frame_index": 0,
         "start_frame_index": 0, "end_frame_index": 3},
        {"role": "clock", "label": "5:23", "roi": "clock", "frame_index": 0,
         "start_frame_index": 0, "end_frame_index": 3},
        {"role": "quarter", "label": "1st", "roi": "quarter", "frame_index": 0,
         "start_frame_index": 0, "end_frame_index": 3},
    ])
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    out = tmp_path / "out"
    doc = H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    _validate(doc)
    glyph_labels = {row["label"] for row in doc["templates"] if row["path"].endswith(".npy")}
    assert glyph_labels == {"1", "7", "5", "2", "3", "1st"}
    assert all(len(row["label"]) == 1 or row["role"] == "quarter" for row in doc["templates"])
    cap = cv2.VideoCapture(str(folder / "video.mp4"))
    ok, frame = cap.read()
    cap.release()
    assert ok and frame is not None

    import qorcap_template_guard as guard
    monkeypatch.setattr(guard, "PROVENANCE_PATH", out / "provenance.json")
    monkeypatch.setattr(guard, "SOURCES_PATH", allow)
    from qorcap_standard import standard_reader

    reader = standard_reader()
    fr = reader.read(frame)
    assert (fr.left_score, fr.right_score) == (1, 7), (fr.reason, fr.detail)
    assert fr.quarter == 1, (fr.reason, fr.quarter)
    assert fr.clock_text == "5:23" and fr.clock_seconds == 323, (fr.reason, fr.clock_text)
    assert fr.ok is True and fr.reason == "ok"
