"""Practice-Mode Post-Match MNv4 Shadow Runner & Active-Learning Sync (info_only).

Reads sealed Practice Mode Call of Duty session artifacts after capture stops,
runs the T-0052 ``cod_gunfire_hit_v2`` ONNX spotter in CPU shadow mode paired
with the external deterministic ammo decrement gate (``t_ammo``) and #199
corroborators, logs every non-excluded window into ``shadow_hit_events``
(SQLite/DuckDB compatible), emits canonical RFC 8785 no-float ``qor.shadow_hit.v0``
records outside the match folder, generates the 2-minute ``practice_reviews/``
confirmation pack, and exports strictly ``dataset_split = 'train_pool'``
human-labeled samples for Colab retraining.

Architectural invariants:
* Never runs during active capture; executes strictly post-match after recording
  stops.
* Never alters #199 verdicts, match results, ``receipt.v0``, bundles, or locks.
* Never passes ammo pixels into MNv4 (``read_ammo_counter`` stays strictly outside
  the model as pre-gate and ``t_ammo`` temporal anchor).
* Excludes ``L1``/``R1`` equipment windows from ``GUNFIRE_HIT`` scoring.
* Rejects any float inside canonicalized ``qor.shadow_hit.v0`` JCS output;
  probabilities are quantized to integer basis points (``confidence_bps: 0..10000``).
* Enforces a hard firewall between ``train_pool`` and ``fresh_holdout``.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import hashlib
import html
import http.server
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

INFO_ONLY = True
COUNTED = False

SCHEMA = "qor.shadow_hit.v0"
OUT_DIR_NAME = "QorCapShadow"
REVIEW_ROOT_NAME = "practice_reviews"
SHADOW_JSONL_NAME = "shadow_hits.jsonl"
DEFAULT_DB_PATH = Path(r"D:\QorCap\index\sessions_index.sqlite")

ENV_LANE = "QORCAP_LANE"
ENV_FLAG = "QORCAP_SHADOW_MNV4"
ENV_MODEL = "QORCAP_SHADOW_MNV4_MODEL"
ENV_MODEL_ID = "QORCAP_SHADOW_MNV4_MODEL_ID"
ENV_METRICS = "QORCAP_SHADOW_MNV4_METRICS"
ENV_TEMPERATURE = "QORCAP_SHADOW_MNV4_TEMPERATURE"
ENV_TAU_LOW_BPS = "QORCAP_SHADOW_MNV4_TAU_LOW_BPS"
ENV_TAU_HIGH_BPS = "QORCAP_SHADOW_MNV4_TAU_HIGH_BPS"
ENV_SPLIT = "QORCAP_SHADOW_SPLIT"
ENV_DB = "QORCAP_SHADOW_DB"
ENV_OUT = "QORCAP_SHADOW_OUT"

V2_LEGACY_MODEL_SHA256 = "1e2ec29dfbfc9478e4e50a23a252d230f67fca129a6914de91e870788407a0f3"
DEFAULT_MODEL_ID = "cod_gunfire_hit_v2"
DEFAULT_TEMPERATURE = 2.9292
DEFAULT_TAU_LOW_BPS = 3500
DEFAULT_TAU_HIGH_BPS = 6500
BOUNDARY_MARGIN_BPS = 1000

CROP_SIZE = 96
WINDOW_FRAMES = 9  # [t_ammo .. t_ammo + 8]
TOL_PX = 1

TRIGGER_THRESHOLD = 60
L1_BIT = 1 << 4
R1_BIT = 1 << 5
EQUIPMENT_PRE_US = 500_000
EQUIPMENT_POST_US = 1_000_000
SINGLE_TAP_MAX_US = 160_000
MAX_AMMO_DROP_PER_STEP = 5
CORROBORATION_WINDOW_MS = 2000
CORROBORATION_SAMPLE_INTERVAL_MS = 100
FRAME_FILE = "video.mp4"

VALID_SPLITS = ("train_pool", "fresh_holdout")
VALID_MNV4_VERDICTS = ("GUNFIRE_HIT", "NO_HIT", "ABSTAIN")
VALID_FIRE_MODES = ("SINGLE_TAP", "FULL_AUTO_SPRAY", "UNKNOWN")
VALID_READER_199_VERDICTS = ("HIT", "KILL", "NONE", "ABSTAIN")
VALID_HUMAN_LABELS = ("GUNFIRE_HIT", "NO_HIT", "SKIP")
KEY_TO_HUMAN_LABEL = {
    "H": "GUNFIRE_HIT",
    "GUNFIRE_HIT": "GUNFIRE_HIT",
    "N": "NO_HIT",
    "NO_HIT": "NO_HIT",
    "S": "SKIP",
    "SKIP": "SKIP",
}

TRIAGE_PRIORITY = {
    "DISAGREEMENT_FP": 1,
    "ABSTAIN_FILL": 2,
    "UNCERTAIN_BOUNDARY": 3,
    "QUOTA_BALANCE": 4,
    "CONSENSUS_AUTO": 99,
    "EQUIPMENT_EXCLUDED": 100,
}

REVIEW_MIN_ITEMS = 15
REVIEW_MAX_ITEMS = 25
MAX_COLAB_CHUNK_BYTES = 35 * 1024 * 1024  # < 50 MiB upload limit

SHADOW_HIT_EVENTS_COLUMNS = (
    "event_id",
    "session_id",
    "dataset_split",
    "frame_idx",
    "t_ammo_ms",
    "model_id",
    "model_sha256",
    "ads_active",
    "fire_mode",
    "equipment_window_excluded",
    "ammo_gate_passed",
    "reader_199_verdict",
    "mnv4_verdict",
    "confidence_bps",
    "xp_popup_corroborated",
    "killfeed_corroborated",
    "audio_peak_ms",
    "triage_reason",
    "human_label",
    "human_labeled_at_utc",
    "created_at_utc",
)

SHADOW_HIT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS shadow_hit_events (
    event_id                  TEXT PRIMARY KEY,
    session_id                TEXT NOT NULL,
    dataset_split             TEXT NOT NULL CHECK (dataset_split IN ('train_pool', 'fresh_holdout')),
    frame_idx                 INTEGER NOT NULL,
    t_ammo_ms                 INTEGER NOT NULL,
    model_id                  TEXT NOT NULL,
    model_sha256              TEXT NOT NULL,
    ads_active                INTEGER NOT NULL,
    fire_mode                 TEXT NOT NULL,
    equipment_window_excluded INTEGER NOT NULL DEFAULT 0,
    ammo_gate_passed          INTEGER NOT NULL,
    reader_199_verdict        TEXT NOT NULL,
    mnv4_verdict              TEXT NOT NULL,
    confidence_bps            INTEGER NOT NULL,
    xp_popup_corroborated     INTEGER NOT NULL DEFAULT 0,
    killfeed_corroborated     INTEGER NOT NULL DEFAULT 0,
    audio_peak_ms             INTEGER,
    triage_reason             TEXT NOT NULL,
    human_label               TEXT,
    human_labeled_at_utc      TEXT,
    created_at_utc            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_hit_session_split
    ON shadow_hit_events(session_id, dataset_split);
"""


class ShadowRunnerError(Exception):
    """Base error for the Practice Mode MNv4 shadow runner."""


class ShadowContractError(ShadowRunnerError):
    """Raised when a canonical record violates no-float RFC 8785 JCS or schema rules."""


class HoldoutFirewallError(ShadowRunnerError):
    """Raised when fresh_holdout or unlabeled rows attempt to enter train_pool export."""


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _under(child: Path, parent: Path) -> bool:
    c = os.path.realpath(child)
    p = os.path.realpath(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def require_safe_leaf(name: str) -> str:
    if not name or name in (".", ".."):
        raise ShadowRunnerError("refusing empty or traversal session folder name")
    if os.path.isabs(name) or "/" in name or "\\" in name or "\x00" in name:
        raise ShadowRunnerError("refusing non-leaf session folder name")
    return name


def validate_dataset_split(split: str | None) -> str:
    if not isinstance(split, str) or split not in VALID_SPLITS:
        raise ValueError(
            f"dataset_split must be one of {VALID_SPLITS}, got {split!r}"
        )
    return split


def _ensure_out_outside_match(out_path: Path, match_dir: Path | None) -> None:
    if match_dir is not None and (_under(out_path, match_dir) or _under(match_dir, out_path)):
        raise ShadowRunnerError(
            f"refusing to write shadow artifacts inside match folder: {out_path}"
        )


def default_out_root(environ=None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_OUT):
        return Path(env[ENV_OUT])
    base = env.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / OUT_DIR_NAME


def default_db_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_DB):
        return Path(env[ENV_DB])
    if DEFAULT_DB_PATH.parent.is_dir():
        return DEFAULT_DB_PATH
    return default_out_root(env) / "sessions_index.sqlite"


