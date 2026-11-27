"""T-0062 blind label pack builder tests (synthetic fixtures only, no inference)."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import cv2
import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import qorcap_cod_blind_pack as bp  # noqa: E402
import qorcap_cod_shadow as shadow  # noqa: E402

W, H = 320, 240


def _digit_frame(val: int, seed: int = 0) -> np.ndarray:
    img = np.zeros((H, W, 3), dtype=np.uint8)
    cy, cx = H // 2, W // 2
    rng = np.random.default_rng(seed)
    img[cy - 48 : cy + 48, cx - 48 : cx + 48] = rng.integers(0, 60, (96, 96, 3), dtype=np.uint8)
    cv2.putText(img, str(val), (int(0.75 * W), int(0.89 * H)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (250, 250, 250), 2, cv2.LINE_AA)
    return img


def _write_sidecar_files(match_dir: Path, n_frames: int, *, r2_end_us: int = 1_200_000, equipment: bool = False) -> None:
    match_dir.mkdir(parents=True, exist_ok=True)
    fc = [json.dumps({"type": "frame", "file": "video.mp4", "frame": i, "pts_90k": i * 1500, "t_mono_us": i * 16667}) for i in range(n_frames)]
    (match_dir / "frame_clock.jsonl").write_text("\n".join(fc) + "\n", encoding="utf-8")
    rows = [
        {"type": "hb", "t_us": 0, "faults": 0},
        {"type": "in", "t_us": 50_000, "r2": 220, "l2": 220, "buttons": ["R1"] if equipment else []},
        {"type": "in", "t_us": r2_end_us, "r2": 0, "l2": 0, "buttons": []},
        {"type": "hb", "t_us": r2_end_us + 50_000, "faults": 0},
    ]
    (match_dir / "inputs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _frames(schedule: list[int]) -> list[np.ndarray]:
    return [_digit_frame(m, seed=i) for i, m in enumerate(schedule)]


class _InMemoryCapture:
    """Stand-in for ``cv2.VideoCapture`` that serves in-memory frames (no media file is decoded)."""

    frames: list = []

    def __init__(self, _path):
        self._i = 0

    def isOpened(self):
        return True

    def grab(self):
        return self._i < len(self.frames)

    def retrieve(self):
        f = self.frames[self._i]
        self._i += 1
        return True, f.copy()

    def get(self, _prop):
        return 60.0

    def release(self):
        pass


# 30->28 (-2) at 6, 28->24 (-4) at 16, 24->10 (-14 reload/glitch, gated out) at 26
SCHEDULE = [30] * 6 + [28] * 10 + [24] * 10 + [10] * 15


def _synthetic_pack(tmp_path: Path, name: str, seed: int) -> Path:
    match_dir = tmp_path / "practice_synth"
    if not match_dir.exists():
        _write_sidecar_files(match_dir, len(SCHEDULE), r2_end_us=900_000)
    out = tmp_path / name
    bp.build_session_pack(match_dir, out, seed=seed, frames=_frames(SCHEDULE))
    return out


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_gate_matches_current_shadow_runner(tmp_path, monkeypatch):
    match_dir = tmp_path / "practice_gate"
    _write_sidecar_files(match_dir, len(SCHEDULE), r2_end_us=900_000)
    (match_dir / "video.mp4").write_bytes(b"")  # empty marker for the existence check; never decoded
    _InMemoryCapture.frames = _frames(SCHEDULE)
    monkeypatch.setattr(cv2, "VideoCapture", _InMemoryCapture)
    db_path = tmp_path / "index.sqlite"
    shadow.run_shadow_on_match(
        match_dir, dataset_split="train_pool", out_root=tmp_path / "shadow_out", db_path=db_path,
        infer_fn=lambda _t: 5000, extract_clips=False,
    )
    conn = shadow.init_shadow_db(db_path)
    try:
        shadow_frames = [r["frame_idx"] for r in conn.execute("SELECT frame_idx FROM shadow_hit_events ORDER BY frame_idx")]
    finally:
        conn.close()
    blind_frames = [w["frame_idx"] for w in bp.iter_gated_windows(match_dir, frames=_frames(SCHEDULE))]
    assert blind_frames == shadow_frames == [6, 16]
    assert bp.iter_gated_windows.__code__.co_names.count("AmmoGatedWindowBuilder") == 1
    assert "AmmoGatedWindowBuilder" in shadow.run_shadow_on_match.__code__.co_names


def _scaled_digit_frame(val: int, w: int, h: int) -> np.ndarray:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    cv2.putText(img, str(val), (int(0.70 * w), int(0.86 * h)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (250, 250, 250), 2, cv2.LINE_AA)
    return img


def test_center_crop_never_overlaps_ammo_roi():
    for w in range(96, 3841, 8):
        for h in (96, 120, 160, 178, 180, 240, 360, 480, 720, 1080, 1440, 2160):
            try:
                shadow.assert_center_crop_excludes_ammo_roi(w, h)
            except shadow.ShadowRunnerError:
                continue
            cx0, cx1, cy0, cy1 = shadow.center_crop_box(w, h)
            ax0, ax1, ay0, ay1 = shadow.ammo_roi_box(w, h)
            assert cx1 <= ax0 or cy1 <= ay0, (w, h)
    for w, h in ((1920, 1080), (1280, 720), (320, 240)):
        shadow.assert_center_crop_excludes_ammo_roi(w, h)
    for w, h in ((216, 160), (95, 200), (200, 95), (64, 64)):
        with pytest.raises(shadow.ShadowRunnerError):
            shadow.assert_center_crop_excludes_ammo_roi(w, h)


def test_small_frames_with_ammo_in_crop_are_rejected(tmp_path):
    # Scout T-0063 witness geometry: at 216x160 the ammo digits fall inside the center crop.
    match_dir = tmp_path / "practice_small"
    _write_sidecar_files(match_dir, 20, r2_end_us=900_000)
    frames = [_scaled_digit_frame(30, 216, 160)] * 6 + [_scaled_digit_frame(28, 216, 160)] * 14
    with pytest.raises(shadow.ShadowRunnerError, match="ammo-corner ROI"):
        list(bp.iter_gated_windows(match_dir, frames=frames))
    # At 320x240 changing only the ammo digits leaves every crop identical.
    a = list(bp.iter_gated_windows(match_dir, frames=[_scaled_digit_frame(30, 320, 240)] * 6 + [_scaled_digit_frame(28, 320, 240)] * 14))
    blank = np.zeros((96, 96, 3), dtype=np.uint8)
    assert [x["frame_idx"] for x in a] == [6]
    assert all(np.array_equal(c, blank) for c in a[0]["crops"])


def test_in_memory_gate_excludes_equipment_and_outside_span(tmp_path):
    frames = _frames(SCHEDULE)
    match_dir = tmp_path / "practice_mem"
    _write_sidecar_files(match_dir, len(SCHEDULE), r2_end_us=900_000)
    wins = list(bp.iter_gated_windows(match_dir, frames=frames))
    assert [w["frame_idx"] for w in wins] == [6, 16]
    assert all(len(w["crops"]) == 9 and w["crops"][0].shape == (96, 96, 3) for w in wins)
    assert [w["t_ammo_ms"] for w in wins] == [100, 266]

    short = tmp_path / "practice_short_span"
    _write_sidecar_files(short, len(SCHEDULE), r2_end_us=200_000)
    assert [w["frame_idx"] for w in bp.iter_gated_windows(short, frames=frames)] == [6]

    equip = tmp_path / "practice_equip"
    _write_sidecar_files(equip, len(SCHEDULE), r2_end_us=900_000, equipment=True)
    assert list(bp.iter_gated_windows(equip, frames=frames)) == []


def test_shuffle_and_hash_manifest_determinism(tmp_path):
    a = _synthetic_pack(tmp_path, "out_a", seed=20261127)
    b = _synthetic_pack(tmp_path, "out_b", seed=20261127)
    ha = json.loads((a / bp.HASH_MANIFEST).read_text(encoding="utf-8"))
    hb = json.loads((b / bp.HASH_MANIFEST).read_text(encoding="utf-8"))
    assert ha == hb
    assert (a / bp.HASH_MANIFEST).read_bytes() == (b / bp.HASH_MANIFEST).read_bytes()
    paths = {f["path"] for f in ha["files"]}
    assert {"review/manifest.json", "review/review.html", "sealed_key/key.json", "review/strips/s001.png"} <= paths
    for f in ha["files"]:
        data = (a / f["path"]).read_bytes()
        assert f["size"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert bp.shuffled_order(50, 7) == bp.shuffled_order(50, 7)
    assert bp.shuffled_order(50, 7) != bp.shuffled_order(50, 8)
    assert sorted(bp.shuffled_order(50, 7)) == list(range(50))
    key = json.loads((a / "sealed_key" / "key.json").read_text(encoding="utf-8"))
    assert key["seed"] == 20261127 and key["count"] == 2


def test_con_facing_leak_scan_clean_and_detects(tmp_path):
    out = _synthetic_pack(tmp_path, "out_leak", seed=3)
    review = out / bp.REVIEW_DIR
    leakage_hits = bp.scan_for_leaks(review)
    assert leakage_hits == []
    for p in (review / "strips").iterdir():
        assert p.name.startswith("s") and p.suffix == ".png"
        assert _png_size(p) == (bp.STRIP_W, bp.STRIP_H)
    manifest = json.loads((review / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["items"][0]) == {"id", "png"}

    dirty = tmp_path / "dirty"
    shutil.copytree(review, dirty)
    (dirty / "manifest.json").write_text('{"confidence_bps": 9100, "source_tier": 2, "a": "match-123456", "b": "20990101-000000"}', encoding="utf-8")
    (dirty / "strips" / "s001_verdict.png").write_bytes(b"x")
    chunk = b"tEXt" + b"Comment\x00p_cal=0.93"
    png = (dirty / "strips" / "s002.png").read_bytes()
    text = len(chunk[4:]).to_bytes(4, "big") + chunk + zlib.crc32(chunk).to_bytes(4, "big")
    (dirty / "strips" / "s002.png").write_bytes(png[:33] + text + png[33:])
    hits = " ".join(bp.scan_for_leaks(dirty))
    for token in ("confidence", "source_tier", "verdict", "p_cal", "session:match-123456", "session:20990101-000000"):
        assert token in hits


def test_hidden_key_outside_served_folder_and_has_no_scores(tmp_path):
    out = _synthetic_pack(tmp_path, "out_key", seed=11)
    key_path = out / "sealed_key" / "key.json"
    assert not str(key_path.resolve()).startswith(str((out / bp.REVIEW_DIR).resolve()))
    key = json.loads(key_path.read_text(encoding="utf-8"))
    for it in key["items"]:
        assert set(it) == {"id", "session", "frame", "t_ammo_ms"}
    assert sorted(it["frame"] for it in key["items"]) == [6, 16]
    for bad in ("bps", "confidence", "verdict", "logit", "p_cal", "tau", "model", "source_tier", "triage"):
        assert bad not in key_path.read_text(encoding="utf-8")


def test_inference_refused_nonzero_exit(tmp_path, capsys):
    for extra in (["--model", "m.onnx"], ["--model=m.pt"], ["--metrics", "x.json"], ["--tau-high-bps", "8800"]):
        rc = bp.main(["session", "--match-dir", str(tmp_path), "--out", str(tmp_path / "o"), "--seed", "1", *extra])
        assert rc != 0
    assert "REFUSED" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
    with pytest.raises(bp.InferenceRefusedError):
        bp.refuse_inference_args(["row", "--npz", "w.npz", "weights.pth"])
    src = Path(bp.__file__).read_text(encoding="utf-8")
    for call in ("onnxruntime", "InferenceSession", "load_onnx_model", "infer_fn", "torch"):
        assert call not in src


def test_single_row_info_only_mode(tmp_path):
    rng = np.random.default_rng(5)
    windows = rng.integers(0, 255, (4, 9, 104, 104, 3), dtype=np.uint8)
    npz = tmp_path / "synthetic_windows.npz"
    np.savez(npz, windows=windows, y=np.array([1, 0, 1, 0]), source_tier=np.array([2, 0, 2, 0]),
             confidence_bps=np.array([9000, 100, 9900, 4000]))
    out = tmp_path / "row_out"
    res = bp.build_single_row_pack(npz, 2, out)
    assert res["count"] == 1
    review = out / bp.REVIEW_DIR
    assert bp.scan_for_leaks(review) == []
    assert _png_size(review / "strips" / "s001.png") == (bp.STRIP_W, bp.STRIP_H)
    key = json.loads((out / "sealed_key" / "key.json").read_text(encoding="utf-8"))
    assert key["mode"] == "single_row" and key["items"] == [{"id": "s001", "npz": "synthetic_windows.npz", "row": 2}]
    with pytest.raises(bp.BlindPackError):
        bp.build_single_row_pack(npz, 9, tmp_path / "bad_row")
    with pytest.raises(bp.BlindPackError):
        bp.build_single_row_pack(npz, 0, tmp_path / "bad_key", windows_key="missing")
    with pytest.raises(bp.BlindPackError):
        bp.build_single_row_pack(npz, 0, out)


def test_serve_binds_loopback_only_and_labels_stay_outside(tmp_path):
    out = _synthetic_pack(tmp_path, "out_serve", seed=1)
    srv = bp.serve_blind_pack(out, port=0)
    try:
        host, port = srv.server_address[:2]
        assert host == "127.0.0.1"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"
        assert urllib.request.urlopen(base + "/review.html", timeout=5).status == 200
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/../sealed_key/key.json", timeout=5)
        req = urllib.request.Request(base + "/label", data=json.dumps({"id": "s001", "key": "h"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert urllib.request.urlopen(req, timeout=5).status == 200
        bad = urllib.request.Request(base + "/label", data=json.dumps({"id": "s001", "key": "X"}).encode(), method="POST")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad, timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    labels = json.loads((out / bp.LABELS_DIR / "labels.json").read_text(encoding="utf-8"))
    assert labels == {"s001": shadow.KEY_TO_HUMAN_LABEL["H"]}
    assert not any(p.name == "labels.json" for p in (out / bp.REVIEW_DIR).rglob("*"))


def test_no_context_clips_emitted(tmp_path):
    out = _synthetic_pack(tmp_path, "out_clips", seed=2)
    suffixes = {p.suffix.lower() for p in out.rglob("*") if p.is_file()}
    assert suffixes <= {".png", ".json", ".html"}
