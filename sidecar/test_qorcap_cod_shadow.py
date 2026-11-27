"""Tests for T-0055 Practice-Mode Post-Match MNv4 Shadow Runner & Active-Learning Sync."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import cv2
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import qorcap_cod_post_nov21 as cod199  # noqa: E402
import qorcap_cod_shadow as shadow  # noqa: E402
import qorcap_match  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_digit_img(val: int, width: int = 320, height: int = 240) -> np.ndarray:
    """Create a 16:9-ish frame with a crisp bottom-right synthetic ammo HUD glyph row."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    # Draw white digits matching CoD bottom-right mag box [0.70*W:0.92*W, 0.77*H:0.94*H]
    x = int(0.75 * width)
    y = int(0.89 * height)
    cv2.putText(
        img,
        str(val),
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (250, 250, 250),
        2,
        cv2.LINE_AA,
    )
    return img


def _draw_diag_hitmarker(img: np.ndarray) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    cy, cx = h // 2, w // 2
    for dx_s, dy_s in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        for r in range(5, 13):
            out[cy + dy_s * r, cx + dx_s * r] = (255, 255, 255)
    return out


def _write_synthetic_match(match_dir: Path, *, with_equipment: bool = False) -> tuple[Path, list[Path]]:
    """Build a read-only CoD practice match folder with video.mp4, inputs, frame_clock, receipt.v0."""
    match_dir.mkdir(parents=True, exist_ok=True)
    w, h = 320, 240
    fps = 60
    n_frames = 30
    video_path = match_dir / "video.mp4"
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (w, h),
    )
    assert writer.isOpened()
    try:
        for idx in range(n_frames):
            # Ammo decrements cleanly from 30 -> 29 at frame 8, and 29 -> 28 at frame 18
            if idx < 8:
                frame = _make_digit_img(30, w, h)
            elif idx < 18:
                frame = _make_digit_img(29, w, h)
            else:
                frame = _make_digit_img(28, w, h)
            if 9 <= idx <= 12:
                frame = _draw_diag_hitmarker(frame)
            writer.write(frame)
    finally:
        writer.release()

    # Frame clock aligned to inputs.jsonl t_us (pts_90k step = 1500 @ 60fps)
    fc_lines = []
    for idx in range(n_frames):
        fc_lines.append(
            json.dumps({
                "type": "frame",
                "file": "video.mp4",
                "frame": idx,
                "pts_90k": idx * 1500,
                "t_mono_us": idx * 16667,
            })
        )
    (match_dir / "frame_clock.jsonl").write_text("\n".join(fc_lines) + "\n", encoding="utf-8")

    # Inputs: two R2 spans covering frame 8 (~133ms) and frame 18 (~300ms)
    # Span 1: short single tap (hipfire, no L2)
    # Span 2: ADS spray (with optional L1/R1 equipment press)
    in_rows = [
        {"type": "hb", "t_us": 0, "faults": 0},
        {"type": "in", "t_us": 100000, "r2": 200, "l2": 0, "buttons": []},
        {"type": "in", "t_us": 200000, "r2": 0, "l2": 0, "buttons": []},
        {"type": "in", "t_us": 250000, "r2": 210, "l2": 220, "buttons": ["L1"] if with_equipment else []},
        {"type": "in", "t_us": 480000, "r2": 0, "l2": 0, "buttons": []},
        {"type": "hb", "t_us": 500000, "faults": 0},
    ]
    (match_dir / "inputs.jsonl").write_text(
        "\n".join(json.dumps(r) for r in in_rows) + "\n",
        encoding="utf-8",
    )

    # Sealed receipt.v0 + #199 companion files that must stay byte-identical
    receipt = {
        "schema": "qor.receipt.v0",
        "match_id": match_dir.name,
        "verdict": "UNVERIFIED",
        "score": {"top": 10, "bottom": 8},
    }
    r199 = {"schema": "cod_reader_stats/v0", "info_only": True, "hit": 1, "kill": 0}
    r_path = match_dir / "receipt.v0.json"
    c_path = match_dir / "cod_reader_stats.json"
    r_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    c_path.write_text(json.dumps(r199, sort_keys=True) + "\n", encoding="utf-8")

    protected = sorted(p for p in match_dir.iterdir() if p.is_file())
    return video_path, protected


# ----------------------------------------------------------------- 1. Schema & split firewall
def test_schema_columns_and_split_check_constraint(tmp_path):
    db_path = tmp_path / "sessions_index.sqlite"
    conn = shadow.init_shadow_db(db_path)
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(shadow_hit_events)").fetchall()]
        assert tuple(cols) == shadow.SHADOW_HIT_EVENTS_COLUMNS
        base_row = (
            "ev_1", "sess_1", "train_pool", 10, 166, "cod_gunfire_hit_v2", "a" * 64,
            1, "FULL_AUTO_SPRAY", 0, 1, "NONE", "GUNFIRE_HIT", 8412,
            0, 0, None, "DISAGREEMENT_FP", None, None, "2026-11-25T19:00:00Z",
        )
        placeholders = ",".join("?" for _ in shadow.SHADOW_HIT_EVENTS_COLUMNS)
        for bad_split in ("test_pool", "", "holdout", None):
            bad_row = list(base_row)
            bad_row[2] = bad_split
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"INSERT INTO shadow_hit_events VALUES ({placeholders})", tuple(bad_row))

        conn.execute(f"INSERT INTO shadow_hit_events VALUES ({placeholders})", base_row)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError):
        shadow.validate_dataset_split("other_split")