# ----------------------------------------------------------------- RFC 8785 no-float JCS
def assert_no_floats(value, path: str = "$") -> None:
    """Reject any float or unsafe >2^53-1 integer before RFC 8785 JCS canonicalization."""
    if isinstance(value, float):
        raise ShadowContractError(f"float forbidden in RFC 8785 no-float JCS at {path}: {value!r}")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > (1 << 53) - 1:
            raise ShadowContractError(f"integer exceeds 2^53-1 JCS bound at {path}: {value!r}")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ShadowContractError(f"JSON object key must be str at {path}: {k!r}")
            assert_no_floats(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            assert_no_floats(item, f"{path}[{i}]")
        return
    raise ShadowContractError(f"unsupported type in canonical JSON at {path}: {type(value).__name__}")


def _jcs_encode(value) -> str:
    """Deterministic RFC 8785 JSON serialization using UTF-16 code-unit key sorting."""
    assert_no_floats(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_encode(x) for x in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_jcs_encode(v)}" for k, v in items) + "}"
    raise ShadowContractError(f"unsupported value type: {type(value).__name__}")


def build_shadow_hit_record(
    *,
    frame_idx: int,
    t_ammo_ms: int,
    model_id: str,
    model_sha256: str,
    ammo_gate_passed: bool,
    verdict: str,
    confidence_bps: int,
    ads_active: bool,
    fire_mode: str,
    triage_reason: str,
) -> dict:
    """Build and validate a canonical ``qor.shadow_hit.v0`` stream record (zero floats)."""
    if verdict not in VALID_MNV4_VERDICTS:
        raise ShadowContractError(f"invalid verdict {verdict!r}")
    if fire_mode not in VALID_FIRE_MODES:
        raise ShadowContractError(f"invalid fire_mode {fire_mode!r}")
    if isinstance(confidence_bps, bool) or not isinstance(confidence_bps, int) or not (0 <= confidence_bps <= 10000):
        raise ShadowContractError(f"confidence_bps must be integer 0..10000, got {confidence_bps!r}")
    if isinstance(frame_idx, bool) or not isinstance(frame_idx, int):
        raise ShadowContractError(f"frame_idx must be int, got {frame_idx!r}")
    if isinstance(t_ammo_ms, bool) or not isinstance(t_ammo_ms, int):
        raise ShadowContractError(f"t_ammo_ms must be int, got {t_ammo_ms!r}")
    if not isinstance(ammo_gate_passed, bool):
        raise ShadowContractError("ammo_gate_passed must be bool")
    if not isinstance(ads_active, bool):
        raise ShadowContractError("ads_active must be bool")
    if not isinstance(model_id, str) or not model_id:
        raise ShadowContractError("model_id must be non-empty str")
    if not isinstance(model_sha256, str) or len(model_sha256) != 64:
        raise ShadowContractError("model_sha256 must be 64-char hex string")
    int(model_sha256, 16)
    if not isinstance(triage_reason, str) or not triage_reason:
        raise ShadowContractError("triage_reason must be non-empty str")

    record = {
        "schema": SCHEMA,
        "info_only": True,
        "frame_idx": frame_idx,
        "t_ammo_ms": t_ammo_ms,
        "model_id": model_id,
        "model_sha256": model_sha256.lower(),
        "ammo_gate_passed": ammo_gate_passed,
        "verdict": verdict,
        "confidence_bps": confidence_bps,
        "combat_state": {
            "ads_active": ads_active,
            "fire_mode": fire_mode,
        },
        "triage_reason": triage_reason,
    }
    assert_no_floats(record)
    return record


def canonical_shadow_json(record: dict) -> str:
    """Validate and canonicalize ``qor.shadow_hit.v0`` under RFC 8785 without floats."""
    expected_top = {
        "schema",
        "info_only",
        "frame_idx",
        "t_ammo_ms",
        "model_id",
        "model_sha256",
        "ammo_gate_passed",
        "verdict",
        "confidence_bps",
        "combat_state",
        "triage_reason",
    }
    if not isinstance(record, dict) or set(record.keys()) != expected_top:
        raise ShadowContractError("record keys do not match qor.shadow_hit.v0 schema")
    if record.get("schema") != SCHEMA or record.get("info_only") is not True:
        raise ShadowContractError("record must set schema='qor.shadow_hit.v0' and info_only=True")
    cs = record.get("combat_state")
    if not isinstance(cs, dict) or set(cs.keys()) != {"ads_active", "fire_mode"}:
        raise ShadowContractError("combat_state keys must be {'ads_active', 'fire_mode'}")
    validated = build_shadow_hit_record(
        frame_idx=record["frame_idx"],
        t_ammo_ms=record["t_ammo_ms"],
        model_id=record["model_id"],
        model_sha256=record["model_sha256"],
        ammo_gate_passed=record["ammo_gate_passed"],
        verdict=record["verdict"],
        confidence_bps=record["confidence_bps"],
        ads_active=cs["ads_active"],
        fire_mode=cs["fire_mode"],
        triage_reason=record["triage_reason"],
    )
    return _jcs_encode(validated)


