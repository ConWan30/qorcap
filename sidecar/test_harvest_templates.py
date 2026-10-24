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
os.environ.setdefault("QORTIME", "/tmp/qortime")
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "sidecar"))
sys.path.insert(0, os.environ["QORTIME"])
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


def _paint(logo, left: str, right: str, clock: str, *, quarter: str = "1", left_kind: str = "font") -> np.ndarray:
    """Font digits at the real ROIs. ``ring`` and ``spur`` are hole-safe stand-ins for 0 and 8."""
    frame = np.zeros((360, 640, 3), np.uint8)
    frame[341:357, 200:300] = (255, 255, 255)
    frame[341:356, 428:484] = (12, 12, 12)
    frame[340:358, 8:80] = cv2.cvtColor(logo, cv2.COLOR_GRAY2BGR)
    if left_kind == "ring":
        frame[346:355, 223:232] = (0, 0, 0)
        frame[348:353, 225:230] = (255, 255, 255)
    elif left_kind == "spur":
        frame[346:355, 224:227] = (0, 0, 0)
        frame[346:348, 227:231] = (0, 0, 0)
        frame[352:355, 227:231] = (0, 0, 0)
    else:
        cv2.putText(frame, left, (224, 355), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_8)
    cv2.putText(frame, right, (262, 355), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_8)
    cv2.putText(frame, clock, (447, 352), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (240, 240, 240), 1, cv2.LINE_8)
    cv2.putText(frame, quarter, (430, 353), cv2.FONT_HERSHEY_SIMPLEX, 0.28, (230, 230, 230), 1, cv2.LINE_8)
    return frame


def _digit_frames(logo) -> list[np.ndarray]:
    """Five bars whose glyphs cover score 0-9 and clock 0-9. Frame 0 is 1 / 7 / 5:23."""
    return [
        _paint(logo, "1", "7", "5:23"),
        _paint(logo, "0", "2", "0:14", left_kind="ring"),
        _paint(logo, "3", "4", "6:23"),
        _paint(logo, "5", "6", "8:19"),
        _paint(logo, "8", "9", "7:23", left_kind="spur"),
    ]


def _match(tmp: Path, short: str, *, video: bool = True, day: str = "20261024", frames=None) -> Path:
    folder = tmp / f"match-{day}-{short}"
    folder.mkdir()
    if video:
        writer = cv2.VideoWriter(
            str(folder / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 360),
        )
        assert writer.isOpened()
        if frames is None:
            frames = [_bar(_logo())] * 4
        for frame in frames:
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
    assert all(row["path"].startswith("score/555001__1__") for row in npy_rows)
    assert all(H.glyph_name_matches(row) for row in doc["templates"])
    assert {row["qortime_commit"] for row in doc["templates"]} == {doc["qortime_commit"]}
    assert len(doc["qortime_commit"]) == 40
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
    assert "QORTIME" in H.POWERSHELL_ONELINER and "--dry-run" in H.POWERSHELL_ONELINER
    from qorcap_match import QORTIME_PIN
    assert QORTIME_PIN == H.QORTIME_PIN


def io_bytes(blob: bytes):
    import io
    return io.BytesIO(blob)


def test_windows_doc_flag_writes_nothing(tmp_path, capsys):
    assert H.main(["--windows-doc"]) == 0
    assert "102332" in capsys.readouterr().out


def _full_labels() -> list[dict]:
    span = {"start_frame_index": 0, "end_frame_index": 4}
    return [
        {"role": "score", "label": "1", "roi": "score_left", "frame_index": 0, **span},
        {"role": "score", "label": "7", "roi": "score_right", "frame_index": 0, **span},
        {"role": "clock", "label": "5:23", "roi": "clock", "frame_index": 0, **span},
        {"role": "quarter", "label": "1st", "roi": "quarter", "frame_index": 0, **span},
        {"role": "score", "label": "0", "roi": "score_left", "frame_index": 1, **span},
        {"role": "score", "label": "2", "roi": "score_right", "frame_index": 1, **span},
        {"role": "clock", "label": "0:14", "roi": "clock", "frame_index": 1, **span},
        {"role": "score", "label": "3", "roi": "score_left", "frame_index": 2, **span},
        {"role": "score", "label": "4", "roi": "score_right", "frame_index": 2, **span},
        {"role": "clock", "label": "6:23", "roi": "clock", "frame_index": 2, **span},
        {"role": "score", "label": "5", "roi": "score_left", "frame_index": 3, **span},
        {"role": "score", "label": "6", "roi": "score_right", "frame_index": 3, **span},
        {"role": "clock", "label": "8:19", "roi": "clock", "frame_index": 3, **span},
        {"role": "score", "label": "8", "roi": "score_left", "frame_index": 4, **span},
        {"role": "score", "label": "9", "roi": "score_right", "frame_index": 4, **span},
        {"role": "clock", "label": "7:23", "roi": "clock", "frame_index": 4, **span},
    ]


