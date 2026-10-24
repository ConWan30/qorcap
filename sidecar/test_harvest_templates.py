"""The harvest script refuses held-out and unknown matches and does not write into the match folder."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import harvest_templates as H  # noqa: E402


def _match(tmp: Path, short: str, *, video: bool = True) -> Path:
    folder = tmp / f"match-20261024-{short}"
    folder.mkdir()
    if video:
        writer = cv2.VideoWriter(str(folder / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 360))
        assert writer.isOpened()
        frame = np.zeros((360, 640, 3), np.uint8)
        frame[342:357, 220:246] = 255
        for _ in range(4):
            writer.write(frame)
        writer.release()
    (folder / "timeline.jsonl").write_text('{"t_ms": 0}\n', encoding="utf-8")
    (folder / "anchors.jsonl").write_text('{"t_ms": 0}\n', encoding="utf-8")
    return folder


def _labels(path: Path) -> None:
    path.write_text(json.dumps({"items": [{
        "role": "score", "label": "0", "roi": "score_left", "frame_index": 0, "t_ms": 0,
    }]}), encoding="utf-8")


def _snap(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): H._sha256(p) for p in folder.rglob("*") if p.is_file()}


def test_held_out_match_is_refused_and_the_match_folder_is_unchanged(tmp_path):
    folder = _match(tmp_path, "084523")
    before = _snap(folder)
    out = tmp_path / "out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"match_ids": ["084523", "102332"]}), encoding="utf-8")
    try:
        H.harvest(folder, out, labels, allow, ROOT / "templates" / "heldout.json",
                  ROOT / "sidecar" / "packs" / "held_out_sha256.json")
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
        H.harvest(folder, out, labels, ROOT / "templates" / "template_sources.json",
                  ROOT / "templates" / "heldout.json", ROOT / "sidecar" / "packs" / "held_out_sha256.json")
    except H.Refuse as exc:
        assert "999999" in str(exc)
    else:
        raise AssertionError("unlisted match was harvested")
    assert not out.exists()
    assert _snap(folder) == before


def test_output_inside_the_match_folder_is_refused(tmp_path):
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    out = folder / "templates-out"
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"match_ids": ["555001"]}), encoding="utf-8")
    try:
        H.harvest(folder, out, labels, allow,
                  ROOT / "templates" / "heldout.json", ROOT / "sidecar" / "packs" / "held_out_sha256.json")
    except H.Refuse as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("wrote inside the match folder")
    assert _snap(folder) == before
    assert not out.exists()


def test_allowlisted_synthetic_match_writes_outside_and_is_deterministic(tmp_path):
    """555001 is not a real template-source game and not a held-out game. The pixels are synthetic."""
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"match_ids": ["555001"]}), encoding="utf-8")
    first = tmp_path / "out-a"
    second = tmp_path / "out-b"
    kwargs = dict(labels_path=labels, allow_path=allow, held_path=ROOT / "templates" / "heldout.json",
                  hash_path=ROOT / "sidecar" / "packs" / "held_out_sha256.json")
    doc = H.harvest(folder, first, **kwargs)
    again = H.harvest(folder, second, **kwargs)
    assert _snap(folder) == before
    assert doc == again
    assert doc["templates"]
    assert {row["source_match_id"] for row in doc["templates"]} == {"555001"}
    for row in doc["templates"]:
        blob = (first / row["path"]).read_bytes()
        assert H.hashlib.sha256(blob).hexdigest() == row["sha256"]
        assert blob == (second / row["path"]).read_bytes()
    assert (first / "HARVEST_WINDOWS.txt").read_text(encoding="utf-8").splitlines()
    assert H.POWERSHELL_ONELINER in (first / "HARVEST_WINDOWS.txt").read_text(encoding="utf-8")
    assert "102332" in H.POWERSHELL_ONELINER and "\\" in H.POWERSHELL_ONELINER


def test_windows_doc_flag_writes_nothing(tmp_path, capsys):
    assert H.main(["--windows-doc"]) == 0
    assert "102332" in capsys.readouterr().out
