"""170123 public highlight clips: clock, banner, grammar, and the hard bar.

Samples are the eval kit's indices. hl00 starts at frame 7, step 12. hl01 and hl02 start at frame 6,
step 12. JPEG q=20 and an 11 px horizontal blur are the kit variants. Traps are the kit's banner
frames and the band-only JPEG recompressions (q=13, q=8, q=21, q=12).

Clips are downloaded from the public qortime tree and are not committed. A confident wrong field
fails the test. Abstain is not a wrong read. Traps run on a fresh reader so a clock history cannot
hide 7:03.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))

from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow  # noqa: E402
from qorcap_reader import prep  # noqa: E402
from qorcap_standard import standard_reader  # noqa: E402

FIXTURE = HERE / "eval" / "170123_hl"
LABELS = json.loads((FIXTURE / "labels.json").read_text())
TRAPS = json.loads((FIXTURE / "traps.json").read_text())
CLIP_URL = "https://github.com/ConWan30/qortime/raw/main/site/matches/170123/clips/{name}.mp4"
CLIPS = ("hl00-q3-a3-h0", "hl01-q4-a9-h0", "hl02-q4-a10-h0")
START = {"hl00-q3-a3-h0": 7, "hl01-q4-a9-h0": 6, "hl02-q4-a10-h0": 6}
# Clips are separate highlights of one match. 20 s between them keeps the 12 s banner window from
# crossing a clip and lets the 45 s try window reach the point after the touchdown.
CLIP_GAP_MS = 20_000
FIELDS = ("away", "home", "quarter", "clock")


def _cache() -> Path:
    root = Path(os.environ.get("QORCAP_HL170123_CACHE", "/tmp/hl170123-eval"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _clip(name: str) -> Path:
    path = _cache() / "clips" / f"{name}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 100_000:
        return path
    urllib.request.urlretrieve(CLIP_URL.format(name=name), path)
    return path


def _indices(name: str) -> list[int]:
    # 30 frames. The stop is exclusive, and the last index is 355 (hl00) or 354 (hl01, hl02).
    return list(range(START[name], 356, 12))


def _extract(name: str) -> Path:
    dest = _cache() / "frames" / name
    idxs = _indices(name)
    if all((dest / f"f{n:03d}.png").is_file() for n in idxs):
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    expr = "+".join(f"eq(n\\,{n})" for n in idxs)
    pattern = str(dest / "_raw_%03d.png")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(_clip(name)), "-vf", f"select='{expr}'", "-vsync", "0", pattern],
        check=True,
    )
    for i, n in enumerate(idxs, start=1):
        src = dest / f"_raw_{i:03d}.png"
        src.rename(dest / f"f{n:03d}.png")
    return dest


def _variants(name: str) -> None:
    dest = _extract(name)
    frames = [dest / f"f{n:03d}.png" for n in _indices(name)]
    kernel = np.zeros((1, 11), np.float32)
    kernel[0, :] = 1 / 11
    for i in (2, 7, 12, 17, 22, 27):
        out = Path(str(frames[i]).replace(".png", "_jpg20.jpg"))
        if not out.is_file():
            cv2.imwrite(str(out), cv2.imread(str(frames[i])), [cv2.IMWRITE_JPEG_QUALITY, 20])
    for i in (4, 14, 24):
        out = Path(str(frames[i]).replace(".png", "_blur11.png"))
        if not out.is_file():
            cv2.imwrite(str(out), cv2.filter2D(cv2.imread(str(frames[i])), -1, kernel))


def _grab(name: str, t_ms: int):
    n = round(t_ms * 30 / 1000)
    dest = _cache() / "trap_src" / f"{name}_{n}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(_clip(name)), "-vf", f"select=eq(n\\,{n})",
             "-vsync", "0", "-frames:v", "1", str(dest)],
            check=True,
        )
    return cv2.imread(str(dest))


def _traps() -> dict[str, np.ndarray]:
    """Rebuild the 18 trap frames. Returns name -> image."""
    out = {}
    for name, meta in TRAPS.items():
        clip = name.split("/")[0]
        img = _grab(clip, meta["t_ms"])
        q = meta["jpeg_q"]
        if q:
            small = cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small[330:360], [cv2.IMWRITE_JPEG_QUALITY, int(q)])
            assert ok
            small[330:360] = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            img = cv2.resize(small, (1280, 720), interpolation=cv2.INTER_CUBIC)
        out[name] = img
    return out


def _clock(secs):
    if secs is None:
        return None
    return f"{int(secs) // 60}:{int(secs) % 60:02d}"


def _claims(fr):
    return {
        "away": fr.left_score,
        "home": fr.right_score,
        "quarter": fr.quarter,
        "clock": _clock(fr.clock_seconds),
    }


def _truth_trap(name, meta):
    clip = name.split("/")[0]
    if meta["jpeg_q"] and "banner" not in meta["why"]:
        clock = {0: "1:03", 1000: "1:02", 2267: "1:01"}[meta["t_ms"]]
        return {"away": 0, "home": 0, "quarter": 3, "clock": clock}
    return {
        "away": None,
        "home": None,
        "quarter": 3 if clip.startswith("hl00") else 4,
        "clock": "0:59" if clip.startswith("hl00") else "0:47",
    }


def _wrong(claims, truth):
    bad = []
    for field in FIELDS:
        got, exp = claims[field], truth[field]
        if got is None:
            continue
        if exp is None or str(got) != str(exp):
            bad.append((field, got, exp))
    return bad


def _t_ms(clip_i: int, frame_n: int) -> int:
    return clip_i * CLIP_GAP_MS + int(round(frame_n / 30 * 1000))


def _read_image(reader, img, t_ms):
    t0 = time.perf_counter()
    fr = reader.read(img, t_ms=t_ms)
    return fr, (time.perf_counter() - t0) * 1000


def test_highlights_lock_the_legal_scores_and_add_no_confident_wrong_read():
    for name in CLIPS:
        _variants(name)
    traps = _traps()
    reader = standard_reader()
    agree = AgreementWindow()
    locks = []
    clean_wrong = []
    costs = []
    kit = {f: {"correct": 0, "wrong": 0, "blank": 0, "visible": 0} for f in FIELDS}
    emitted = {f: {"correct": 0, "wrong": 0, "blank": 0, "visible": 0} for f in FIELDS}

    def note_counts(fr, truth, into):
        claimed = _claims(fr)
        kit_claimed = claimed if fr.ok else {f: None for f in FIELDS}
        for field in FIELDS:
            if truth[field] is None:
                continue
            into[field]["visible"] += 1
            got = kit_claimed[field] if into is kit else claimed[field]
            if got is None:
                into[field]["blank"] += 1
            elif str(got) == str(truth[field]):
                into[field]["correct"] += 1
            else:
                into[field]["wrong"] += 1

    for clip_i, name in enumerate(CLIPS):
        for n in _indices(name):
            img = cv2.imread(str(_cache() / "frames" / name / f"f{n:03d}.png"))
            t_ms = _t_ms(clip_i, n)
            fr, ms = _read_image(reader, img, t_ms)
            costs.append(ms)
            truth = LABELS[f"{name}/f{n:03d}.png"]
            bad = _wrong(_claims(fr), truth)
            if bad:
                clean_wrong.append((f"{name}/f{n:03d}.png", bad, fr.reason))
            note_counts(fr, truth, kit)
            note_counts(fr, truth, emitted)
            band, _why = reader.reference_band(img)
            crop = hashlib.sha256(band.tobytes()).hexdigest()[:16] if band is not None else "noband"
            res = agree.offer(fr, clock_ns=t_ms * 1_000_000, crop_hash=crop, session_id="170123")
            if res.agreed and res.read is not None and res.read.pair is not None:
                pair = (int(res.read.pair[0]), int(res.read.pair[1]))
                reader.note_lock(t_ms, pair[0], pair[1])
                if not locks or locks[-1] != pair:
                    locks.append(pair)

    variant_wrong = []
    for key, truth in LABELS.items():
        if truth["variant"] == "clean":
            continue
        img = cv2.imread(str(_cache() / "frames" / key))
        fresh = standard_reader()
        fr, ms = _read_image(fresh, img, None)
        costs.append(ms)
        bad = _wrong(_claims(fr), truth)
        if bad:
            variant_wrong.append((key, bad, fr.reason))
        note_counts(fr, truth, kit)
        note_counts(fr, truth, emitted)

    trap_wrong = []
    trap_rows = []
    for name, meta in TRAPS.items():
        fresh = standard_reader()
        fr, ms = _read_image(fresh, traps[name], None)
        costs.append(ms)
        truth = _truth_trap(name, meta)
        claimed = _claims(fr)
        bad = _wrong(claimed, truth)
        if bad:
            trap_wrong.append((name, bad, fr.reason))
        trap_rows.append((name, fr.ok, fr.reason, claimed))
        note_counts(fr, truth, kit)
        note_counts(fr, truth, emitted)

    costs.sort()
    def pct(n, d):
        return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"

    print("LOCKS", locks)
    print("KIT", {f: f"correct {pct(kit[f]['correct'], kit[f]['visible'])} wrong {kit[f]['wrong']} blank {kit[f]['blank']}" for f in FIELDS})
    print("EMITTED", {f: f"correct {pct(emitted[f]['correct'], emitted[f]['visible'])} wrong {emitted[f]['wrong']} blank {emitted[f]['blank']}" for f in FIELDS})
    print(
        f"COST n {len(costs)} median {costs[len(costs)//2]:.2f} ms "
        f"p95 {costs[min(len(costs)-1, int(len(costs)*0.95))]:.2f} ms max {costs[-1]:.2f} ms"
    )
    print("TRAPS")
    for row in trap_rows:
        print(" ", row)
    assert locks[:3] == [(0, 0), (3, 0), (9, 0)], locks
    assert (10, 0) in locks, locks
    assert clean_wrong == [], clean_wrong
    assert variant_wrong == [], variant_wrong
    assert trap_wrong == [], trap_wrong