def _install(monkeypatch, out: Path, allow: Path):
    import qorcap_template_guard as guard
    monkeypatch.setattr(guard, "PROVENANCE_PATH", out / "provenance.json")
    monkeypatch.setattr(guard, "SOURCES_PATH", allow)
    from qorcap_standard import standard_reader
    return standard_reader()


def test_two_labels_for_one_slot_are_refused(tmp_path):
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels, [
        {"role": "score", "label": "6", "roi": "score_left", "frame_index": 0,
         "start_frame_index": 0, "end_frame_index": 0},
        {"role": "score", "label": "8", "roi": "score_left", "frame_index": 0,
         "start_frame_index": 0, "end_frame_index": 0},
    ])
    try:
        H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "6" in str(exc) and "8" in str(exc)
    else:
        raise AssertionError("two labels for one slot were harvested")
    assert not (tmp_path / "out").exists()
    assert _snap(folder) == before


def test_a_nonempty_out_is_refused(tmp_path):
    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    out = tmp_path / "out"
    H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    before = _snap(out)
    try:
        H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "not empty" in str(exc)
    else:
        raise AssertionError("a second harvest wrote into a nonempty out")
    assert _snap(out) == before


def test_labels_with_a_utf8_bom_are_read(tmp_path):
    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    body = json.dumps({"items": [{
        "role": "score", "label": "1", "roi": "score_left", "frame_index": 0,
        "start_frame_index": 0, "end_frame_index": 0,
    }]})
    labels.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    doc = H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW)
    assert {row["label"] for row in doc["templates"]} == {"1"}


def test_missing_qortime_refuses_before_the_video_is_hashed(tmp_path, monkeypatch):
    """A miss refuses before any video byte is hashed.

    Unsetting QORTIME is not enough. CI checks the pin out at ``<repo>/qortime``,
    and ``find_qortime`` walks ``HERE.parent / "qortime"``. Point HERE at a
    scratch sidecar so that lookup cannot see the checkout.
    """
    import qorcap_match

    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)

    def boom(*_a, **_k):
        raise AssertionError("hashed or decoded the video before QorTime was resolved")

    monkeypatch.delenv("QORTIME", raising=False)
    monkeypatch.setenv("QORCAP_HOME", str(tmp_path / "no-qortime"))
    monkeypatch.setattr(qorcap_match, "HERE", tmp_path / "sidecar")
    monkeypatch.setattr(H, "_sha256", boom)
    monkeypatch.setattr(H, "iter_decoded", boom)
    try:
        H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW, qortime=None)
    except H.Refuse as exc:
        assert "QorTime" in str(exc)
    else:
        raise AssertionError("missing QorTime was harvested")
    assert not (tmp_path / "out").exists()


def test_qortime_is_resolved_before_the_video_hash(tmp_path, monkeypatch):
    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    order = []
    real_resolve = H._resolve_qortime
    real_sha = H._sha256

    def resolve(arg):
        order.append("qortime")
        return real_resolve(arg)

    def sha(path):
        order.append("hash")
        return real_sha(path)

    monkeypatch.setattr(H, "_resolve_qortime", resolve)
    monkeypatch.setattr(H, "_sha256", sha)
    H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, dry_run=True, now=NOW)
    assert order[0] == "qortime"
    assert "hash" in order and order.index("qortime") < order.index("hash")


def test_uppercase_allowlist_pin_matches(tmp_path):
    folder = _match(tmp_path, "555001")
    digest = H._sha256(folder / "video.mp4").upper()
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"sources": [{
        "match_id": "555001", "folder": folder.name, "video_sha256": digest,
    }]}), encoding="utf-8")
    labels = tmp_path / "labels.json"
    _labels(labels)
    doc = H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, now=NOW)
    assert doc["templates"][0]["video_sha256"] == digest.lower()


def test_a_partial_score_pack_abstains(tmp_path, monkeypatch):
    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    out = tmp_path / "out"
    H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    cap = cv2.VideoCapture(str(folder / "video.mp4"))
    ok, frame = cap.read()
    cap.release()
    assert ok
    from qorcap_template_guard import TEMPLATES_MISSING
    reader = _install(monkeypatch, out, allow)
    fr = reader.read(frame)
    assert fr.reason == TEMPLATES_MISSING
    assert fr.left_score is None and fr.right_score is None