def test_session_split_mixing_rejected(tmp_path):
    db_path = tmp_path / "sessions_index.sqlite"
    conn = shadow.init_shadow_db(db_path)
    row_tp = {
        "event_id": "e1",
        "session_id": "s_mix",
        "dataset_split": "train_pool",
        "frame_idx": 1,
        "t_ammo_ms": 16,
        "model_id": "cod_gunfire_hit_v2",
        "model_sha256": "b" * 64,
        "ads_active": 1,
        "fire_mode": "FULL_AUTO_SPRAY",
        "equipment_window_excluded": 0,
        "ammo_gate_passed": 1,
        "reader_199_verdict": "NONE",
        "mnv4_verdict": "GUNFIRE_HIT",
        "confidence_bps": 8000,
        "xp_popup_corroborated": 0,
        "killfeed_corroborated": 0,
        "audio_peak_ms": None,
        "triage_reason": "DISAGREEMENT_FP",
        "human_label": None,
        "human_labeled_at_utc": None,
        "created_at_utc": "2026-11-25T19:00:00Z",
    }
    try:
        shadow.upsert_shadow_hit_events(conn, [row_tp])
        row_fh = dict(row_tp, event_id="e2", dataset_split="fresh_holdout")
        with pytest.raises(shadow.HoldoutFirewallError):
            shadow.upsert_shadow_hit_events(conn, [row_fh])
    finally:
        conn.close()


# ----------------------------------------------------------------- 2. Tensor parity
def test_tensor_builder_shape_symmetry_and_luma():
    base = np.zeros((240, 320, 3), dtype=np.uint8)
    hit = _draw_diag_hitmarker(base)
    frames = [base] + [hit] * 8
    tensor = shadow.build_mnv4_v2_tensor(frames)
    assert tensor.shape == (16, 96, 96)
    assert tensor.dtype == np.float32
    # At step 0 (t_ammo -> t_ammo+1), all 4 diagonal hitmarker arms light up symmetrically
    pos_0 = tensor[0]
    sym_0 = tensor[1]
    assert float(pos_0.max()) > 0.9
    assert float(sym_0.max()) > 0.9

    # Asymmetric one-sided blob collapses to 0 under 4-quadrant sym_min
    asym = np.zeros((96, 96), dtype=np.float32)
    asym[10:15, 10:15] = 0.8
    sym_asym = shadow.fold_4quadrant_sym_min(asym, tol_px=1)
    assert float(sym_asym.max()) == 0.0


# ----------------------------------------------------------------- 3. No-float RFC 8785 JCS
def test_canonical_shadow_json_rejects_floats_and_matches_jcs():
    rec = shadow.build_shadow_hit_record(
        frame_idx=14822,
        t_ammo_ms=247033,
        model_id="cod_gunfire_hit_v2",
        model_sha256="c" * 64,
        ammo_gate_passed=True,
        verdict="GUNFIRE_HIT",
        confidence_bps=8412,
        ads_active=True,
        fire_mode="FULL_AUTO_SPRAY",
        triage_reason="DISAGREEMENT_FP",
    )
    out = shadow.canonical_shadow_json(rec)
    # Parse back rejecting any JSON float token (parse_float raises immediately)
    def _reject_float(token: str):
        raise AssertionError(f"float token emitted in JCS output: {token}")

    parsed = json.loads(out, parse_float=_reject_float)
    assert parsed == rec
    # Matches Python stdlib compact sorted serialization for ASCII keys with zero floats
    assert out == json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    # Verify integer bounds and exact RFC 8785 parity with Rust tests/vectors/jcs_vector.json
    shadow.assert_no_floats(parsed)
    vec_path = HERE.parent / "tests" / "vectors" / "jcs_vector.json"
    if vec_path.is_file():
        vec = json.loads(vec_path.read_text(encoding="utf-8"))
        canon_vec = shadow._jcs_encode(vec["input"])
        assert canon_vec == vec["canonical"]
        assert hashlib.sha256(canon_vec.encode("utf-8")).hexdigest() == vec["sha256"]

    with pytest.raises(shadow.ShadowContractError):
        shadow.assert_no_floats({"unsafe_int": 1 << 53})

    # Cross-check with JS JSON.stringify canonicalization when node is installed
    import shutil

    node = shutil.which("node")
    if node:
        js = (
            "const s = JSON.parse(process.argv[1]);"
            "function jcs(v){"
            "if(Array.isArray(v))return '['+v.map(jcs).join(',')+']';"
            "if(v&&typeof v==='object'){return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+jcs(v[k])).join(',')+'}';}"
            "return JSON.stringify(v);}"
            "process.stdout.write(jcs(s));"
        )
        proc = subprocess.run([node, "-e", js, out], capture_output=True, text=True, check=True)
        assert proc.stdout == out

    bad_float = dict(rec, confidence_bps=0.8412)
    with pytest.raises(shadow.ShadowContractError):
        shadow.canonical_shadow_json(bad_float)

    with pytest.raises(shadow.ShadowContractError):
        shadow.assert_no_floats({"nested": [{"x": 1.0}]})