# ----------------------------------------------------------------- SQLite index
def init_shadow_db(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` and ensure the ``shadow_hit_events`` table and split index exist."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SHADOW_HIT_EVENTS_DDL)
    conn.commit()
    return conn


def upsert_shadow_hit_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert or update shadow hit rows while enforcing session-level dataset_split integrity."""
    if not rows:
        return 0
    seen_session_splits: dict[str, str] = {}
    for r in rows:
        split = validate_dataset_split(r.get("dataset_split"))
        sid = str(r["session_id"])
        if sid in seen_session_splits and seen_session_splits[sid] != split:
            raise HoldoutFirewallError(
                f"conflicting dataset_split in batch for session {sid!r}"
            )
        seen_session_splits[sid] = split

    for sid, split in seen_session_splits.items():
        existing = conn.execute(
            "SELECT DISTINCT dataset_split FROM shadow_hit_events WHERE session_id = ?",
            (sid,),
        ).fetchall()
        for row in existing:
            if row["dataset_split"] != split:
                raise HoldoutFirewallError(
                    f"session {sid!r} already tagged as {row['dataset_split']!r}; refusing {split!r}"
                )

    cols = SHADOW_HIT_EVENTS_COLUMNS
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "event_id")
    sql = (
        f"INSERT INTO shadow_hit_events ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(event_id) DO UPDATE SET {updates}"
    )
    values = [tuple(r.get(c) for c in cols) for r in rows]
    with conn:
        conn.executemany(sql, values)
    return len(rows)


# ----------------------------------------------------------------- T-0052 tensor builder
def extract_center_luma_crops(
    frames_bgr: list,
    crop_size: int = CROP_SIZE,
    dx: int = 0,
    dy: int = 0,
):
    """Extract native 1:1 ``crop_size x crop_size`` center crops -> BT.709 luma ``[0.0, 1.0]``."""
    import numpy as np

    if crop_size % 2 != 0:
        raise ShadowRunnerError("crop_size must be even for 4-quadrant folding")
    if len(frames_bgr) < 2:
        raise ShadowRunnerError("need at least 2 frames for temporal diff")

    h, w = frames_bgr[0].shape[:2]
    half = crop_size // 2
    cx = (w // 2) + dx
    cy = (h // 2) + dy
    y0, y1 = cy - half, cy + half
    x0, x1 = cx - half, cx + half
    if y0 < 0 or x0 < 0 or y1 > h or x1 > w:
        raise ShadowRunnerError(f"center crop [{x0}:{x1}, {y0}:{y1}] exceeds frame {(h, w)}")

    luma_list = []
    for frame in frames_bgr:
        crop = frame[y0:y1, x0:x1].astype(np.float32) / 255.0
        luma = 0.0722 * crop[..., 0] + 0.7152 * crop[..., 1] + 0.2126 * crop[..., 2]
        luma_list.append(luma)
    return np.stack(luma_list, axis=0).astype(np.float32)


def fold_4quadrant_sym_min(pos_diff, tol_px: int = TOL_PX):
    """Fold 4 quadrants around the center dot and take pixel-wise min across diagonals."""
    import cv2
    import numpy as np

    h, w = pos_diff.shape
    half_h, half_w = h // 2, w // 2
    if tol_px > 0:
        k = 2 * tol_px + 1
        kernel = np.ones((k, k), dtype=np.uint8)
        work = cv2.dilate(pos_diff, kernel)
    else:
        work = pos_diff

    q1 = work[:half_h, :half_w]
    q2 = work[:half_h, half_w:][:, ::-1]
    q3 = work[half_h:, :half_w][::-1, :]
    q4 = work[half_h:, half_w:][::-1, ::-1]
    q_min = np.minimum(np.minimum(q1, q2), np.minimum(q3, q4))

    sym_map = np.empty_like(pos_diff)
    sym_map[:half_h, :half_w] = q_min
    sym_map[:half_h, half_w:] = q_min[:, ::-1]
    sym_map[half_h:, :half_w] = q_min[::-1, :]
    sym_map[half_h:, half_w:] = q_min[::-1, ::-1]
    return sym_map


def build_mnv4_v2_tensor(
    frames_bgr: list,
    crop_size: int = CROP_SIZE,
    dx: int = 0,
    dy: int = 0,
    tol_px: int = TOL_PX,
):
    """Build ``(16, 96, 96)`` float32 interleaved ``[pos_diff_t, sym_min_t]`` from 9 frames."""
    import numpy as np

    if len(frames_bgr) != WINDOW_FRAMES:
        raise ShadowRunnerError(f"expected {WINDOW_FRAMES} frames [t_ammo..t_ammo+8], got {len(frames_bgr)}")
    luma = extract_center_luma_crops(frames_bgr, crop_size=crop_size, dx=dx, dy=dy)
    channels = []
    for t in range(1, len(luma)):
        pos_diff = np.maximum(0.0, luma[t] - luma[t - 1])
        sym_min = fold_4quadrant_sym_min(pos_diff, tol_px=tol_px)
        channels.append(pos_diff)
        channels.append(sym_min)
    return np.stack(channels, axis=0).astype(np.float32)


def build_tensor_and_luma_from_center_crops(
    crops_bgr: list,
    crop_size: int = CROP_SIZE,
    tol_px: int = TOL_PX,
):
    """Build ``(16, 96, 96)`` tensor + ``(9, 96, 96)`` luma directly from already-cropped 96x96 frames."""
    import numpy as np

    if len(crops_bgr) != WINDOW_FRAMES:
        raise ShadowRunnerError(f"expected {WINDOW_FRAMES} crops, got {len(crops_bgr)}")
    luma = extract_center_luma_crops(crops_bgr, crop_size=crop_size, dx=0, dy=0)
    channels = []
    for t in range(1, len(luma)):
        pos_diff = np.maximum(0.0, luma[t] - luma[t - 1])
        sym_min = fold_4quadrant_sym_min(pos_diff, tol_px=tol_px)
        channels.append(pos_diff)
        channels.append(sym_min)
    return np.stack(channels, axis=0).astype(np.float32), luma


def logits_to_confidence_bps(logits, temperature: float = DEFAULT_TEMPERATURE) -> int:
    """Convert raw 2-class ``[no_hit, gunfire_hit]`` logits to integer basis points ``0..10000``."""
    vals = [float(logits[0]), float(logits[1])]
    if any(math.isnan(v) or math.isinf(v) for v in vals):
        return 5000
    t = max(1e-6, float(temperature))
    z0, z1 = vals[0] / t, vals[1] / t
    m = max(z0, z1)
    e0 = math.exp(z0 - m)
    e1 = math.exp(z1 - m)
    prob_hit = e1 / (e0 + e1)
    return max(0, min(10000, int(round(prob_hit * 10000.0))))


def verdict_from_bps(
    confidence_bps: int,
    tau_low_bps: int = DEFAULT_TAU_LOW_BPS,
    tau_high_bps: int = DEFAULT_TAU_HIGH_BPS,
) -> str:
    if confidence_bps >= tau_high_bps:
        return "GUNFIRE_HIT"
    if confidence_bps <= tau_low_bps:
        return "NO_HIT"
    return "ABSTAIN"


def validate_calibration(
    temperature: float,
    tau_low_bps: int,
    tau_high_bps: int,
) -> tuple[float, int, int]:
    """Validate softmax temperature (> 0, finite) and integer decision gates (0 <= low < high <= 10000)."""
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ShadowRunnerError(f"invalid temperature {temperature!r}; expected positive finite number")
    temp_f = float(temperature)
    if not math.isfinite(temp_f) or temp_f <= 0.0:
        raise ShadowRunnerError(f"invalid temperature {temperature!r}; must be > 0 and finite")
    for name, val in (("tau_low_bps", tau_low_bps), ("tau_high_bps", tau_high_bps)):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ShadowRunnerError(f"invalid {name} {val!r}; expected integer basis points")
    if not (0 <= tau_low_bps < tau_high_bps <= 10000):
        raise ShadowRunnerError(
            f"invalid thresholds tau_low_bps={tau_low_bps}, tau_high_bps={tau_high_bps}; require 0 <= low < high <= 10000"
        )
    return temp_f, tau_low_bps, tau_high_bps


def _parse_gate_bps(data: dict, bps_key: str, float_key: str) -> int:
    if bps_key in data and data[bps_key] is not None:
        raw = data[bps_key]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ShadowRunnerError(f"metrics field {bps_key!r} must be an integer, got {raw!r}")
        return raw
    gate = data.get("calibrated_gate")
    if isinstance(gate, dict) and float_key in gate and gate[float_key] is not None:
        raw_f = gate[float_key]
        if isinstance(raw_f, bool) or not isinstance(raw_f, (int, float)):
            raise ShadowRunnerError(f"metrics field calibrated_gate.{float_key} must be numeric, got {raw_f!r}")
        val_f = float(raw_f)
        if not math.isfinite(val_f):
            raise ShadowRunnerError(f"metrics field calibrated_gate.{float_key} is non-finite: {raw_f!r}")
        return int(round(val_f * 10000.0))
    raise ShadowRunnerError(
        f"metrics JSON missing required threshold {bps_key!r} (or calibrated_gate.{float_key})"
    )


def load_model_calibration(
    metrics_path: Path,
    *,
    expected_model_sha256: str | None = None,
) -> dict:
    """Load and validate temperature, tau_low_bps, tau_high_bps, and optional onnx_sha256 from metrics JSON."""
    mp = Path(metrics_path)
    if not mp.is_file():
        raise ShadowRunnerError(f"metrics JSON file not found: {mp}")
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ShadowRunnerError(f"failed to parse metrics JSON {mp}: {exc}") from exc
    if not isinstance(data, dict):
        raise ShadowRunnerError(f"metrics JSON {mp} must be a JSON object")

    metrics_sha = data.get("onnx_sha256")
    if metrics_sha is not None:
        if not isinstance(metrics_sha, str) or len(metrics_sha.strip()) != 64:
            raise ShadowRunnerError(f"invalid onnx_sha256 in metrics JSON {mp}: {metrics_sha!r}")
        try:
            int(metrics_sha.strip(), 16)
        except ValueError as exc:
            raise ShadowRunnerError(f"non-hex onnx_sha256 in metrics JSON {mp}: {metrics_sha!r}") from exc
        norm_metrics_sha = metrics_sha.strip().lower()
        if expected_model_sha256 is not None and norm_metrics_sha != expected_model_sha256.strip().lower():
            raise ShadowRunnerError(
                f"metrics JSON {mp} onnx_sha256 ({norm_metrics_sha}) does not match model SHA-256 ({expected_model_sha256.lower()})"
            )
    else:
        if expected_model_sha256 is not None:
            raise ShadowRunnerError(
                f"metrics JSON {mp} missing required 'onnx_sha256' to bind against model SHA-256 ({expected_model_sha256.lower()})"
            )
        norm_metrics_sha = None

    if "temperature" not in data or data["temperature"] is None:
        raise ShadowRunnerError(f"metrics JSON {mp} missing required field 'temperature'")

    tau_low_bps = _parse_gate_bps(data, "tau_low_bps", "tau_low")
    tau_high_bps = _parse_gate_bps(data, "tau_high_bps", "tau_high")
    temp_f, tau_low_i, tau_high_i = validate_calibration(data["temperature"], tau_low_bps, tau_high_bps)

    raw_model_id = data.get("model_id")
    model_id = raw_model_id.strip() if isinstance(raw_model_id, str) and raw_model_id.strip() else None

    return {
        "model_id": model_id,
        "temperature": temp_f,
        "tau_low_bps": tau_low_i,
        "tau_high_bps": tau_high_i,
        "onnx_sha256": norm_metrics_sha,
        "metrics_path": str(mp),
    }


def resolve_calibration(
    *,
    metrics_path: Path | None = None,
    model_id: str | None = None,
    temperature: float | None = None,
    tau_low_bps: int | None = None,
    tau_high_bps: int | None = None,
    expected_model_sha256: str | None = None,
    environ=None,
) -> dict:
    """Resolve model calibration (explicit > env > metrics JSON > legacy v2 defaults for v2 SHA only)."""
    env = os.environ if environ is None else environ
    resolved_model_id = DEFAULT_MODEL_ID
    resolved_temp = DEFAULT_TEMPERATURE
    resolved_low = DEFAULT_TAU_LOW_BPS
    resolved_high = DEFAULT_TAU_HIGH_BPS
    provided_temp = False
    provided_low = False
    provided_high = False
    sources: list[str] = []
    resolved_metrics_path: str | None = None

    eff_metrics = metrics_path
    if eff_metrics is None:
        raw_env_metrics = env.get(ENV_METRICS)
        if isinstance(raw_env_metrics, str) and raw_env_metrics.strip():
            eff_metrics = Path(raw_env_metrics.strip())
    if eff_metrics is not None:
        loaded = load_model_calibration(eff_metrics, expected_model_sha256=expected_model_sha256)
        if loaded["model_id"]:
            resolved_model_id = loaded["model_id"]
        resolved_temp = loaded["temperature"]
        resolved_low = loaded["tau_low_bps"]
        resolved_high = loaded["tau_high_bps"]
        provided_temp = True
        provided_low = True
        provided_high = True
        resolved_metrics_path = loaded["metrics_path"]
        sources.append("metrics_json")

    env_used = False
    raw_env_mid = env.get(ENV_MODEL_ID)
    if isinstance(raw_env_mid, str) and raw_env_mid.strip():
        resolved_model_id = raw_env_mid.strip()
        env_used = True
    raw_env_temp = env.get(ENV_TEMPERATURE)
    if isinstance(raw_env_temp, str) and raw_env_temp.strip():
        try:
            resolved_temp = float(raw_env_temp.strip())
        except ValueError as exc:
            raise ShadowRunnerError(f"invalid {ENV_TEMPERATURE}={raw_env_temp!r}") from exc
        provided_temp = True
        env_used = True
    raw_env_low = env.get(ENV_TAU_LOW_BPS)
    if isinstance(raw_env_low, str) and raw_env_low.strip():
        try:
            resolved_low = int(raw_env_low.strip(), 10)
        except ValueError as exc:
            raise ShadowRunnerError(f"invalid {ENV_TAU_LOW_BPS}={raw_env_low!r}") from exc
        provided_low = True
        env_used = True
    raw_env_high = env.get(ENV_TAU_HIGH_BPS)
    if isinstance(raw_env_high, str) and raw_env_high.strip():
        try:
            resolved_high = int(raw_env_high.strip(), 10)
        except ValueError as exc:
            raise ShadowRunnerError(f"invalid {ENV_TAU_HIGH_BPS}={raw_env_high!r}") from exc
        provided_high = True
        env_used = True
    if env_used:
        sources.append("env")

    explicit_used = False
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ShadowRunnerError(f"invalid model_id {model_id!r}")
        resolved_model_id = model_id.strip()
        explicit_used = True
    if temperature is not None:
        resolved_temp = temperature
        provided_temp = True
        explicit_used = True
    if tau_low_bps is not None:
        resolved_low = tau_low_bps
        provided_low = True
        explicit_used = True
    if tau_high_bps is not None:
        resolved_high = tau_high_bps
        provided_high = True
        explicit_used = True
    if explicit_used:
        sources.append("explicit")

    # B1: fail closed on non-v2 ONNX models unless all 3 calibration params were supplied
    if expected_model_sha256 is not None:
        norm_sha = expected_model_sha256.strip().lower()
        if norm_sha != V2_LEGACY_MODEL_SHA256 and not (provided_temp and provided_low and provided_high):
            raise ShadowRunnerError(
                f"calibration required for non-v2 model sha {norm_sha}; "
                f"supply --metrics / {ENV_METRICS} or explicit temperature, tau_low_bps, and tau_high_bps"
            )

    temp_f, low_i, high_i = validate_calibration(resolved_temp, resolved_low, resolved_high)
    return {
        "model_id": resolved_model_id,
        "temperature": temp_f,
        "temperature_milli": int(round(temp_f * 1000.0)),
        "tau_low_bps": low_i,
        "tau_high_bps": high_i,
        "calibration_source": "+".join(sources) if sources else "default_v2",
        "metrics_path": resolved_metrics_path,
    }


def _make_onnx_infer_fn(model_path: Path, temperature: float = DEFAULT_TEMPERATURE):
    """Lazy-import onnxruntime only when real model weights are executed on CPU."""
    import onnxruntime as ort

    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name
    output_name = sess.get_outputs()[0].name

    def _run(tensor_chw) -> int:
        batch = tensor_chw[None, ...]
        logits = sess.run([output_name], {input_name: batch})[0][0]
        return logits_to_confidence_bps(logits, temperature=temperature)

    return _run


# ----------------------------------------------------------------- Clock & combat-state join
def _has_equipment_button(buttons) -> bool:
    if isinstance(buttons, int) and not isinstance(buttons, bool):
        return bool(buttons & (L1_BIT | R1_BIT))
    if isinstance(buttons, (list, tuple, set, frozenset)):
        upper = {str(b).strip().upper() for b in buttons}
        return "L1" in upper or "R1" in upper
    return False


def _hold_spans(samples: list[tuple[int, int]], last_t_us: int, threshold: int = TRIGGER_THRESHOLD):
    spans = []
    down = None
    for t_us, val in samples:
        held = val > threshold
        if held and down is None:
            down = t_us
        elif not held and down is not None:
            spans.append((down, t_us))
            down = None
    if down is not None:
        spans.append((down, max(last_t_us, down)))
    return spans


def parse_match_inputs(match_dir: Path, *, inputs_path: Path | None = None) -> dict:
    """Parse ``inputs.jsonl``, optional ``--inputs`` override, or ``hand_timeline.v0.jsonl`` for R2/L2 and L1/R1."""
    if inputs_path is not None:
        ip = Path(inputs_path)
        if not ip.is_file():
            raise ShadowRunnerError(f"inputs override file not found: {ip}")
    else:
        ip = match_dir / "inputs.jsonl"
        if not ip.is_file():
            hp = match_dir / "hand_timeline.v0.jsonl"
            if not hp.is_file():
                raise ShadowRunnerError(f"missing inputs.jsonl in {match_dir}")
            ip = hp

    r2_rows: list[tuple[int, int]] = []
    l2_rows: list[tuple[int, int]] = []
    equip_ts: list[int] = []
    last_t_us = 0
    with open(ip, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = r.get("t_us")
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                continue
            t_us = int(t)
            if t_us > last_t_us:
                last_t_us = t_us
            if r.get("type") not in ("in", "press", "buttons"):
                continue
            r2 = r.get("r2")
            l2 = r.get("l2")
            if r2 is not None and not isinstance(r2, bool):
                r2_rows.append((t_us, int(r2)))
            if l2 is not None and not isinstance(l2, bool):
                l2_rows.append((t_us, int(l2)))
            btns = r.get("buttons") if r.get("buttons") is not None else r.get("b")
            if _has_equipment_button(btns) or int(r.get("l1") or 0) > 0 or int(r.get("r1") or 0) > 0:
                equip_ts.append(t_us)

    r2_spans = _hold_spans(r2_rows, last_t_us)
    l2_spans = _hold_spans(l2_rows, last_t_us)
    equip_ts.sort()

    enriched = []
    for a, b in r2_spans:
        ads = any(min(b, lb) > max(a, la) for la, lb in l2_spans)
        dur_us = b - a
        fire_mode = "SINGLE_TAP" if dur_us <= SINGLE_TAP_MAX_US else "FULL_AUTO_SPRAY"
        lo = bisect.bisect_left(equip_ts, a - EQUIPMENT_PRE_US)
        eq = lo < len(equip_ts) and equip_ts[lo] <= (b + EQUIPMENT_POST_US)
        enriched.append({
            "start_us": a,
            "end_us": b,
            "ads_active": bool(ads),
            "fire_mode": fire_mode,
            "equipment_excluded": bool(eq),
        })
    return {"r2_spans": enriched, "equip_ts": equip_ts, "last_t_us": last_t_us}


def parse_match_frame_clock(match_dir: Path, file_name: str = FRAME_FILE) -> list[tuple[int, float]]:
    """Return sorted list of ``(frame_idx, t_us)`` matching ``qorcap_engagement`` timing."""
    fc_path = match_dir / "frame_clock.jsonl"
    if not fc_path.is_file():
        raise ShadowRunnerError(f"missing frame_clock.jsonl in {match_dir}")
    raw_frames = []
    with open(fc_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("type") != "frame":
                continue
            if r.get("file", FRAME_FILE) != file_name:
                continue
            try:
                raw_frames.append((int(r["frame"]), float(r["t_mono_us"]), float(r["pts_90k"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not raw_frames:
        raise ShadowRunnerError(f"no {file_name} entries in frame_clock.jsonl")
    env = min(t - p * 100.0 / 9.0 for _, t, p in raw_frames)
    mapped = [(idx, p * 100.0 / 9.0 + env) for idx, _, p in raw_frames]
    mapped.sort(key=lambda pair: pair[0])
    return mapped


# ----------------------------------------------------------------- Active-learning triage
def assign_triage_reason(
    event: dict,
    *,
    tau_low_bps: int = DEFAULT_TAU_LOW_BPS,
    tau_high_bps: int = DEFAULT_TAU_HIGH_BPS,
    boundary_margin_bps: int = BOUNDARY_MARGIN_BPS,
) -> str:
    """Assign exact T-0055 Section 4 triage_reason to one shadow event."""
    if event.get("equipment_window_excluded"):
        return "EQUIPMENT_EXCLUDED"

    r199 = event.get("reader_199_verdict", "NONE")
    mnv4 = event.get("mnv4_verdict", "ABSTAIN")
    bps = int(event.get("confidence_bps", 5000))
    xp = bool(event.get("xp_popup_corroborated"))
    kf = bool(event.get("killfeed_corroborated"))
    audio = event.get("audio_peak_ms") is not None
    corroborated = xp or kf or audio
    ads_active = int(event.get("ads_active", 0))
    fire_mode = event.get("fire_mode", "UNKNOWN")

    # P1: #199 == NONE, no XP/killfeed/audio peak, and MNv4 == GUNFIRE_HIT
    if r199 == "NONE" and not corroborated and mnv4 == "GUNFIRE_HIT":
        return "DISAGREEMENT_FP"

    # P2: #199 == ABSTAIN and MNv4 IN ('GUNFIRE_HIT', 'NO_HIT')
    if r199 == "ABSTAIN" and mnv4 in ("GUNFIRE_HIT", "NO_HIT"):
        return "ABSTAIN_FILL"

    # P3: confidence_bps falls within +-1000 bps of tau_low or tau_high
    if abs(bps - tau_low_bps) <= boundary_margin_bps or abs(bps - tau_high_bps) <= boundary_margin_bps:
        return "UNCERTAIN_BOUNDARY"

    # P4: fire_mode == 'SINGLE_TAP' OR ads_active == 0 (HIPFIRE)
    if fire_mode == "SINGLE_TAP" or ads_active == 0:
        return "QUOTA_BALANCE"

    # All remaining unanimous / consensus windows stay out of manual review queue
    return "CONSENSUS_AUTO"


def select_review_queue(
    events: list[dict],
    *,
    min_items: int = REVIEW_MIN_ITEMS,
    max_items: int = REVIEW_MAX_ITEMS,
    tau_low_bps: int = DEFAULT_TAU_LOW_BPS,
    tau_high_bps: int = DEFAULT_TAU_HIGH_BPS,
) -> list[dict]:
    """Rank high-value P1..P4 events and cap at ``max_items`` (15-25 per match)."""
    del min_items  # target floor when fewer than max_items candidates exist
    candidates = []
    for ev in events:
        if ev.get("equipment_window_excluded"):
            continue
        reason = ev.get("triage_reason") or assign_triage_reason(
            ev, tau_low_bps=tau_low_bps, tau_high_bps=tau_high_bps
        )
        if reason in ("CONSENSUS_AUTO", "EQUIPMENT_EXCLUDED"):
            continue
        prio = TRIAGE_PRIORITY.get(reason, 99)
        if prio >= 99:
            continue
        bps = int(ev.get("confidence_bps", 5000))
        if reason == "DISAGREEMENT_FP":
            ads_spray = 0 if (int(ev.get("ads_active", 0)) == 1 and ev.get("fire_mode") == "FULL_AUTO_SPRAY") else 1
            sub_rank = (ads_spray, -bps)
        elif reason == "ABSTAIN_FILL":
            sub_rank = (0, -abs(bps - 5000))
        elif reason == "UNCERTAIN_BOUNDARY":
            dist = min(abs(bps - tau_low_bps), abs(bps - tau_high_bps))
            sub_rank = (0, dist)
        else:
            # P4 QUOTA_BALANCE: surface scarce tap and hipfire cases first
            tap_first = 0 if ev.get("fire_mode") == "SINGLE_TAP" else 1
            sub_rank = (tap_first, -abs(bps - 5000))
        candidates.append((prio, sub_rank, int(ev.get("frame_idx", 0)), ev))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in candidates[:max_items]]


# ----------------------------------------------------------------- Visual strip & 2-min review pack
def render_composite_strip(
    frames_bgr_or_crops: list,
    tensor_16x96x96,
    luma_9x96x96,
    out_png: Path,
    *,
    header_text: str = "",
) -> Path:
    """Render 3-row x 8-column strip (Row 1 Luma, Row 2 pos_diff, Row 3 sym_min) using cv2 + numpy."""
    import cv2
    import numpy as np

    del frames_bgr_or_crops  # tensor + luma carry exact 96x96 crops
    steps = 8
    cell = CROP_SIZE
    pad = 4
    header_h = 28 if header_text else 0
    label_w = 88
    width = label_w + steps * (cell + pad) + pad
    height = header_h + 3 * (cell + pad) + pad
    canvas = np.full((height, width, 3), 24, dtype=np.uint8)

    if header_text:
        cv2.putText(
            canvas,
            header_text[:110],
            (8, 19),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (235, 235, 235),
            1,
            cv2.LINE_AA,
        )

    row_labels = ("Luma", "pos_diff", "sym_min")
    for r_idx, r_name in enumerate(row_labels):
        y = header_h + pad + r_idx * (cell + pad)
        cv2.putText(
            canvas,
            r_name,
            (8, y + cell // 2 + 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (210, 210, 210),
            1,
            cv2.LINE_AA,
        )
        for s in range(steps):
            x = label_w + pad + s * (cell + pad)
            if r_idx == 0:
                gray = np.clip(luma_9x96x96[s + 1] * 255.0, 0, 255).astype(np.uint8)
                tile = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                cv2.putText(
                    tile,
                    f"t+{s + 1}",
                    (4, 13),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.36,
                    (255, 255, 0),
                    1,
                    cv2.LINE_AA,
                )
            else:
                ch = tensor_16x96x96[2 * s + (0 if r_idx == 1 else 1)]
                scaled = np.clip((ch / 0.4) * 255.0, 0, 255).astype(np.uint8)
                tile = cv2.applyColorMap(scaled, cv2.COLORMAP_INFERNO)
            canvas[y : y + cell, x : x + cell] = tile

    out_png.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", canvas)
    if not ok:
        raise ShadowRunnerError(f"failed to encode composite PNG strip for {out_png}")
    _atomic_write_bytes(out_png, buf.tobytes())
    return out_png


def _try_extract_context_clip(video_path: Path, t_ammo_ms: int, out_mp4: Path) -> str | None:
    """Extract a 1-second context clip around ``t_ammo_ms`` if ffmpeg is available."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_path.is_file():
        return None
    start_s = max(0.0, (t_ammo_ms - 500) / 1000.0)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(video_path),
        "-t",
        "1.000",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-an",
        str(out_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode == 0 and out_mp4.is_file() and out_mp4.stat().st_size > 0:
        return out_mp4.name
    return None


def _render_review_html(session_id: str, manifest: dict) -> str:
    items_json = json.dumps(manifest["items"], ensure_ascii=False, indent=2)
    title = html.escape(f"QorCap Practice Shadow Review - {session_id}")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #111317; color: #e8ecf2; margin: 20px; }}
  .bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding: 12px 16px; background: #1a1e26; border-radius: 8px; }}
  .card {{ background: #1a1e26; border: 1px solid #2b3240; border-radius: 8px; padding: 14px; margin-bottom: 14px; }}
  .card.active {{ border-color: #60a5fa; }}
  .meta {{ font-size: 13px; color: #b4bfd0; margin-bottom: 8px; display: flex; gap: 14px; flex-wrap: wrap; }}
  .pill {{ background: #262e3d; padding: 2px 8px; border-radius: 999px; font-weight: 600; }}
  .btns button {{ margin-right: 8px; padding: 6px 12px; border: 0; border-radius: 6px; cursor: pointer; font-weight: 600; }}
  .btn-h {{ background: #16a34a; color: #fff; }}
  .btn-n {{ background: #dc2626; color: #fff; }}
  .btn-s {{ background: #4b5563; color: #fff; }}
  img.strip {{ max-width: 100%; border-radius: 4px; display: block; margin: 8px 0; }}
</style>
</head>
<body>
<div class="bar">
  <div><strong>{title}</strong> &middot; Split: <code>{html.escape(manifest['dataset_split'])}</code> &middot; Info only</div>
  <div>Hotkeys: <strong>H</strong>=GUNFIRE_HIT &middot; <strong>N</strong>=NO_HIT &middot; <strong>S</strong>=SKIP</div>
</div>
<div id="list"></div>
<script>
const ITEMS = {items_json};
let idx = 0;
async function sendLabel(eventId, key) {{
  try {{
    await fetch('/label', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{event_id: eventId, key}})
    }});
  }} catch (_) {{}}
  const item = ITEMS.find(x => x.event_id === eventId);
  if (item) item.human_label = key === 'H' ? 'GUNFIRE_HIT' : (key === 'N' ? 'NO_HIT' : 'SKIP');
  if (idx + 1 < ITEMS.length) idx += 1;
  render();
}}
function render() {{
  const root = document.getElementById('list');
  root.innerHTML = '';
  ITEMS.forEach((it, i) => {{
    const div = document.createElement('div');
    div.className = 'card' + (i === idx ? ' active' : '');
    const lbl = it.human_label || 'UNLABELED';
    div.innerHTML = `
      <div class="meta">
        <span class="pill">${{it.triage_reason}}</span>
        <span>id=${{it.event_id}}</span>
        <span>frame=${{it.frame_idx}}</span>
        <span>t_ammo=${{it.t_ammo_ms}}ms</span>
        <span>MNv4=${{it.mnv4_verdict}} (${{it.confidence_bps}} bps)</span>
        <span>#199=${{it.reader_199_verdict}}</span>
        <span>${{it.ads_active ? 'ADS' : 'HIPFIRE'}} / ${{it.fire_mode}}</span>
        <span>label=<strong>${{lbl}}</strong></span>
      </div>
      <img class="strip" src="${{it.strip_png}}" alt="strip">
      <div class="btns">
        <button class="btn-h" onclick="sendLabel('${{it.event_id}}','H')">[H] GUNFIRE_HIT</button>
        <button class="btn-n" onclick="sendLabel('${{it.event_id}}','N')">[N] NO_HIT</button>
        <button class="btn-s" onclick="sendLabel('${{it.event_id}}','S')">[S] SKIP</button>
      </div>`;
    root.appendChild(div);
  }});
}}
document.addEventListener('keydown', (e) => {{
  const k = e.key.toUpperCase();
  if (!['H','N','S'].includes(k) || !ITEMS[idx]) return;
  sendLabel(ITEMS[idx].event_id, k);
}});
render();
</script>
</body>
</html>
"""


def write_human_label(
    db_path: Path,
    event_id: str,
    key_or_label: str,
    *,
    manifest_path: Path | None = None,
    labeled_at_utc: str | None = None,
) -> str:
    """Map single keystroke ``H``/``N``/``S`` (or full label) and persist into SQLite."""
    norm = str(key_or_label).strip().upper()
    if norm not in KEY_TO_HUMAN_LABEL:
        raise ValueError(f"invalid confirmation input {key_or_label!r}; expected H, N, or S")
    label = KEY_TO_HUMAN_LABEL[norm]
    ts = labeled_at_utc or _utc_now_iso()
    conn = init_shadow_db(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE shadow_hit_events SET human_label = ?, human_labeled_at_utc = ? WHERE event_id = ?",
                (label, ts, event_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"event_id not found in shadow_hit_events: {event_id!r}")
    finally:
        conn.close()

    if manifest_path is not None and Path(manifest_path).is_file():
        mp = Path(manifest_path)
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        for item in manifest.get("items", []):
            if item.get("event_id") == event_id:
                item["human_label"] = label
                item["human_labeled_at_utc"] = ts
        _atomic_write_text(mp, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return label


def serve_review_pack(review_dir: Path, db_path: Path, *, port: int = 0) -> http.server.ThreadingHTTPServer:
    """Create loopback HTTP server (127.0.0.1 only) backing ``review.html`` single-keystroke POSTs."""
    review_dir = Path(review_dir).resolve()
    manifest_path = review_dir / "manifest.json"

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(review_dir), **kwargs)

        def do_POST(self):
            if self.path != "/label":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            label = write_human_label(
                db_path,
                body["event_id"],
                body["key"],
                manifest_path=manifest_path,
            )
            resp = json.dumps({"ok": True, "human_label": label}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format, *args):
            return

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)


# ----------------------------------------------------------------- Match pipeline
def _load_match_killfeed_tag(match_dir: Path) -> str | None:
    meta = match_dir / "match.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
            tag = data.get("gamertag") or data.get("player_tag")
            if isinstance(tag, str) and tag.strip():
                return tag.strip()
        except Exception:
            pass
    return None


def _summarize_199_window(frame_states: list[str]) -> str:
    if "KILL" in frame_states:
        return "KILL"
    if "HIT" in frame_states:
        return "HIT"
    if frame_states and all(s == "ABSTAIN" for s in frame_states):
        return "ABSTAIN"
    if "NONE" in frame_states or "ADS_OR_FIRING" in frame_states:
        return "NONE"
    return "ABSTAIN"


def run_shadow_on_match(
    match_dir: Path,
    *,
    dataset_split: str,
    model_path: Path | None = None,
    metrics_path: Path | None = None,
    model_id: str | None = None,
    out_root: Path | None = None,
    db_path: Path | None = None,
    inputs_path: Path | None = None,
    infer_fn=None,
    temperature: float | None = None,
    tau_low_bps: int | None = None,
    tau_high_bps: int | None = None,
    corroboration_window_ms: int = CORROBORATION_WINDOW_MS,
    corrob_sample_interval_ms: int = CORROBORATION_SAMPLE_INTERVAL_MS,
    extract_clips: bool = True,
    environ=None,
) -> dict:
    """Execute post-match shadow inference, SQL sync, and review pack generation strictly outside ``match_dir``."""
    import cv2

    from qorcap_cod_killfeed import SELF, read_killfeed_lines
    from qorcap_cod_post_nov21 import CenterBoxTrigger, read_ammo_counter, read_kill_xp_popup

    match_dir = Path(match_dir)
    session_id = require_safe_leaf(match_dir.name)
    split = validate_dataset_split(dataset_split)

    resolved_out = Path(out_root) if out_root is not None else default_out_root(environ)
    resolved_db = Path(db_path) if db_path is not None else default_db_path(environ)
    session_out = resolved_out / session_id
    review_dir = resolved_out / REVIEW_ROOT_NAME / session_id
    _ensure_out_outside_match(session_out, match_dir)
    _ensure_out_outside_match(review_dir, match_dir)
    _ensure_out_outside_match(resolved_db, match_dir)

    real_model_sha: str | None = (
        _sha256_file(Path(model_path))
        if (model_path is not None and Path(model_path).is_file())
        else None
    )
    if infer_fn is None and real_model_sha is None:
        raise ShadowRunnerError(f"shadow model file missing: {model_path!r}")

    has_metrics_cfg = metrics_path is not None or bool(
        environ is not None and isinstance(environ.get(ENV_METRICS), str) and environ.get(ENV_METRICS).strip()
    )
    calibration = resolve_calibration(
        metrics_path=metrics_path,
        model_id=model_id,
        temperature=temperature,
        tau_low_bps=tau_low_bps,
        tau_high_bps=tau_high_bps,
        expected_model_sha256=real_model_sha if (infer_fn is None or has_metrics_cfg) else None,
        environ=environ,
    )
    model_id = calibration["model_id"]
    temperature = calibration["temperature"]
    tau_low_bps = calibration["tau_low_bps"]
    tau_high_bps = calibration["tau_high_bps"]

    if infer_fn is None:
        model_sha256 = real_model_sha
        runner = _make_onnx_infer_fn(Path(model_path), temperature=temperature)
    else:
        if model_path is not None and Path(model_path).is_file():
            model_sha256 = _sha256_file(Path(model_path))
        else:
            model_sha256 = hashlib.sha256(str(model_id).encode("utf-8")).hexdigest()
        runner = infer_fn

    inputs_info = parse_match_inputs(match_dir, inputs_path=inputs_path)
    frame_pairs = parse_match_frame_clock(match_dir)
    video_path = match_dir / FRAME_FILE
    if not video_path.is_file():
        raise ShadowRunnerError(f"missing {FRAME_FILE} in {match_dir}")

    # Map each frame_idx -> matching R2 span (or None)
    r2_spans = inputs_info["r2_spans"]
    frame_to_span: dict[int, dict] = {}
    span_idx = 0
    for fidx, t_us in frame_pairs:
        while span_idx < len(r2_spans) and t_us > r2_spans[span_idx]["end_us"]:
            span_idx += 1
        if span_idx < len(r2_spans):
            sp = r2_spans[span_idx]
            if sp["start_us"] <= t_us <= sp["end_us"]:
                frame_to_span[fidx] = sp

    t_us_by_frame = {fidx: int(round(t_us)) for fidx, t_us in frame_pairs}
    t0_us = int(round(frame_pairs[0][1])) if frame_pairs else 0
    killfeed_tag = _load_match_killfeed_tag(match_dir)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ShadowRunnerError(f"cannot open video {video_path}")

    cbt = CenterBoxTrigger()
    events: list[dict] = []
    window_assets: dict[str, tuple] = {}
    jsonl_records: list[tuple[int, str]] = []

    pending: list[dict] = []  # collecting 9-frame crops [t_ammo..t_ammo+8]
    open_corrob: list[dict] = []  # awaiting XP/killfeed corroboration until t_ammo_ms + corroboration_window_ms
    ring_crops: collections.deque = collections.deque(maxlen=WINDOW_FRAMES)
    prev_mag: int | None = None
    last_span_anchor: int = -1
    last_corrob_sample_ms: int | None = None
    created_at = _utc_now_iso()

    def _run_window_onnx(p: dict) -> dict:
        tensor, luma = build_tensor_and_luma_from_center_crops(p["crops"])
        bps = int(runner(tensor))
        mnv4_v = verdict_from_bps(bps, tau_low_bps=tau_low_bps, tau_high_bps=tau_high_bps)
        r199_v = _summarize_199_window(p["states_199"])
        eid = f"{session_id}_f{p['frame_idx']:06d}"
        window_assets[eid] = (tensor, luma)
        # Drop crop buffers immediately once inference + asset creation finish
        p["crops"].clear()
        return {
            "event_id": eid,
            "session_id": session_id,
            "dataset_split": split,
            "frame_idx": p["frame_idx"],
            "t_ammo_ms": p["t_ammo_ms"],
            "corrob_until_ms": p["t_ammo_ms"] + int(corroboration_window_ms),
            "model_id": model_id,
            "model_sha256": model_sha256,
            "ads_active": 1 if p["ads_active"] else 0,
            "fire_mode": p["fire_mode"],
            "equipment_window_excluded": 0,
            "ammo_gate_passed": 1,
            "reader_199_verdict": r199_v,
            "mnv4_verdict": mnv4_v,
            "confidence_bps": bps,
            "xp_popup_corroborated": 1 if p["xp_corrob"] else 0,
            "killfeed_corroborated": 1 if p["kf_corrob"] else 0,
            "audio_peak_ms": None,
            "triage_reason": "",
            "human_label": None,
            "human_labeled_at_utc": None,
            "created_at_utc": created_at,
        }

    def _finalize_corrob_event(ev: dict) -> None:
        ev.pop("corrob_until_ms", None)
        ev["triage_reason"] = assign_triage_reason(
            ev, tau_low_bps=tau_low_bps, tau_high_bps=tau_high_bps
        )
        events.append(ev)
        rec = build_shadow_hit_record(
            frame_idx=ev["frame_idx"],
            t_ammo_ms=ev["t_ammo_ms"],
            model_id=ev["model_id"],
            model_sha256=ev["model_sha256"],
            ammo_gate_passed=True,
            verdict=ev["mnv4_verdict"],
            confidence_bps=ev["confidence_bps"],
            ads_active=bool(ev["ads_active"]),
            fire_mode=ev["fire_mode"],
            triage_reason=ev["triage_reason"],
        )
        jsonl_records.append((ev["frame_idx"], canonical_shadow_json(rec)))

    try:
        frame_idx = 0
        while True:
            grabbed = cap.grab()
            if not grabbed:
                break
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                frame_idx += 1
                continue

            h, w = frame.shape[:2]
            half = CROP_SIZE // 2
            cy, cx = h // 2, w // 2
            if h >= CROP_SIZE and w >= CROP_SIZE:
                crop = frame[cy - half : cy + half, cx - half : cx + half].copy()
            else:
                crop = cv2.resize(frame, (CROP_SIZE, CROP_SIZE))
            ring_crops.append(crop)

            t_us = t_us_by_frame.get(frame_idx, t0_us + frame_idx * 16667)
            t_ammo_ms = max(0, (t_us - t0_us) // 1000)

            span = frame_to_span.get(frame_idx)
            cbt_res = cbt.step(frame, t_us=t_us)
            state_199 = cbt_res.get("state", "ABSTAIN")

            # B3 + B2: Decoupled streaming XP + killfeed corroboration (~10 Hz when windows are open;
            # read_killfeed_lines falls back to OWNER_TAG ("Qortrola30") when match.json is absent).
            xp_hit = False
            kf_hit = False
            due_for_sample = (
                last_corrob_sample_ms is None
                or (t_ammo_ms - last_corrob_sample_ms) >= corrob_sample_interval_ms
            )
            if (pending or open_corrob) and due_for_sample:
                last_corrob_sample_ms = t_ammo_ms
                xp_res = read_kill_xp_popup(frame)
                xp_hit = xp_res.get("state") == "XP_POPUP"
                try:
                    kf_res = read_killfeed_lines(frame, tag=killfeed_tag)
                    kf_hit = any(ln.get("killer") == SELF for ln in kf_res.get("lines") or [])
                except Exception:
                    kf_hit = False

            still_open: list[dict] = []
            for oc in open_corrob:
                if t_ammo_ms <= oc["corrob_until_ms"]:
                    if xp_hit:
                        oc["xp_popup_corroborated"] = 1
                    if kf_hit:
                        oc["killfeed_corroborated"] = 1
                    still_open.append(oc)
                else:
                    _finalize_corrob_event(oc)
            open_corrob = still_open

            still_pending = []
            for p in pending:
                p["crops"].append(crop)
                p["states_199"].append(state_199)
                p["xp_corrob"] = p["xp_corrob"] or xp_hit
                p["kf_corrob"] = p["kf_corrob"] or kf_hit
                if len(p["crops"]) == WINDOW_FRAMES:
                    open_corrob.append(_run_window_onnx(p))
                else:
                    still_pending.append(p)
            pending = still_pending

            # B1: External deterministic multi-round ammo decrement gate (1..5 rounds per frame step)
            ammo_res = read_ammo_counter(frame)
            mag = ammo_res.get("mag") if ammo_res.get("ok") else None
            if mag is not None and prev_mag is not None and 0 < (prev_mag - mag) <= MAX_AMMO_DROP_PER_STEP:
                if span is not None and frame_idx != last_span_anchor:
                    last_span_anchor = frame_idx
                    if span["equipment_excluded"]:
                        # Explicit L1/R1 exclusion fence: log exclusion, skip ONNX inference
                        ev = {
                            "event_id": f"{session_id}_f{frame_idx:06d}",
                            "session_id": session_id,
                            "dataset_split": split,
                            "frame_idx": frame_idx,
                            "t_ammo_ms": t_ammo_ms,
                            "model_id": model_id,
                            "model_sha256": model_sha256,
                            "ads_active": 1 if span["ads_active"] else 0,
                            "fire_mode": span["fire_mode"],
                            "equipment_window_excluded": 1,
                            "ammo_gate_passed": 1,
                            "reader_199_verdict": state_199,
                            "mnv4_verdict": "ABSTAIN",
                            "confidence_bps": 0,
                            "xp_popup_corroborated": 1 if xp_hit else 0,
                            "killfeed_corroborated": 1 if kf_hit else 0,
                            "audio_peak_ms": None,
                            "triage_reason": "EQUIPMENT_EXCLUDED",
                            "human_label": None,
                            "human_labeled_at_utc": None,
                            "created_at_utc": created_at,
                        }
                        events.append(ev)
                    else:
                        pending.append({
                            "frame_idx": frame_idx,
                            "t_ammo_ms": t_ammo_ms,
                            "ads_active": span["ads_active"],
                            "fire_mode": span["fire_mode"],
                            "crops": [crop],
                            "states_199": [state_199],
                            "xp_corrob": xp_hit,
                            "kf_corrob": kf_hit,
                        })
            if mag is not None:
                prev_mag = mag

            del frame
            frame_idx += 1

        # Flush any events whose corroboration window extends past EOF
        for oc in open_corrob:
            _finalize_corrob_event(oc)
        open_corrob.clear()
        events.sort(key=lambda e: int(e["frame_idx"]))
        jsonl_records.sort(key=lambda pair: pair[0])
    finally:
        cap.release()

    conn = init_shadow_db(resolved_db)
    try:
        upsert_shadow_hit_events(conn, events)
    finally:
        conn.close()

    session_out.mkdir(parents=True, exist_ok=True)
    jsonl_path = session_out / SHADOW_JSONL_NAME
    _atomic_write_text(jsonl_path, "".join(line + "\n" for _, line in jsonl_records))

    queue = select_review_queue(
        events,
        tau_low_bps=tau_low_bps,
        tau_high_bps=tau_high_bps,
    )
    strips_dir = review_dir / "strips"
    clips_dir = review_dir / "clips"
    strips_dir.mkdir(parents=True, exist_ok=True)

    manifest_items = []
    for ev in queue:
        eid = ev["event_id"]
        tensor, luma = window_assets[eid]
        strip_path = strips_dir / f"{eid}.png"
        header = (
            f"{eid} | {ev['triage_reason']} | {ev['mnv4_verdict']} "
            f"{ev['confidence_bps']}bps | #199={ev['reader_199_verdict']} | "
            f"{'ADS' if ev['ads_active'] else 'HIP'} {ev['fire_mode']}"
        )
        render_composite_strip([], tensor, luma, strip_path, header_text=header)
        clip_name = None
        if extract_clips:
            clip_name = _try_extract_context_clip(
                video_path, ev["t_ammo_ms"], clips_dir / f"{eid}.mp4"
            )
        manifest_items.append({
            "event_id": eid,
            "session_id": session_id,
            "dataset_split": split,
            "frame_idx": ev["frame_idx"],
            "t_ammo_ms": ev["t_ammo_ms"],
            "triage_reason": ev["triage_reason"],
            "triage_priority": TRIAGE_PRIORITY.get(ev["triage_reason"], 99),
            "mnv4_verdict": ev["mnv4_verdict"],
            "confidence_bps": ev["confidence_bps"],
            "reader_199_verdict": ev["reader_199_verdict"],
            "ads_active": ev["ads_active"],
            "fire_mode": ev["fire_mode"],
            "strip_png": f"strips/{eid}.png",
            "clip_mp4": f"clips/{clip_name}" if clip_name else None,
            "human_label": ev["human_label"],
            "human_labeled_at_utc": ev["human_labeled_at_utc"],
        })

    manifest = {
        "schema": "qor.shadow_review_pack.v0",
        "info_only": True,
        "session_id": session_id,
        "dataset_split": split,
        "model_id": model_id,
        "model_sha256": model_sha256,
        "temperature_milli": calibration["temperature_milli"],
        "tau_low_bps": tau_low_bps,
        "tau_high_bps": tau_high_bps,
        "calibration_source": calibration["calibration_source"],
        "metrics_path": calibration["metrics_path"],
        "total_events_logged": len(events),
        "review_queue_count": len(manifest_items),
        "items": manifest_items,
    }
    _atomic_write_text(review_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    _atomic_write_text(review_dir / "review.html", _render_review_html(session_id, manifest))

    return {
        "session_id": session_id,
        "dataset_split": split,
        "model_id": model_id,
        "temperature": temperature,
        "temperature_milli": calibration["temperature_milli"],
        "tau_low_bps": tau_low_bps,
        "tau_high_bps": tau_high_bps,
        "calibration_source": calibration["calibration_source"],
        "db_path": str(resolved_db),
        "shadow_jsonl": str(jsonl_path),
        "review_dir": str(review_dir),
        "events_count": len(events),
        "review_queue_count": len(manifest_items),
    }


# ----------------------------------------------------------------- Colab train_pool firewall export
def validate_colab_export_rows(rows: list[dict]) -> list[dict]:
    """Fail closed if any row violates ``dataset_split == 'train_pool'`` or lacks a human label."""
    validated = []
    for row in rows:
        split = row.get("dataset_split")
        label = row.get("human_label")
        if split != "train_pool":
            raise HoldoutFirewallError(
                f"FIREWALL VIOLATION: refusing export of dataset_split={split!r} row {row.get('event_id')!r}"
            )
        if not isinstance(label, str) or not label.strip() or label == "SKIP":
            raise HoldoutFirewallError(
                f"FIREWALL VIOLATION: refusing unlabeled/SKIP row {row.get('event_id')!r} (human_label={label!r})"
            )
        if label not in ("GUNFIRE_HIT", "NO_HIT"):
            raise HoldoutFirewallError(f"invalid label in export row {row.get('event_id')!r}: {label!r}")
        validated.append(dict(row))
    return validated


def export_train_pool_for_colab(
    db_path: Path,
    out_dir: Path,
    *,
    requested_split: str = "train_pool",
    session_ids: list[str] | None = None,
    max_chunk_bytes: int = MAX_COLAB_CHUNK_BYTES,
) -> dict:
    """Export strictly ``dataset_split = 'train_pool' AND human_label IS NOT NULL`` in <35 MB chunks."""
    if requested_split != "train_pool":
        raise HoldoutFirewallError(
            f"Colab export permits only dataset_split='train_pool', got {requested_split!r}"
        )
    out_dir = Path(out_dir)
    conn = init_shadow_db(db_path)
    try:
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            split_rows = conn.execute(
                f"SELECT DISTINCT session_id, dataset_split FROM shadow_hit_events WHERE session_id IN ({placeholders})",
                tuple(session_ids),
            ).fetchall()
            for sr in split_rows:
                if sr["dataset_split"] != "train_pool":
                    raise HoldoutFirewallError(
                        f"session {sr['session_id']!r} belongs to {sr['dataset_split']!r}; refused"
                    )
            sql = (
                "SELECT * FROM shadow_hit_events "
                f"WHERE dataset_split = 'train_pool' AND human_label IN ('GUNFIRE_HIT', 'NO_HIT') "
                f"AND session_id IN ({placeholders}) ORDER BY session_id, frame_idx"
            )
            raw_rows = [dict(r) for r in conn.execute(sql, tuple(session_ids)).fetchall()]
        else:
            raw_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM shadow_hit_events "
                    "WHERE dataset_split = 'train_pool' AND human_label IN ('GUNFIRE_HIT', 'NO_HIT') "
                    "ORDER BY session_id, frame_idx"
                ).fetchall()
            ]
    finally:
        conn.close()

    rows = validate_colab_export_rows(raw_rows)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Chunk JSONL payloads cleanly below max_chunk_bytes (< 50 MB Colab limit)
    chunks: list[dict] = []
    cur_lines: list[bytes] = []
    cur_bytes = 0
    part = 0

    def _flush_chunk(lines: list[bytes], idx: int) -> dict:
        tar_path = out_dir / f"train_pool_part_{idx:03d}.tar.gz"
        payload = b"".join(lines)
        import io

        with tarfile.open(tar_path, "w:gz") as tf:
            info = tarfile.TarInfo(name="shadow_train_pool.jsonl")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        return {
            "file": tar_path.name,
            "rows": len(lines),
            "bytes": tar_path.stat().st_size,
            "sha256": _sha256_file(tar_path),
        }

    for r in rows:
        encoded = (json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if cur_lines and cur_bytes + len(encoded) > max_chunk_bytes:
            chunks.append(_flush_chunk(cur_lines, part))
            part += 1
            cur_lines = []
            cur_bytes = 0
        cur_lines.append(encoded)
        cur_bytes += len(encoded)
    if cur_lines:
        chunks.append(_flush_chunk(cur_lines, part))

    manifest = {
        "schema": "qor.shadow_colab_export.v0",
        "info_only": True,
        "dataset_split": "train_pool",
        "fresh_holdout_rows_exported": 0,
        "exported_rows": len(rows),
        "chunks": chunks,
        "created_at_utc": _utc_now_iso(),
    }
    _atomic_write_text(out_dir / "export_manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


# ----------------------------------------------------------------- Practice-lane post-match hook
def _falsy(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off")


def enabled(game, environ=None) -> bool:
    """True only on ``QORCAP_LANE=practice`` + ``game=='cod'`` with model & valid split configured."""
    env = os.environ if environ is None else environ
    lane = env.get(ENV_LANE)
    if game != "cod" or not (isinstance(lane, str) and lane.strip().lower() == "practice"):
        return False
    if _falsy(env.get(ENV_FLAG)):
        return False
    model = env.get(ENV_MODEL)
    split = env.get(ENV_SPLIT)
    if not isinstance(model, str) or not model.strip() or not Path(model).is_file():
        return False
    if not isinstance(split, str) or split.strip() not in VALID_SPLITS:
        return False
    return True


def maybe_run_cod_mnv4_shadow(
    bundle,
    game,
    *,
    environ=None,
    out_root=None,
    db_path=None,
    infer_fn=None,
    say=None,
) -> bool:
    """Post-match shadow hook. Practice lane + CoD only. Never raises; info_only."""
    try:
        env = os.environ if environ is None else environ
        if not enabled(game, env):
            return False
        res = run_shadow_on_match(
            Path(bundle),
            dataset_split=env[ENV_SPLIT].strip(),
            model_path=Path(env[ENV_MODEL].strip()),
            out_root=Path(out_root) if out_root is not None else default_out_root(env),
            db_path=Path(db_path) if db_path is not None else default_db_path(env),
            infer_fn=infer_fn,
            environ=env,
        )
        if say is not None:
            if "metrics_json" in res.get("calibration_source", "") and (
                "env" in res["calibration_source"] or "explicit" in res["calibration_source"]
            ):
                say(
                    f"  CoD MNv4 shadow WARNING: calibration source {res['calibration_source']} "
                    f"overrode metrics JSON (temperature_milli={res['temperature_milli']}, "
                    f"tau={res['tau_low_bps']}/{res['tau_high_bps']})"
                )
            say(
                f"  CoD MNv4 shadow (info only, not Counted): "
                f"{res['events_count']} events, {res['review_queue_count']} queued in {res['review_dir']}"
            )
        return True
    except Exception as e:  # noqa: BLE001 - shadow sidecar must never affect save/verify path
        try:
            if say is not None:
                say(f"  CoD MNv4 shadow did not run (info only; match unaffected): {type(e).__name__}: {e}")
        except Exception:
            pass
        return False


# ----------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QorCap Practice MNv4 Shadow Runner & Review Sync (info_only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analyze", help="Run post-match shadow pipeline on a Practice CoD match folder")
    p_an.add_argument("--match-dir", required=True)
    p_an.add_argument("--split", required=True, choices=VALID_SPLITS)
    p_an.add_argument("--model", required=True)
    p_an.add_argument("--metrics", help="Optional model train_metrics_*.json with temperature & tau gates")
    p_an.add_argument("--model-id", default=None)
    p_an.add_argument("--temperature", type=float, default=None, help="Override softmax temperature (> 0)")
    p_an.add_argument("--tau-low-bps", type=int, default=None, help="Override NO_HIT threshold (0..10000)")
    p_an.add_argument("--tau-high-bps", type=int, default=None, help="Override GUNFIRE_HIT threshold (0..10000)")
    p_an.add_argument("--out", help="Output root outside match folder")
    p_an.add_argument("--db", help="SQLite DB path")
    p_an.add_argument("--inputs", help="Optional explicit inputs.jsonl or hand_timeline.v0.jsonl path")
    p_an.add_argument("--no-clips", action="store_true")

    p_lbl = sub.add_parser("label", help="Write H/N/S confirmation label back into SQLite")
    p_lbl.add_argument("--db", required=True)
    p_lbl.add_argument("--event-id", required=True)
    p_lbl.add_argument("--key", required=True, help="H=GUNFIRE_HIT, N=NO_HIT, S=SKIP")
    p_lbl.add_argument("--manifest")

    p_srv = sub.add_parser("serve", help="Serve 2-minute review pack on 127.0.0.1 loopback")
    p_srv.add_argument("--review-dir", required=True)
    p_srv.add_argument("--db", required=True)
    p_srv.add_argument("--port", type=int, default=8765)

    p_exp = sub.add_parser("export-colab", help="Export train_pool human-labeled rows for Colab")
    p_exp.add_argument("--db", required=True)
    p_exp.add_argument("--out", required=True)
    p_exp.add_argument("--split", default="train_pool")
    p_exp.add_argument("--session-id", action="append", default=[])

    args = ap.parse_args(argv)
    if args.cmd == "analyze":
        res = run_shadow_on_match(
            Path(args.match_dir),
            dataset_split=args.split,
            model_path=Path(args.model),
            metrics_path=Path(args.metrics) if args.metrics else None,
            model_id=args.model_id,
            temperature=args.temperature,
            tau_low_bps=args.tau_low_bps,
            tau_high_bps=args.tau_high_bps,
            out_root=Path(args.out) if args.out else None,
            db_path=Path(args.db) if args.db else None,
            inputs_path=Path(args.inputs) if args.inputs else None,
            extract_clips=not args.no_clips,
        )
        print(json.dumps(res, sort_keys=True))
        return 0
    if args.cmd == "label":
        lbl = write_human_label(
            Path(args.db),
            args.event_id,
            args.key,
            manifest_path=Path(args.manifest) if args.manifest else None,
        )
        print(lbl)
        return 0
    if args.cmd == "serve":
        srv = serve_review_pack(Path(args.review_dir), Path(args.db), port=args.port)
        actual_port = srv.server_address[1]
        print(f"Serving practice review pack at http://127.0.0.1:{actual_port}/review.html")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0
    if args.cmd == "export-colab":
        res = export_train_pool_for_colab(
            Path(args.db),
            Path(args.out),
            requested_split=args.split,
            session_ids=args.session_id or None,
        )
        print(json.dumps(res, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