def test_harvested_templates_round_trip_through_the_reader(tmp_path, monkeypatch):
    """Synthesize a bar, harvest every digit, install it, and read frame 0 back.

    CI fails when the harvester's vectors are not what the reader matches.
    Dropping one score digit or one clock digit must not leave a partial pack
    that can misread.
    """
    logo = _logo()
    folder = _match(tmp_path, "555001", frames=_digit_frames(logo))
    labels = tmp_path / "labels.json"
    _labels(labels, _full_labels())
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    out = tmp_path / "out"
    doc = H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    _validate(doc)
    npy_rows = [row for row in doc["templates"] if row["path"].endswith(".npy")]
    by_role = {}
    for row in npy_rows:
        by_role.setdefault(row["role"], set()).add(row["label"])
        assert row["path"].startswith(f"{row['role']}/555001__{row['label']}__")
        assert H.glyph_name_matches(row)
    assert by_role["score"] == set("0123456789")
    assert by_role["clock"] == set("0123456789")
    assert by_role["quarter"] == {"1st"}
    cap = cv2.VideoCapture(str(folder / "video.mp4"))
    ok, frame = cap.read()
    cap.release()
    assert ok and frame is not None

    from qorcap_template_guard import TEMPLATES_MISSING

    reader = _install(monkeypatch, out, allow)
    fr = reader.read(frame)
    assert (fr.left_score, fr.right_score) == (1, 7), (fr.reason, fr.detail)
    assert fr.quarter == 1, (fr.reason, fr.quarter)
    assert fr.clock_text == "5:23" and fr.clock_seconds == 323, (fr.reason, fr.clock_text)
    assert fr.ok is True and fr.reason == "ok"

    dropped = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    dropped["templates"] = [
        row for row in dropped["templates"]
        if not (row["role"] == "score" and row["label"] == "4")
    ]
    (out / "provenance.json").write_text(json.dumps(dropped), encoding="utf-8")
    short = _install(monkeypatch, out, allow)
    missed = short.read(frame)
    assert missed.reason == TEMPLATES_MISSING
    assert missed.left_score is None

    clockless = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    # Restore the score digit, then drop one clock digit. Scores may still read.
    full = json.loads(json.dumps(doc))
    clockless["templates"] = [
        row for row in full["templates"]
        if not (row["role"] == "clock" and row["label"] == "9")
    ]
    (out / "provenance.json").write_text(json.dumps(clockless), encoding="utf-8")
    no_clock = _install(monkeypatch, out, allow)
    held = no_clock.read(frame)
    assert (held.left_score, held.right_score) == (1, 7), (held.reason, held.detail)
    assert held.clock_seconds is None
    assert "9" not in no_clock.inner.pack.groups.get("clock", {})


def test_pin_preview_and_merge(tmp_path, capsys):
    import merge_template_provenance as merge
    import pin_template_source as pin
    import preview_frame as preview

    folder = _match(tmp_path, "102332")
    video = folder / "video.mp4"
    assert video.parent.name == "match-20261024-102332"
    sources = tmp_path / "sources.json"
    sources.write_text((ROOT / "templates" / "template_sources.json").read_text(encoding="utf-8"), encoding="utf-8")
    before = sources.read_text(encoding="utf-8")
    try:
        pin.pin("999999", video, sources)
    except SystemExit as exc:
        assert "999999" in str(exc.code)
    else:
        raise AssertionError("an unknown match id was pinned")
    assert sources.read_text(encoding="utf-8") == before
    digest = pin.pin("102332", video, sources)
    assert digest == digest.lower() and len(digest) == 64
    raw = sources.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    saved = json.loads(raw)
    row = next(item for item in saved["sources"] if item["match_id"] == "102332")
    assert row["video_sha256"] == digest
    assert saved["sources"][1]["video_sha256"] == ""

    png = tmp_path / "preview.png"
    index, measured, fps, count = preview.preview(video, png, frame=0, t_ms=None)
    assert index == 0 and png.is_file() and count >= 1 and fps > 0
    assert preview.main(["--video", str(video), "--frame", "0", "--out", str(tmp_path / "b.png")]) == 0
    assert "frame_index=0" in capsys.readouterr().out

    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    out = tmp_path / "out"
    H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    dest = tmp_path / "templates"
    assert merge.merge(dest, [out]) > 0
    merged = (dest / "provenance.json").read_bytes()
    assert b"\r" not in merged and merged.endswith(b"\n")
    snap = _snap(dest)
    try:
        merge.merge(dest, [out])
    except SystemExit as exc:
        assert "duplicate" in str(exc.code)
    else:
        raise AssertionError("a duplicate path was merged")
    assert _snap(dest) == snap