# ----------------------------------------------------------------- 4. Triage P1-P4 & 15-25 queue
def test_triage_priorities_p1_to_p4_and_consensus_exclusion():
    def _ev(eid, *, r199="NONE", mnv4="GUNFIRE_HIT", bps=8500, ads=1, fm="FULL_AUTO_SPRAY", xp=0, kf=0):
        e = {
            "event_id": eid,
            "frame_idx": int(eid.split("_")[1]),
            "ads_active": ads,
            "fire_mode": fm,
            "equipment_window_excluded": 0,
            "ammo_gate_passed": 1,
            "reader_199_verdict": r199,
            "mnv4_verdict": mnv4,
            "confidence_bps": bps,
            "xp_popup_corroborated": xp,
            "killfeed_corroborated": kf,
            "audio_peak_ms": None,
        }
        e["triage_reason"] = shadow.assign_triage_reason(e)
        return e

    p1_ads = _ev("ev_1", r199="NONE", mnv4="GUNFIRE_HIT", bps=9000, ads=1, fm="FULL_AUTO_SPRAY")
    p1_hip = _ev("ev_2", r199="NONE", mnv4="GUNFIRE_HIT", bps=9500, ads=0, fm="SINGLE_TAP")
    p2_abs = _ev("ev_3", r199="ABSTAIN", mnv4="NO_HIT", bps=2000, ads=1, fm="FULL_AUTO_SPRAY")
    p3_bnd = _ev("ev_4", r199="NONE", mnv4="ABSTAIN", bps=4400, ads=1, fm="FULL_AUTO_SPRAY")
    p4_tap = _ev("ev_5", r199="HIT", mnv4="NO_HIT", bps=1000, ads=1, fm="SINGLE_TAP", xp=1)
    cons_pos = _ev("ev_6", r199="HIT", mnv4="GUNFIRE_HIT", bps=9200, ads=1, fm="FULL_AUTO_SPRAY", xp=1)
    cons_neg = _ev("ev_7", r199="NONE", mnv4="NO_HIT", bps=800, ads=1, fm="FULL_AUTO_SPRAY")

    assert p1_ads["triage_reason"] == "DISAGREEMENT_FP"
    assert p1_hip["triage_reason"] == "DISAGREEMENT_FP"
    assert p2_abs["triage_reason"] == "ABSTAIN_FILL"
    assert p3_bnd["triage_reason"] == "UNCERTAIN_BOUNDARY"
    assert p4_tap["triage_reason"] == "QUOTA_BALANCE"
    assert cons_pos["triage_reason"] == "CONSENSUS_AUTO"
    assert cons_neg["triage_reason"] == "CONSENSUS_AUTO"

    queue = shadow.select_review_queue([cons_pos, p4_tap, p3_bnd, p2_abs, p1_hip, p1_ads, cons_neg])
    ids = [e["event_id"] for e in queue]
    assert ids == ["ev_1", "ev_2", "ev_3", "ev_4", "ev_5"]

    # Cap at 25 max items when a noisy match emits > 25 triaged windows
    many = [_ev(f"ev_{100 + i}", r199="NONE", mnv4="GUNFIRE_HIT", bps=7000 + i) for i in range(40)]
    capped = shadow.select_review_queue(many)
    assert len(capped) == 25


# ----------------------------------------------------------------- 5. Review pack + H/N/S writeback
def test_review_pack_composite_strip_and_writeback(tmp_path):
    match_dir = tmp_path / "matches" / "sess_pack_01"
    _write_synthetic_match(match_dir)
    out_root = tmp_path / "shadow_out"
    db_path = tmp_path / "index" / "sessions_index.sqlite"

    res = shadow.run_shadow_on_match(
        match_dir,
        dataset_split="train_pool",
        model_id="cod_gunfire_hit_v2",
        out_root=out_root,
        db_path=db_path,
        infer_fn=lambda _tensor: 8412,
        extract_clips=False,
    )
    assert res["events_count"] >= 1
    assert res["review_queue_count"] >= 1

    review_dir = Path(res["review_dir"])
    manifest_path = review_dir / "manifest.json"
    assert manifest_path.is_file()
    assert (review_dir / "review.html").is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["dataset_split"] == "train_pool"
    first_item = manifest["items"][0]
    strip_file = review_dir / first_item["strip_png"]
    assert strip_file.is_file() and strip_file.stat().st_size > 100
    strip_img = cv2.imread(str(strip_file))
    assert strip_img is not None and strip_img.shape[0] > 280 and strip_img.shape[1] > 800

    # Keystroke H -> GUNFIRE_HIT write-back into SQLite & manifest.json
    lbl = shadow.write_human_label(
        db_path,
        first_item["event_id"],
        "H",
        manifest_path=manifest_path,
        labeled_at_utc="2026-11-25T20:05:00Z",
    )
    assert lbl == "GUNFIRE_HIT"
    conn = shadow.init_shadow_db(db_path)
    try:
        row = conn.execute(
            "SELECT human_label, human_labeled_at_utc FROM shadow_hit_events WHERE event_id = ?",
            (first_item["event_id"],),
        ).fetchone()
        assert row["human_label"] == "GUNFIRE_HIT"
        assert row["human_labeled_at_utc"] == "2026-11-25T20:05:00Z"
    finally:
        conn.close()


