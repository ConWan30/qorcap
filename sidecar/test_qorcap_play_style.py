"""Tests for T-0056 Practice-Mode play-style sequence tagger (info_only)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import qorcap_play_style as ps  # noqa: E402


def _make_session(
    practice_dir: Path,
    session_id: str,
    *,
    frames: int = 10,
    hid_rows: list[dict] | None = None,
    meta: dict | None = None,
    reader_stats: dict | None = None,
    score_events: list | None = None,
) -> Path:
    mdir = practice_dir / session_id
    mdir.mkdir(parents=True, exist_ok=True)
    if frames > 0:
        fc = "\n".join(
            json.dumps({"type": "frame", "file": "video.mp4", "frame": i, "pts_90k": i * 1500, "t_mono_us": i * 16667})
            for i in range(frames)
        )
        (mdir / "frame_clock.jsonl").write_text(fc + "\n", encoding="utf-8")
    if hid_rows is not None:
        (mdir / "inputs.jsonl").write_text(
            "\n".join(json.dumps(r) for r in hid_rows) + ("\n" if hid_rows else ""),
            encoding="utf-8",
        )
    if meta is not None:
        (mdir / "cod_session.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if reader_stats is not None:
        (mdir / "cod_reader_stats.json").write_text(json.dumps(reader_stats) + "\n", encoding="utf-8")
    if score_events is not None:
        stl = {"schema": "qorcap-score-timeline/v0", "signals": {"score_timeline": {"events": score_events}}}
        (mdir / "score_timeline.json").write_text(json.dumps(stl) + "\n", encoding="utf-8")
    (mdir / "receipt.v0.json").write_text(json.dumps({"schema": "qor.receipt.v0", "id": session_id}) + "\n", encoding="utf-8")
    return mdir


def _tap_hid() -> list[dict]:
    return [
        {"type": "in", "t_us": 100_000, "r2": 200, "l2": 200},
        {"type": "in", "t_us": 180_000, "r2": 0, "l2": 0},
        {"type": "in", "t_us": 300_000, "r2": 200, "l2": 200},
        {"type": "in", "t_us": 390_000, "r2": 0, "l2": 0},
    ]


def _hipfire_hid() -> list[dict]:
    return [
        {"type": "in", "t_us": 100_000, "r2": 220, "l2": 0},
        {"type": "in", "t_us": 450_000, "r2": 0, "l2": 0},
        {"type": "in", "t_us": 600_000, "r2": 220, "l2": 0},
        {"type": "in", "t_us": 950_000, "r2": 0, "l2": 0},
    ]


def _ads_spray_hid() -> list[dict]:
    return [
        {"type": "in", "t_us": 100_000, "r2": 220, "l2": 220},
        {"type": "in", "t_us": 500_000, "r2": 0, "l2": 0},
        {"type": "in", "t_us": 600_000, "r2": 220, "l2": 220},
        {"type": "in", "t_us": 1_050_000, "r2": 0, "l2": 0},
    ]


# ----------------------------------------------------------------- 1. Sequence mapping & custom counts
def test_sequence_mapping_default_and_custom_counts(tmp_path):
    pdir = tmp_path / "QorCap Practice"
    out = tmp_path / "out"
    _make_session(pdir, "match-20261125-210000", hid_rows=_tap_hid())
    _make_session(pdir, "match-20261125-211000", hid_rows=_hipfire_hid())
    _make_session(pdir, "match-20261125-212000", hid_rows=_ads_spray_hid())
    _make_session(
        pdir,
        "match-20261125-213000",
        hid_rows=_ads_spray_hid(),
        reader_stats={"owner_row_ever_read": True},
    )

    summary = ps.tag_practice_sessions(pdir, out, date_filter="20261125")
    styles = [r["play_style"] for r in summary["sessions"]]
    assert styles == ["TAP", "HIPFIRE", "SPRAY_MIX", "OPTIC"]
    assert all(r["controller_check"] == "AGREES" for r in summary["sessions"])
    assert summary["sequence_mismatch"] is False

    # Read-back helper works cleanly for T-0055 ingest
    tag = ps.read_play_style_tag(out, "match-20261125-213000")
    assert tag is not None and tag["play_style"] == "OPTIC"

    # Custom counts: 2 TAP + 2 HIPFIRE
    out2 = tmp_path / "out_custom"
    counts = ps.parse_counts_spec("TAP=2,HIPFIRE=2")
    sum2 = ps.tag_practice_sessions(pdir, out2, date_filter="20261125", sequence_counts=counts)
    assert [r["play_style"] for r in sum2["sessions"]] == ["TAP", "TAP", "HIPFIRE", "HIPFIRE"]


# ----------------------------------------------------------------- 2. Firing Range vs Multiplayer vs UNKNOWN
def test_firing_range_vs_multiplayer_and_unknown_fallback(tmp_path):
    # Firing range explicitly identified -> FIRING_RANGE
    pdir = tmp_path / "fr"
    _make_session(pdir, "match-20261125-210000", hid_rows=_tap_hid())
    _make_session(pdir, "match-20261125-211000", hid_rows=_hipfire_hid())
    _make_session(pdir, "match-20261125-212000", hid_rows=_ads_spray_hid())
    _make_session(
        pdir,
        "match-20261125-213000",
        hid_rows=_ads_spray_hid(),
        meta={"mode": "firing_range"},
    )
    res_fr = ps.tag_practice_sessions(pdir, tmp_path / "out_fr")
    assert res_fr["sessions"][3]["play_style"] == "FIRING_RANGE"

    # Unclear 4th session -> UNKNOWN + environment_unclear flag (never guesses)
    pdir_unk = tmp_path / "unk"
    _make_session(pdir_unk, "match-20261125-210000", hid_rows=_tap_hid())
    _make_session(pdir_unk, "match-20261125-211000", hid_rows=_hipfire_hid())
    _make_session(pdir_unk, "match-20261125-212000", hid_rows=_ads_spray_hid())
    _make_session(pdir_unk, "match-20261125-213000", hid_rows=_ads_spray_hid())
    res_unk = ps.tag_practice_sessions(pdir_unk, tmp_path / "out_unk")
    last = res_unk["sessions"][3]
    assert last["play_style"] == "UNKNOWN"
    assert "environment_unclear" in last["flags"]


# ----------------------------------------------------------------- 3. Bad session skipped & mismatch reported
def test_bad_session_skipped_and_flagged_without_silent_shift(tmp_path):
    pdir = tmp_path / "bad_mid"
    _make_session(pdir, "match-20261125-210000", hid_rows=_tap_hid())
    # Crashed session #2: 0 HID reports + missing frame_clock
    _make_session(pdir, "match-20261125-210500", frames=0, hid_rows=[])
    # Session #3 is actually HIPFIRE
    _make_session(pdir, "match-20261125-211000", hid_rows=_hipfire_hid())

    summary = ps.tag_practice_sessions(pdir, tmp_path / "out_bad")
    assert summary["sequence_mismatch"] is True
    assert summary["skipped_invalid"] == 1
    bad_rec = summary["sessions"][1]
    assert bad_rec["status"] == "SKIPPED_INVALID"
    assert bad_rec["play_style"] == "SKIPPED"
    assert {"missing_frame_clock", "no_hid_reports", "sequence_mismatch"}.issubset(set(bad_rec["flags"]))
    # Later valid session keeps explicit warning flag instead of shifting silently
    next_rec = summary["sessions"][2]
    assert next_rec["play_style"] == "HIPFIRE"
    assert "after_skipped_session" in next_rec["flags"]


# ----------------------------------------------------------------- 4. Cross-check never mutates tag
def test_controller_crosscheck_agrees_disagrees_unmeasured_never_mutates_tag(tmp_path):
    pdir = tmp_path / "xcheck"
    # Slot 0 is TAP, give it long ADS spray -> DISAGREES, yet play_style stays TAP
    _make_session(pdir, "match-20261125-210000", hid_rows=_ads_spray_hid())
    # Slot 1 is HIPFIRE, give it only heartbeat / no R2 presses -> UNMEASURED, yet play_style stays HIPFIRE
    _make_session(pdir, "match-20261125-211000", hid_rows=[{"type": "in", "t_us": 1000, "r2": 0, "l2": 0}])

    summary = ps.tag_practice_sessions(pdir, tmp_path / "out_xc")
    s0, s1 = summary["sessions"]
    assert s0["play_style"] == "TAP"
    assert s0["controller_check"] == "DISAGREES"
    assert "controller_check_disagrees" in s0["flags"]
    assert s1["play_style"] == "HIPFIRE"
    assert s1["controller_check"] == "UNMEASURED"


# ----------------------------------------------------------------- 5. Zero floats & read-only sealed bundle
def test_no_floats_and_read_only_outside_sealed_bundle(tmp_path):
    pdir = tmp_path / "ro"
    mdir = _make_session(pdir, "match-20261125-210000", hid_rows=_tap_hid())
    before_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in mdir.iterdir()
        if p.is_file()
    }
    out = tmp_path / "out_ro"
    summary = ps.tag_practice_sessions(pdir, out)

    def _reject_float(tok: str):
        raise AssertionError(f"float emitted in play_style artifact: {tok}")

    sidecar_path = out / "match-20261125-210000" / ps.SIDECAR_NAME
    json.loads(sidecar_path.read_text(encoding="utf-8"), parse_float=_reject_float)
    json.loads((out / "play_style_summary.v0.json").read_text(encoding="utf-8"), parse_float=_reject_float)
    ps.assert_no_floats(summary)

    after_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in mdir.iterdir()
        if p.is_file()
    }
    assert after_hashes == before_hashes

    # Must refuse writing inside the practice directory / sealed bundle
    with pytest.raises(ps.PlayStyleContractError):
        ps.tag_practice_sessions(pdir, pdir / "inside_out")
