"""C11 / B9r: pre-sign final check fails closed on a busy inputs lock."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import threading
import time

from test_qorcap_match import _health_rows, _tail_bundle, qm


def test_inputs_lock_taken_between_the_two_final_check_reads_fails_closed(tmp_path, monkeypatch):
    import qorcap_reader
    assert qorcap_reader.INPUTS_LOCK_TIMEOUT_S <= 5
    bundle = _tail_bundle(tmp_path)
    share = qorcap_reader.shared_inputs(bundle / "inputs.jsonl")
    holder_has = threading.Event(); release = threading.Event()
    orig = qorcap_reader.note_options_from_inputs
    calls = []
    def hold():
        with share._lock:
            holder_has.set(); release.wait(30)
    def first_call_then_refresh_grabs_lock(core, path, timeout=None):
        calls.append(timeout)
        orig(core, path, timeout=timeout)
        threading.Thread(target=hold, daemon=True).start()
        assert holder_has.wait(2)
    monkeypatch.setattr(qorcap_reader, "note_options_from_inputs", first_call_then_refresh_grabs_lock)
    draft = {"result": {"method": "agreed_scorebug_final", "final_confirmed": True, "home_score": 7, "away_score": 10}}
    box = {}
    def run():
        t0 = time.perf_counter()
        try:
            box["out"] = qm.tail_final_check(bundle, draft, ffmpeg="ffmpeg", core_factory=lambda post: qorcap_reader.ReaderCore(post, None, None, None, lambda _tl: None))
            box["err"] = None
        except Exception as exc:
            box["err"] = exc
        box["dt"] = time.perf_counter() - t0
    th = threading.Thread(target=run, daemon=True); th.start()
    try:
        th.join(qorcap_reader.INPUTS_LOCK_TIMEOUT_S + 3)
        assert not th.is_alive()
        assert calls and calls[0] is not None and calls[0] <= qorcap_reader.INPUTS_LOCK_TIMEOUT_S
        assert box["err"] is None, box["err"]
        assert box["dt"] < qorcap_reader.INPUTS_LOCK_TIMEOUT_S + 1.5, box["dt"]
        assert box["out"]["result"]["final_confirmed"] is False
        assert box["out"]["result"]["home_score"] == 7 and box["out"]["result"]["away_score"] == 10
        rows = _health_rows(bundle / "reader_health.jsonl")
        assert [r["kind"] for r in rows] == ["inputs_unavailable"], rows
        assert not (bundle / "final.tail.json").exists()
    finally:
        release.set(); th.join(3)

def test_first_read_busy_then_lock_free_still_fails_closed(tmp_path, monkeypatch):
    import qorcap_reader
    bundle = _tail_bundle(tmp_path)
    share = qorcap_reader.shared_inputs(bundle / "inputs.jsonl")
    real_view = share.view
    n = {"calls": 0}
    def view(timeout=None):
        n["calls"] += 1
        if n["calls"] == 1:
            time.sleep(min(0.2, timeout or 0.2))
            raise qorcap_reader.InputsBusy("inputs lock busy")
        return real_view(timeout=timeout)
    monkeypatch.setattr(share, "view", view)
    draft = {"result": {"method": "agreed_scorebug_final", "final_confirmed": True, "home_score": 7, "away_score": 10}}
    out = qm.tail_final_check(bundle, draft, ffmpeg="ffmpeg", core_factory=lambda post: qorcap_reader.ReaderCore(post, None, None, None, lambda _tl: None))
    assert n["calls"] == 1, n
    assert out["result"]["final_confirmed"] is False
    assert [r["kind"] for r in _health_rows(bundle / "reader_health.jsonl")] == ["inputs_unavailable"]
