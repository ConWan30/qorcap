"""125430 public highlights at real source times.

The clips are evaluation only. No template in the reader was cut from them. ``MONO`` drops the
overlap so time stays monotonic, which is the reviewer's ``seq_real.py 125430 6`` run. Each
deadlock from that review must finish at the true 7-14 or abstain. It must not name another winner.

Clock and banner templates are not trained here. A non-null score or quarter that is not the
clip's labelled score is a confident wrong read.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import cv2
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))

from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow  # noqa: E402
from qorcap_standard import standard_reader  # noqa: E402

_HELD_OUT = json.loads((HERE / "packs" / "held_out_sha256.json").read_text(encoding="utf-8"))
QORTIME_COMMIT = _HELD_OUT["qortime_commit"]
CLIP_URL = "https://github.com/ConWan30/qortime/raw/" + QORTIME_COMMIT + "/site/matches/125430/clips/{name}.mp4"
# source.start_s from site/matches/125430/clips/manifest.json. hl00 overlaps hl01.
START_S = {
    "hl00-q1-a0-h6": 58.375,
    "hl01-q1-a0-h7": 67.170,
    "hl02-q2-a6-h7": 378.650,
    "hl03-q2-a7-h7": 400.127,
    "hl04-q2-a7-h13": 599.476,
    "hl05-q2-a7-h14": 612.028,
}
ORDER = tuple(START_S)
LEGAL = {(0, 0), (0, 6), (0, 7), (6, 7), (7, 7), (7, 13), (7, 14)}
# A highlight opens on the previous score and ends on the score in its name.
ON_CLIP = {
    "hl00-q1-a0-h6": {(0, 6)},
    "hl01-q1-a0-h7": {(0, 6), (0, 7)},
    "hl02-q2-a6-h7": {(0, 7), (6, 7)},
    "hl03-q2-a7-h7": {(6, 7), (7, 7)},
    "hl04-q2-a7-h13": {(7, 7), (7, 13)},
    "hl05-q2-a7-h14": {(7, 13), (7, 14)},
}
STRIDE = 6


def _need_score_templates():
    """Held-out clips are for grading. They skip until an allowlisted harvest loads."""
    from qorcap_template_guard import has_role
    if not has_role("score", profile="standard"):
        pytest.skip("templates_missing: held-out 125430 evaluation waits for allowlisted score templates")


def _cache() -> Path:
    root = Path(os.environ.get("QORCAP_HL125430_CACHE", "/tmp/hl125430-eval"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _clip_sha(name: str) -> str:
    suffix = f"site/matches/125430/clips/{name}.mp4"
    hits = [digest for digest, path in _HELD_OUT["sha256"].items() if path == suffix]
    if len(hits) != 1:
        raise KeyError(suffix)
    return hits[0]


def _clip(name: str) -> Path:
    path = _cache() / "clips" / f"{name}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    expect = _clip_sha(name)
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expect:
        return path
    urllib.request.urlretrieve(CLIP_URL.format(name=name), path)
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != expect:
        raise RuntimeError(f"{name} sha256 {got} != {expect} at {QORTIME_COMMIT}")
    return path


def _frames(name: str) -> Path:
    dest = _cache() / "frames" / name
    if (dest / "f000.png").is_file() and (dest / "f354.png").is_file():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    raw = dest / "_raw_%04d.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(_clip(name)),
         "-vf", "select='not(mod(n\\,6))'", "-vsync", "0", str(raw)],
        check=True,
    )
    for i, src in enumerate(sorted(dest.glob("_raw_*.png"))):
        src.rename(dest / f"f{i * STRIDE:03d}.png")
    return dest


def _t_ms(name: str, frame_n: int, extra_ms: int = 0) -> int:
    return int(round((START_S[name] + frame_n / 30.0) * 1000)) + int(extra_ms)


def _load(name: str, frame_n: int):
    return cv2.imread(str(_frames(name) / f"f{frame_n:03d}.png"))


def _samples():
    for name in ORDER:
        dest = _frames(name)
        for path in sorted(dest.glob("f*.png")):
            n = int(path.stem[1:])
            yield name, n


def replay(*, mono: bool, seed=None, shift=None, skip=(), withhold=()):
    """Agreement window plus note_lock, the same wiring as ReaderCore._post_agreed.

    ``withhold`` drops a frame before the scenario reader sees it when a fresh read's score is in
    that set. That is how a lock is missed. hl02 opens on 0-7, so skipping hl01 alone still shows it.
    """
    reader = standard_reader()
    scout = standard_reader() if withhold else None
    agree = AgreementWindow()
    locks = []
    cursor = None
    seeded = False
    for name, n in _samples():
        if name in skip:
            continue
        t_ms = _t_ms(name, n, (shift or {}).get(name, 0))
        if mono and cursor is not None and t_ms < cursor:
            continue
        img = _load(name, n)
        if scout is not None:
            probe = scout.read(img)
            if (probe.left_score, probe.right_score) in withhold:
                cursor = t_ms
                continue
        if seed is not None and not seeded:
            reader.note_lock(t_ms - 1, seed[0], seed[1])
            seeded = True
        fr = reader.read(img, t_ms=t_ms)
        band, _why = reader.reference_band(img)
        crop = hashlib.sha256(band.tobytes()).hexdigest()[:16] if band is not None else "noband"
        res = agree.offer(fr, clock_ns=t_ms * 1_000_000, crop_hash=crop, session_id="125430")
        if res.agreed and res.read is not None and res.read.pair is not None:
            pair = (int(res.read.pair[0]), int(res.read.pair[1]))
            reader.note_lock(t_ms, pair[0], pair[1])
            if not locks or locks[-1] != pair:
                locks.append(pair)
        cursor = t_ms
    final = locks[-1] if locks and reader.grammar.allows_final_fallback() else None
    return locks, final


def test_stride6_reads_do_not_invent_a_score_or_a_quarter():
    """A clip title is the lock the highlight was cut for, not the score on frame 0.

    hl01 opens on 0-6 and only later shows 0-7. Two fresh reads of one frame match.
    """
    _need_score_templates()
    bad = []
    for name, n in _samples():
        a = standard_reader().read(_load(name, n))
        b = standard_reader().read(_load(name, n))
        assert (a.left_score, a.right_score, a.quarter, a.clock_seconds) == (
            b.left_score, b.right_score, b.quarter, b.clock_seconds)
        if a.left_score is not None and (a.left_score, a.right_score) not in ON_CLIP[name]:
            bad.append((name, n, a.left_score, a.right_score))
    assert bad == [], bad[:8]


def test_real_time_deadlocks_reach_7_14_or_abstain():
    _need_score_templates()
    # Late first lock is 0-6. MONO=1, no seed: the reviewer's seq_real.py 125430 6.
    scenarios = {
        "late_0_6": dict(mono=True),
        "pat_after_45s": dict(mono=True, seed=(0, 0), shift={"hl03-q2-a7-h7": 60_000}),
        # Reviewer's SKIP=hl01. hl02 still opens on 0-7, so those reads are withheld too.
        # The anchor has to jump 0-6 to 6-7, which moves both sides.
        "missed_0_7": dict(mono=True, seed=(0, 0), skip={"hl01-q1-a0-h7"},
                          withhold={(0, 7)}),
        "wrong_3_7": dict(mono=True, seed=(3, 7)),
        "cut": dict(mono=False, seed=(0, 0)),
    }
    for name, kw in scenarios.items():
        locks, final = replay(**kw)
        print("SCENARIO", name, "LOCKS", locks, "FINAL", final)
        if name == "missed_0_7":
            assert (0, 7) not in locks, locks
        assert all(pair in LEGAL for pair in locks), (name, locks)
        assert final in (None, (7, 14)), (name, locks, final)
        assert final not in {(0, 6), (6, 7), (3, 7), (3, 10)}