# ----------------------------------------------------------------- 6. receipt.v0 & #199 bit-identical
def test_receipt_v0_and_reader_199_bit_identical_shadow_on_vs_off(tmp_path):
    import shutil

    m_off = tmp_path / "match_off"
    _write_synthetic_match(m_off)
    m_on = tmp_path / "match_on"
    shutil.copytree(m_off, m_on)
    files_off = sorted(p for p in m_off.iterdir() if p.is_file())
    files_on = sorted(p for p in m_on.iterdir() if p.is_file())
    hashes_before = {p.name: _sha256(p) for p in files_on}

    fake_model = tmp_path / "cod_gunfire_hit_v2.onnx"
    fake_model.write_bytes(b"fake-onnx-placeholder")
    out_root = tmp_path / "shadow_out"
    db_path = tmp_path / "index.sqlite"

    # Shadow OFF via qorcap_match hook
    ran_off = qorcap_match.maybe_run_cod_mnv4_shadow(
        m_off,
        "cod",
        environ={"QORCAP_LANE": "practice", "QORCAP_SHADOW_MNV4": "0"},
        out_root=out_root,
        db_path=db_path,
    )
    assert ran_off is False

    # Shadow ON via qorcap_match hook
    ran_on = qorcap_match.maybe_run_cod_mnv4_shadow(
        m_on,
        "cod",
        environ={
            "QORCAP_LANE": "practice",
            "QORCAP_SHADOW_MNV4_MODEL": str(fake_model),
            "QORCAP_SHADOW_SPLIT": "fresh_holdout",
        },
        out_root=out_root,
        db_path=db_path,
        infer_fn=lambda _t: 8900,
    )
    assert ran_on is True

    # Every file in match_on must remain unchanged and match match_off byte-for-byte
    assert sorted(p.name for p in m_on.iterdir()) == sorted(hashes_before.keys())
    for p_off, p_on in zip(sorted(files_off), sorted(files_on)):
        assert _sha256(p_off) == _sha256(p_on) == hashes_before[p_on.name]

    # #199 reader outputs stay deterministic on identically decoded frames
    cbt_off = cod199.CenterBoxTrigger()
    cbt_on = cod199.CenterBoxTrigger()
    sample = _draw_diag_hitmarker(_make_digit_img(29))
    assert cbt_off.step(sample, t_us=1000)["state"] == cbt_on.step(sample, t_us=1000)["state"]

    # Refuses writing outputs inside the match folder
    with pytest.raises(shadow.ShadowRunnerError):
        shadow.run_shadow_on_match(
            m_on,
            dataset_split="train_pool",
            out_root=m_on / "shadow_inside",
            db_path=db_path,
            infer_fn=lambda _t: 5000,
        )


# ----------------------------------------------------------------- 7. Colab firewall rejects holdout & NULL
def test_colab_export_rejects_fresh_holdout_and_unlabeled_rows(tmp_path):
    db_path = tmp_path / "sessions_index.sqlite"
    conn = shadow.init_shadow_db(db_path)
    base = {
        "model_id": "cod_gunfire_hit_v2",
        "model_sha256": "d" * 64,
        "ads_active": 1,
        "fire_mode": "FULL_AUTO_SPRAY",
        "equipment_window_excluded": 0,
        "ammo_gate_passed": 1,
        "reader_199_verdict": "NONE",
        "mnv4_verdict": "GUNFIRE_HIT",
        "confidence_bps": 8200,
        "xp_popup_corroborated": 0,
        "killfeed_corroborated": 0,
        "audio_peak_ms": None,
        "triage_reason": "DISAGREEMENT_FP",
        "created_at_utc": "2026-11-25T19:00:00Z",
    }
    rows = [
        dict(base, event_id="tp_labeled_1", session_id="s_tp", dataset_split="train_pool", frame_idx=1, t_ammo_ms=16, human_label="NO_HIT", human_labeled_at_utc="2026-11-25T19:05:00Z"),
        dict(base, event_id="tp_labeled_2", session_id="s_tp", dataset_split="train_pool", frame_idx=2, t_ammo_ms=33, human_label="GUNFIRE_HIT", human_labeled_at_utc="2026-11-25T19:05:05Z"),
        dict(base, event_id="tp_unlabeled", session_id="s_tp", dataset_split="train_pool", frame_idx=3, t_ammo_ms=50, human_label=None, human_labeled_at_utc=None),
        dict(base, event_id="tp_skip", session_id="s_tp", dataset_split="train_pool", frame_idx=4, t_ammo_ms=66, human_label="SKIP", human_labeled_at_utc="2026-11-25T19:05:10Z"),
        dict(base, event_id="fh_labeled", session_id="s_fh", dataset_split="fresh_holdout", frame_idx=5, t_ammo_ms=83, human_label="NO_HIT", human_labeled_at_utc="2026-11-25T19:05:15Z"),
        dict(base, event_id="fh_unlabeled", session_id="s_fh", dataset_split="fresh_holdout", frame_idx=6, t_ammo_ms=100, human_label=None, human_labeled_at_utc=None),
    ]
    try:
        shadow.upsert_shadow_hit_events(conn, rows)
    finally:
        conn.close()

    # Valid export pulls ONLY labeled train_pool rows
    export_dir = tmp_path / "colab_out"
    manifest = shadow.export_train_pool_for_colab(db_path, export_dir)
    assert manifest["dataset_split"] == "train_pool"
    assert manifest["fresh_holdout_rows_exported"] == 0
    assert manifest["exported_rows"] == 2
    chunk_path = export_dir / manifest["chunks"][0]["file"]
    with tarfile.open(chunk_path, "r:gz") as tf:
        data = tf.extractfile("shadow_train_pool.jsonl").read().decode("utf-8").splitlines()
    exported_ids = [json.loads(line)["event_id"] for line in data]
    assert exported_ids == ["tp_labeled_1", "tp_labeled_2"]

    # Explicit fresh_holdout request or holdout session fails closed with zero output
    with pytest.raises(shadow.HoldoutFirewallError):
        shadow.export_train_pool_for_colab(db_path, tmp_path / "bad_1", requested_split="fresh_holdout")
    assert not (tmp_path / "bad_1").exists()

    with pytest.raises(shadow.HoldoutFirewallError):
        shadow.export_train_pool_for_colab(db_path, tmp_path / "bad_2", session_ids=["s_fh"])
    assert not (tmp_path / "bad_2").exists()

    # Direct validator firewall tests
    with pytest.raises(shadow.HoldoutFirewallError):
        shadow.validate_colab_export_rows([rows[4]])  # fresh_holdout
    with pytest.raises(shadow.HoldoutFirewallError):
        shadow.validate_colab_export_rows([rows[2]])  # human_label IS NULL


