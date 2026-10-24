"""Pre-sign notices. The signed receipt and the posted final stay as they were.

FU2 writes a sidecar note when the check fails closed. FU3 freezes the live
reader's final and reanchor notes after the seal. FU4 bounds the whole check
by one deadline. FU6 lets a slow read that finishes under that deadline through.
"""
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qorcap_match as qm  # noqa: E402
import qorcap_reader  # noqa: E402
from qorcap_reanchor_notes import FILENAME  # noqa: E402


def _bundle(tmp_path):
    bundle = tmp_path / "m"
    bundle.mkdir()
    (bundle / "inputs.jsonl").write_text('{"type":"in","b":0,"t_ms":1}\n', encoding="utf-8")
    (bundle / "timeline.jsonl").write_text(
        json.dumps({"t_ms": 1000, "home": 7, "away": 10, "quarter": 4, "clock_s": 5}) + "\n",
        encoding="utf-8")
    (bundle / "manifest.jsonl").write_text(
        json.dumps({"type": "segment", "file": "video.mp4", "wall_ms": 5000, "seq": 0}) + "\n",
        encoding="utf-8")
    (bundle / "video.mp4").write_bytes(b"")
    return bundle


def _draft():
    return {"result": {"method": "agreed_scorebug_final", "final_confirmed": True,
                       "home_score": 7, "away_score": 10}}


def _core(post):
    return qorcap_reader.ReaderCore(post, None, None, None, lambda _tl: None)


def _notes(bundle):
    path = bundle / FILENAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _health(bundle):
    path = bundle / "reader_health.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_fail_closed_check_writes_a_sidecar_note_not_a_receipt_field(tmp_path):
    """The pre-sign failure is a sidecar line. The draft result gains no notes field."""
    bundle = _bundle(tmp_path)
    share = qorcap_reader.shared_inputs(bundle / "inputs.jsonl")
    assert share._lock.acquire(timeout=1)
    try:
        out = qm.tail_final_check(bundle, _draft(), ffmpeg="ffmpeg", core_factory=_core, frames=[])
    finally:
        share._lock.release()
    assert out["result"]["final_confirmed"] is False
    assert out["result"]["home_score"] == 7 and out["result"]["away_score"] == 10
    assert "notes" not in out["result"]
    rows = _notes(bundle)
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "pre_sign_fail_closed"
    assert rows[0]["lock"] == "inputs_unavailable"
    assert rows[0]["notice"]
    assert [r["kind"] for r in _health(bundle)] == ["inputs_unavailable"]
    assert not (bundle / "final.tail.json").exists()


def test_seal_freezes_final_and_reanchor_notes(tmp_path):
    """After the join cap, a late frame cannot replace the final or append a note."""
    from qor_receipt.final_detect import detect_final

    notes = []
    posts = []
    core = qorcap_reader.ReaderCore(posts.append, None, None, None, detect_final)
    core.reanchor_note = notes.append
    health = []
    core.health_write = health.append
    core.final = {"type": "final", "basis": "end_menu", "home": 7, "away": 10, "t_ms": 1}
    core.timeline = [
        {"t_ms": 1000 + i * 1000, "home": 14, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
        for i in range(5)
    ]
    state = {"core": core, "final": {"type": "final", "basis": "end_menu", "home": 7, "away": 10}}

    def sleep():
        time.sleep(2)

    thread = threading.Thread(target=sleep, daemon=True)
    thread.start()
    state["reader_thread"] = thread
    try:
        assert qm.seal_reader(threading.Event(), state, timeout=0.2) is False
    finally:
        thread.join(3)
    assert state.get("reader_frozen") is True
    assert core._frozen is True
    assert [r["kind"] for r in health] == ["reader_join_timeout"]
    core._check_final(20_000)
    assert core.final["basis"] == "end_menu"
    assert (core.final["home"], core.final["away"]) == (7, 10)
    assert posts == []
    fr = type("F", (), {})()
    fr.detail = {"grammar_reanchor": True}
    fr.left_score, fr.right_score, fr.quarter = 1, 0, 4
    core._note_reanchor(3000, fr)
    assert notes == []
    assert qm.note_reader_final(state, {"type": "final", "basis": "clock_zero", "home": 1, "away": 0}) is False
    assert state["final"]["basis"] == "end_menu"


def test_pre_sign_check_uses_one_time_budget(tmp_path, monkeypatch):
    """The two pre-sign reads share one deadline. A fresh timeout on the second read takes about twice as long."""
    assert qorcap_reader.INPUTS_LOCK_TIMEOUT_S == 2.0
    bundle = _bundle(tmp_path)
    share = qorcap_reader.shared_inputs(bundle / "inputs.jsonl")
    real = share.view
    n = {"c": 0}

    def view(timeout=None):
        n["c"] += 1
        if n["c"] == 1:
            time.sleep(1.2)
            return real(timeout=timeout)
        if timeout is None:
            time.sleep(5)
            return real(timeout=timeout)
        time.sleep(min(5.0, float(timeout) + 0.05))
        raise qorcap_reader.InputsBusy("inputs lock busy")

    monkeypatch.setattr(share, "view", view)
    box = {}

    def run():
        t0 = time.perf_counter()
        box["out"] = qm.tail_final_check(bundle, _draft(), ffmpeg="ffmpeg", core_factory=_core, frames=[])
        box["dt"] = time.perf_counter() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(qorcap_reader.INPUTS_LOCK_TIMEOUT_S + 0.7)
    assert not th.is_alive(), box
    assert box["dt"] < qorcap_reader.INPUTS_LOCK_TIMEOUT_S + 0.7, box["dt"]
    assert n["c"] == 2, n
    assert box["out"]["result"]["final_confirmed"] is False
    assert "notes" not in box["out"]["result"]
    assert _notes(bundle)[0]["lock"] == "inputs_unavailable"


def test_slow_inputs_read_under_two_seconds_still_returns(tmp_path):
    """A read that finishes near the 2 s budget is not a fail-closed check."""
    assert qorcap_reader.INPUTS_LOCK_TIMEOUT_S == 2.0
    bundle = _bundle(tmp_path)
    share = qorcap_reader.shared_inputs(bundle / "inputs.jsonl")
    assert share._lock.acquire(timeout=1)
    release = threading.Event()

    def hold():
        time.sleep(1.6)
        release.set()
        share._lock.release()

    threading.Thread(target=hold, daemon=True).start()
    t0 = time.perf_counter()
    try:
        out = qm.tail_final_check(bundle, _draft(), ffmpeg="ffmpeg", core_factory=_core, frames=[])
    finally:
        release.wait(3)
    dt = time.perf_counter() - t0
    assert 1.4 < dt < qorcap_reader.INPUTS_LOCK_TIMEOUT_S + 0.5, dt
    assert out["result"]["final_confirmed"] is True
    assert out["result"]["home_score"] == 7 and out["result"]["away_score"] == 10
    assert "notes" not in out["result"]
    assert _notes(bundle) == []
    assert _health(bundle) == []