def test_a_held_out_filename_inside_an_allowlisted_match_is_refused(tmp_path):
    """Deleting the match-folder filename scan lets this harvest succeed."""
    folder = _match(tmp_path, "555001")
    (folder / "frame_170123.jpg").write_bytes(b"not-a-jpeg")
    before = _snap(folder)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    out = tmp_path / "out"
    try:
        H.harvest(folder, out, labels, allow, HELD, HASHES, now=NOW)
    except H.Refuse as exc:
        assert "170123" in str(exc)
        assert "frame_170123.jpg" in str(exc)
    else:
        raise AssertionError("a held-out file name was harvested")
    assert not out.exists()
    assert _snap(folder) == before


def test_a_video_sha_listed_in_the_held_out_table_is_refused(tmp_path):
    """Deleting the sha-table check lets this harvest succeed. The allowlist pin matches."""
    folder = _match(tmp_path, "555001")
    before = _snap(folder)
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    digest = H._sha256(folder / "video.mp4")
    table = tmp_path / "held_out_sha256.json"
    table.write_text(json.dumps({
        "sha256": {digest: "site/matches/match-20261024-170123/clips/held.mp4"},
    }), encoding="utf-8")
    labels = tmp_path / "labels.json"
    _labels(labels)
    out = tmp_path / "out"
    try:
        H.harvest(folder, out, labels, allow, HELD, table, now=NOW)
    except H.Refuse as exc:
        assert "held-out footage" in str(exc)
        assert "170123" in str(exc)
    else:
        raise AssertionError("a blocklisted video sha was harvested")
    assert not out.exists()
    assert _snap(folder) == before


def test_a_different_qortime_commit_warns_and_still_harvests(tmp_path, monkeypatch, capsys):
    folder = _match(tmp_path, "555001")
    allow = tmp_path / "allow.json"
    _allow(allow, folder)
    labels = tmp_path / "labels.json"
    _labels(labels)
    real = H._qortime_commit(Path(os.environ["QORTIME"]))
    monkeypatch.setattr(H, "QORTIME_PIN", "0" * 40)
    doc = H.harvest(folder, tmp_path / "out", labels, allow, HELD, HASHES, dry_run=True, now=NOW)
    err = capsys.readouterr().err
    assert f"warning: QorTime commit {real} differs from the Start Match pin {'0' * 40}" in err
    assert doc["templates"]
    assert not (tmp_path / "out").exists()


def _sources_copy(path: Path) -> str:
    text = (ROOT / "templates" / "template_sources.json").read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return text


def test_pin_refuses_a_video_outside_the_allowlisted_folder(tmp_path):
    import pin_template_source as pin

    folder = _match(tmp_path, "555001")
    video = folder / "video.mp4"
    sources = tmp_path / "sources.json"
    before = _sources_copy(sources)
    try:
        pin.pin("102332", video, sources)
    except SystemExit as exc:
        assert "match-20261024-102332" in str(exc.code)
        assert "555001" in str(exc.code)
    else:
        raise AssertionError("a video outside the allowlisted folder was pinned")
    assert sources.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("held_id", ["170123", "021003", "125430", "084523", "193534"])
def test_pin_refuses_a_held_out_id_anywhere_in_the_path(tmp_path, held_id):
    """The parent folder is the allowlisted match. The held-out id is in the file name."""
    import pin_template_source as pin

    folder = tmp_path / "match-20261024-102332"
    folder.mkdir()
    src_root = tmp_path / "src"
    src_root.mkdir()
    src = _match(src_root, "102332")
    video = folder / f"video-{held_id}.mp4"
    video.write_bytes((src / "video.mp4").read_bytes())
    assert video.parent.name == "match-20261024-102332"
    sources = tmp_path / "sources.json"
    before = _sources_copy(sources)
    try:
        pin.pin("102332", video, sources)
    except SystemExit as exc:
        assert held_id in str(exc.code)
    else:
        raise AssertionError(f"held-out id {held_id} in the path was pinned")
    assert sources.read_text(encoding="utf-8") == before


def test_pin_refuses_a_sha_listed_in_held_out_sha256(tmp_path):
    import pin_template_source as pin

    folder = _match(tmp_path, "102332")
    video = folder / "video.mp4"
    digest = pin.sha256_file(video)
    table = tmp_path / "held_out_sha256.json"
    table.write_text(json.dumps({
        "sha256": {digest: "site/matches/match-20261024-125430/clips/held.mp4"},
    }), encoding="utf-8")
    sources = tmp_path / "sources.json"
    before = _sources_copy(sources)
    try:
        pin.pin("102332", video, sources, table)
    except SystemExit as exc:
        assert "held-out" in str(exc.code)
        assert "125430" in str(exc.code)
    else:
        raise AssertionError("a blocklisted video sha was pinned")
    assert sources.read_text(encoding="utf-8") == before