# ----------------------------------------------------------------- 8. Equipment L1/R1 & lane gating
def test_equipment_l1_r1_excluded_and_lane_gating(tmp_path):
    match_dir = tmp_path / "match_equip"
    _write_synthetic_match(match_dir, with_equipment=True)
    out_root = tmp_path / "shadow_out"
    db_path = tmp_path / "index.sqlite"
    scored_frames = []

    def _spy_infer(_tensor):
        scored_frames.append(1)
        return 7500

    res = shadow.run_shadow_on_match(
        match_dir,
        dataset_split="train_pool",
        out_root=out_root,
        db_path=db_path,
        infer_fn=_spy_infer,
        extract_clips=False,
    )
    conn = shadow.init_shadow_db(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM shadow_hit_events WHERE session_id = ?", (match_dir.name,)).fetchall()]
    finally:
        conn.close()
    assert rows
    # L1 was active on Span 2 -> all logged events from Span 2 are marked equipment_window_excluded=1 and skipped by MNv4
    assert all(r["equipment_window_excluded"] == 1 for r in rows)
    assert scored_frames == []
    assert res["review_queue_count"] == 0

    # Non-practice / non-CoD / missing model return False
    fake_model = tmp_path / "m.onnx"
    fake_model.write_bytes(b"x")
    assert qorcap_match.maybe_run_cod_mnv4_shadow(
        match_dir, "cod", environ={"QORCAP_LANE": "real", "QORCAP_SHADOW_MNV4_MODEL": str(fake_model), "QORCAP_SHADOW_SPLIT": "train_pool"}
    ) is False
    assert qorcap_match.maybe_run_cod_mnv4_shadow(
        match_dir, "madden", environ={"QORCAP_LANE": "practice", "QORCAP_SHADOW_MNV4_MODEL": str(fake_model), "QORCAP_SHADOW_SPLIT": "train_pool"}
    ) is False


# ----------------------------------------------------------------- 9. AG-SCOUT B1-B3 regression tests
def _write_custom_video_match(
    match_dir: Path,
    *,
    mag_schedule: list[int],
    r2_end_us: int = 1_200_000,
) -> Path:
    """Create a synthetic match folder with configurable per-frame magazine ammo sequence."""
    match_dir.mkdir(parents=True, exist_ok=True)
    w, h = 320, 240
    fps = 60
    n_frames = len(mag_schedule)
    video_path = match_dir / "video.mp4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert writer.isOpened()
    try:
        for mag in mag_schedule:
            writer.write(_make_digit_img(mag, w, h))
    finally:
        writer.release()

    fc_lines = [
        json.dumps({"type": "frame", "file": "video.mp4", "frame": idx, "pts_90k": idx * 1500, "t_mono_us": idx * 16667})
        for idx in range(n_frames)
    ]
    (match_dir / "frame_clock.jsonl").write_text("\n".join(fc_lines) + "\n", encoding="utf-8")
    in_rows = [
        {"type": "hb", "t_us": 0, "faults": 0},
        {"type": "in", "t_us": 50_000, "r2": 220, "l2": 220, "buttons": []},
        {"type": "in", "t_us": r2_end_us, "r2": 0, "l2": 0, "buttons": []},
        {"type": "hb", "t_us": r2_end_us + 50_000, "faults": 0},
    ]
    (match_dir / "inputs.jsonl").write_text("\n".join(json.dumps(r) for r in in_rows) + "\n", encoding="utf-8")
    return video_path


def test_b1_multi_round_ammo_drop_logged(tmp_path):
    """B1: 2..5 round drops per step are logged; huge jumps (>5) are excluded as reloads/glitches."""
    # Frame 6: 30->28 (-2), Frame 16: 28->24 (-4), Frame 26: 24->10 (-14, ignored)
    schedule = [30] * 6 + [28] * 10 + [24] * 10 + [10] * 15
    match_dir = tmp_path / "sess_b1"
    _write_custom_video_match(match_dir, mag_schedule=schedule, r2_end_us=900_000)

    db_path = tmp_path / "index.sqlite"
    res = shadow.run_shadow_on_match(
        match_dir,
        dataset_split="train_pool",
        out_root=tmp_path / "out",
        db_path=db_path,
        infer_fn=lambda _t: 8500,
        extract_clips=False,
    )
    assert res["events_count"] == 2
    conn = shadow.init_shadow_db(db_path)
    try:
        frames = [r["frame_idx"] for r in conn.execute("SELECT frame_idx FROM shadow_hit_events ORDER BY frame_idx").fetchall()]
    finally:
        conn.close()
    assert frames == [6, 16]


def test_b2_killfeed_corroborates_without_match_json(tmp_path, monkeypatch):
    """B2: read_killfeed_lines runs with tag=None (OWNER_TAG fallback) even without match.json."""
    import qorcap_cod_killfeed as kf_mod

    schedule = [30] * 8 + [29] * 22
    match_dir = tmp_path / "sess_b2"
    _write_custom_video_match(match_dir, mag_schedule=schedule)
    assert not (match_dir / "match.json").exists()

    seen_tags = []

    def _fake_kf(_frame, tag=None):
        seen_tags.append(tag)
        return {"ok": True, "lines": [{"killer": kf_mod.SELF, "victim": kf_mod.NOT_SELF}]}

    monkeypatch.setattr(kf_mod, "read_killfeed_lines", _fake_kf)

    db_path = tmp_path / "index.sqlite"
    shadow.run_shadow_on_match(
        match_dir,
        dataset_split="train_pool",
        out_root=tmp_path / "out",
        db_path=db_path,
        infer_fn=lambda _t: 9100,
        extract_clips=False,
    )
    assert seen_tags and all(t is None for t in seen_tags)
    conn = shadow.init_shadow_db(db_path)
    try:
        row = dict(conn.execute("SELECT * FROM shadow_hit_events").fetchone())
    finally:
        conn.close()
    assert row["killfeed_corroborated"] == 1
    assert row["triage_reason"] != "DISAGREEMENT_FP"


def test_b3_delayed_xp_and_killfeed_corroboration_within_2000ms(tmp_path, monkeypatch):
    """B3: XP popup / killfeed arriving +500 ms after t_ammo (well past t_ammo+8=133ms) is still captured."""
    import qorcap_cod_killfeed as kf_mod
    import qorcap_cod_post_nov21 as cod199_mod

    # 60 frames = ~1000 ms @ 60fps; t_ammo at frame 8 (~133 ms), t_ammo+8 at frame 16 (~266 ms),
    # delayed XP + killfeed arrive during frames 36..44 (~600..733 ms, i.e. +467..+600 ms after t_ammo).
    schedule = [30] * 8 + [29] * 52
    match_dir = tmp_path / "sess_b3"
    _write_custom_video_match(match_dir, mag_schedule=schedule, r2_end_us=950_000)

    # Encode frame tag brightness in top-left pixel so fake readers know which frame is decoded
    cap = cv2.VideoCapture(str(match_dir / "video.mp4"))
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
    cap.release()
    writer = cv2.VideoWriter(str(match_dir / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 60, (320, 240))
    for i, fr in enumerate(frames):
        if 36 <= i <= 44:
            fr[0:20, 0:20] = (0, 240, 240)
        writer.write(fr)
    writer.release()

    def _fake_xp(frame):
        # Delayed XP marker well after frame 16 (t_ammo + 8)
        if int(frame[5, 5, 1]) > 180:
            return {"ok": True, "state": "XP_POPUP", "xp_value": 100}
        return {"ok": True, "state": "NONE", "xp_value": None}

    def _fake_kf(frame, tag=None):
        del tag
        if int(frame[5, 5, 2]) > 180:
            return {"ok": True, "lines": [{"killer": kf_mod.SELF}]}
        return {"ok": True, "lines": []}

    monkeypatch.setattr(cod199_mod, "read_kill_xp_popup", _fake_xp)
    monkeypatch.setattr(kf_mod, "read_killfeed_lines", _fake_kf)

    db_path = tmp_path / "index.sqlite"
    shadow.run_shadow_on_match(
        match_dir,
        dataset_split="train_pool",
        out_root=tmp_path / "out",
        db_path=db_path,
        inputs_path=match_dir / "inputs.jsonl",
        infer_fn=lambda _t: 8900,
        extract_clips=False,
    )
    conn = shadow.init_shadow_db(db_path)
    try:
        row = dict(conn.execute("SELECT * FROM shadow_hit_events").fetchone())
    finally:
        conn.close()
    assert row["xp_popup_corroborated"] == 1
    assert row["killfeed_corroborated"] == 1
    assert row["triage_reason"] != "DISAGREEMENT_FP"


# ----------------------------------------------------------------- 10. N4 configurable T / tau gates
def test_n4_load_model_calibration_and_fail_closed(tmp_path):
    model_sha = "ab" * 32
    metrics_v2_2 = tmp_path / "train_metrics_v2_2.json"
    metrics_v2_2.write_text(
        json.dumps({
            "model_id": "cod_gunfire_hit_v2_2",
            "temperature": 1.0,
            "tau_low_bps": 4500,
            "tau_high_bps": 7900,
            "onnx_sha256": model_sha.upper(),
        }),
        encoding="utf-8",
    )
    cal = shadow.load_model_calibration(metrics_v2_2, expected_model_sha256=model_sha)
    assert cal["model_id"] == "cod_gunfire_hit_v2_2"
    assert cal["temperature"] == 1.0
    assert cal["tau_low_bps"] == 4500
    assert cal["tau_high_bps"] == 7900
    assert cal["onnx_sha256"] == model_sha

    # Legacy fallback via calibrated_gate.{tau_low, tau_high}
    metrics_gate = tmp_path / "gate_only.json"
    metrics_gate.write_text(
        json.dumps({
            "temperature": 3.2001,
            "calibrated_gate": {"tau_low": 0.4, "tau_high": 0.6},
        }),
        encoding="utf-8",
    )
    cal_gate = shadow.load_model_calibration(metrics_gate)
    assert cal_gate["temperature"] == pytest.approx(3.2001)
    assert cal_gate["tau_low_bps"] == 4000
    assert cal_gate["tau_high_bps"] == 6000

    # Fail closed on SHA mismatch, missing file, non-positive temperature, and bad gates
    with pytest.raises(shadow.ShadowRunnerError):
        shadow.load_model_calibration(metrics_v2_2, expected_model_sha256="cd" * 32)
    with pytest.raises(shadow.ShadowRunnerError):
        shadow.load_model_calibration(tmp_path / "missing.json")
    for bad_payload in (
        {"temperature": 0.0, "tau_low_bps": 3000, "tau_high_bps": 7000},
        {"temperature": -1.5, "tau_low_bps": 3000, "tau_high_bps": 7000},
        {"temperature": True, "tau_low_bps": 3000, "tau_high_bps": 7000},
        {"temperature": 1.0, "tau_low_bps": 7000, "tau_high_bps": 7000},
        {"temperature": 1.0, "tau_low_bps": -1, "tau_high_bps": 7000},
        {"temperature": 1.0, "tau_low_bps": 3000, "tau_high_bps": 10001},
        {"temperature": 1.0, "tau_low_bps": True, "tau_high_bps": 7000},
        {"tau_low_bps": 3000, "tau_high_bps": 7000},
    ):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps(bad_payload), encoding="utf-8")
        with pytest.raises(shadow.ShadowRunnerError):
            shadow.load_model_calibration(bad_file)


def test_n4_resolve_calibration_precedence(tmp_path):
    # 1. Defaults (v2 backwards-compatible)
    cfg_def = shadow.resolve_calibration(environ={})
    assert cfg_def["model_id"] == shadow.DEFAULT_MODEL_ID
    assert cfg_def["temperature"] == shadow.DEFAULT_TEMPERATURE
    assert cfg_def["tau_low_bps"] == shadow.DEFAULT_TAU_LOW_BPS
    assert cfg_def["tau_high_bps"] == shadow.DEFAULT_TAU_HIGH_BPS
    assert cfg_def["calibration_source"] == "default_v2"

    # 2. Metrics JSON overrides defaults
    mp = tmp_path / "m.json"
    mp.write_text(
        json.dumps({"model_id": "cod_gunfire_hit_v2_2", "temperature": 1.0, "tau_low_bps": 4500, "tau_high_bps": 7900}),
        encoding="utf-8",
    )
    cfg_m = shadow.resolve_calibration(metrics_path=mp, environ={})
    assert (cfg_m["model_id"], cfg_m["temperature"], cfg_m["tau_low_bps"], cfg_m["tau_high_bps"]) == (
        "cod_gunfire_hit_v2_2",
        1.0,
        4500,
        7900,
    )
    assert cfg_m["calibration_source"] == "metrics_json"

    # 3. Env overrides metrics JSON
    env = {
        shadow.ENV_METRICS: str(mp),
        shadow.ENV_TEMPERATURE: "1.25",
        shadow.ENV_TAU_HIGH_BPS: "8200",
    }
    cfg_env = shadow.resolve_calibration(environ=env)
    assert (cfg_env["temperature"], cfg_env["tau_low_bps"], cfg_env["tau_high_bps"]) == (1.25, 4500, 8200)
    assert cfg_env["calibration_source"] == "metrics_json+env"

    # 4. Explicit overrides env + metrics JSON
    cfg_exp = shadow.resolve_calibration(
        model_id="cod_custom",
        tau_low_bps=4000,
        tau_high_bps=8500,
        environ=env,
    )
    assert (cfg_exp["model_id"], cfg_exp["temperature"], cfg_exp["tau_low_bps"], cfg_exp["tau_high_bps"]) == (
        "cod_custom",
        1.25,
        4000,
        8500,
    )
    assert cfg_exp["calibration_source"] == "metrics_json+env+explicit"


def test_n4_shadow_runner_hook_and_cli_respect_v2_2_calibration(tmp_path, monkeypatch):
    match_dir = tmp_path / "sess_n4"
    _write_synthetic_match(match_dir)
    fake_model = tmp_path / "cod_gunfire_hit_v2_2.onnx"
    fake_model.write_bytes(b"fake-v22-onnx")
    model_sha = _sha256(fake_model)

    metrics_path = tmp_path / "train_metrics_v2_2.json"
    metrics_path.write_text(
        json.dumps({
            "model_id": "cod_gunfire_hit_v2_2",
            "temperature": 1.0,
            "tau_low_bps": 4500,
            "tau_high_bps": 7900,
            "onnx_sha256": model_sha,
        }),
        encoding="utf-8",
    )

    # Under legacy v2 (3500/6500), confidence=7200 -> GUNFIRE_HIT; under v2.2 (4500/7900), 7200 -> ABSTAIN
    out_root = tmp_path / "out_hook"
    db_path = tmp_path / "index.sqlite"
    ran = qorcap_match.maybe_run_cod_mnv4_shadow(
        match_dir,
        "cod",
        environ={
            "QORCAP_LANE": "practice",
            "QORCAP_SHADOW_MNV4_MODEL": str(fake_model),
            "QORCAP_SHADOW_MNV4_METRICS": str(metrics_path),
            "QORCAP_SHADOW_SPLIT": "train_pool",
        },
        out_root=out_root,
        db_path=db_path,
        infer_fn=lambda _t: 7200,
    )
    assert ran is True

    # Strict no-float check on hashed shadow_hits.jsonl
    jsonl_lines = (out_root / match_dir.name / shadow.SHADOW_JSONL_NAME).read_text(encoding="utf-8").splitlines()
    assert jsonl_lines
    def _reject_float(tok: str):
        raise AssertionError(f"float emitted in shadow_hits.jsonl: {tok}")
    for line in jsonl_lines:
        rec = json.loads(line, parse_float=_reject_float)
        assert rec["model_id"] == "cod_gunfire_hit_v2_2"
        assert rec["verdict"] == "ABSTAIN"
        assert rec["confidence_bps"] == 7200

    manifest = json.loads((out_root / shadow.REVIEW_ROOT_NAME / match_dir.name / "manifest.json").read_text(encoding="utf-8"))
    shadow.assert_no_floats(manifest)
    assert "temperature" not in manifest
    assert manifest["temperature_milli"] == 1000
    assert manifest["tau_low_bps"] == 4500
    assert manifest["tau_high_bps"] == 7900
    assert manifest["calibration_source"] == "metrics_json"

    # Verify CLI analyze passes resolved temperature to _make_onnx_infer_fn & applies --tau-high-bps override
    seen_temps: list[float] = []
    def _stub_ort(_mpath, temperature: float = shadow.DEFAULT_TEMPERATURE):
        seen_temps.append(temperature)
        return lambda _t: shadow.logits_to_confidence_bps([0.0, 0.9], temperature=temperature)

    monkeypatch.setattr(shadow, "_make_onnx_infer_fn", _stub_ort)
    # logits [0.0, 0.9] @ T=1.0 -> sigmoid(0.9)=7109 bps (ABSTAIN at 7900, GUNFIRE_HIT with --tau-high-bps 7000)
    rc = shadow.main([
        "analyze",
        "--match-dir", str(match_dir),
        "--split", "train_pool",
        "--model", str(fake_model),
        "--metrics", str(metrics_path),
        "--tau-high-bps", "7000",
        "--out", str(tmp_path / "out_cli"),
        "--db", str(tmp_path / "cli.sqlite"),
        "--no-clips",
    ])
    assert rc == 0
    assert seen_temps == [1.0]
    cli_lines = (tmp_path / "out_cli" / match_dir.name / shadow.SHADOW_JSONL_NAME).read_text(encoding="utf-8").splitlines()
    cli_rec = json.loads(cli_lines[0], parse_float=_reject_float)
    assert cli_rec["confidence_bps"] == 7109
    assert cli_rec["verdict"] == "GUNFIRE_HIT"


def test_n4_b1_unknown_model_without_calibration_fails_closed_and_n2_n3(tmp_path):
    match_dir = tmp_path / "sess_b1_guard"
    _write_synthetic_match(match_dir)
    unknown_onnx = tmp_path / "unknown_v2_2.onnx"
    unknown_onnx.write_bytes(b"unknown-onnx-bytes")
    unknown_sha = _sha256(unknown_onnx)
    assert unknown_sha != shadow.V2_LEGACY_MODEL_SHA256

    # B1: legacy v2 SHA allows default_v2 without metrics; any other SHA fails closed
    cfg_v2 = shadow.resolve_calibration(expected_model_sha256=shadow.V2_LEGACY_MODEL_SHA256, environ={})
    assert cfg_v2["calibration_source"] == "default_v2"
    assert cfg_v2["temperature_milli"] == 2929

    with pytest.raises(shadow.ShadowRunnerError, match="calibration required"):
        shadow.resolve_calibration(expected_model_sha256=unknown_sha, environ={})

    # Partial env override without metrics JSON must still fail closed on unknown SHA
    with pytest.raises(shadow.ShadowRunnerError, match="calibration required"):
        shadow.resolve_calibration(
            expected_model_sha256=unknown_sha,
            environ={shadow.ENV_TAU_HIGH_BPS: "8000"},
        )

    # B1 in run_shadow_on_match (infer_fn=None) and maybe_run_cod_mnv4_shadow hook
    with pytest.raises(shadow.ShadowRunnerError, match="calibration required"):
        shadow.run_shadow_on_match(
            match_dir,
            dataset_split="train_pool",
            model_path=unknown_onnx,
            out_root=tmp_path / "out_bad",
            db_path=tmp_path / "bad.sqlite",
            extract_clips=False,
        )

    msgs: list[str] = []
    ran = qorcap_match.maybe_run_cod_mnv4_shadow(
        match_dir,
        "cod",
        environ={
            "QORCAP_LANE": "practice",
            "QORCAP_SHADOW_MNV4_MODEL": str(unknown_onnx),
            "QORCAP_SHADOW_SPLIT": "train_pool",
        },
        out_root=tmp_path / "out_hook_bad",
        db_path=tmp_path / "hook_bad.sqlite",
        say=msgs.append,
    )
    assert ran is False
    assert any("calibration required" in m for m in msgs)

    # N3: unbound metrics JSON without onnx_sha256 rejected when bound against a real model SHA
    unbound_metrics = tmp_path / "unbound_metrics.json"
    unbound_metrics.write_text(
        json.dumps({"temperature": 1.0, "tau_low_bps": 4500, "tau_high_bps": 7900}),
        encoding="utf-8",
    )
    with pytest.raises(shadow.ShadowRunnerError, match="missing required 'onnx_sha256'"):
        shadow.load_model_calibration(unbound_metrics, expected_model_sha256=unknown_sha)

    # N2: warn via say when env overrides metrics JSON
    bound_metrics = tmp_path / "bound_metrics.json"
    bound_metrics.write_text(
        json.dumps({
            "model_id": "cod_gunfire_hit_v2_2",
            "temperature": 1.0,
            "tau_low_bps": 4500,
            "tau_high_bps": 7900,
            "onnx_sha256": unknown_sha,
        }),
        encoding="utf-8",
    )
    warn_msgs: list[str] = []
    ran_ovr = qorcap_match.maybe_run_cod_mnv4_shadow(
        match_dir,
        "cod",
        environ={
            "QORCAP_LANE": "practice",
            "QORCAP_SHADOW_MNV4_MODEL": str(unknown_onnx),
            "QORCAP_SHADOW_MNV4_METRICS": str(bound_metrics),
            "QORCAP_SHADOW_MNV4_TAU_HIGH_BPS": "8200",
            "QORCAP_SHADOW_SPLIT": "train_pool",
        },
        out_root=tmp_path / "out_hook_ovr",
        db_path=tmp_path / "hook_ovr.sqlite",
        infer_fn=lambda _t: 8000,
        say=warn_msgs.append,
    )
    assert ran_ovr is True
    assert any("WARNING:" in m and "metrics_json+env" in m for m in warn_msgs)



