"""Post-final capture tail.

The recorder used to auto-stop a few seconds after the end screen. These tests drive the real
stop decision and the real segment/checkpoint link (the same JCS link as evidence::link) and
check that tail frames stay on that chain.

QORTIME must be importable. CI sets it. A missing checkout errors here; it must not skip.
"""
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
import qorcap_match as qm  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def _run(tail_s, source, state=None):
    state = {} if state is None else state
    clock = _Clock()
    chain = qm.new_segment_chain()
    cps = qm.new_checkpoint_chain()
    reason, stopped = qm.run_match_capture(
        state, tail_s=tail_s, source=source, chain=chain, checkpoints=cps,
        clock=clock.clock, sleep=clock.sleep)
    return reason, stopped, state, chain, cps


def _frames(now):
    return {"segments": [f"frame-{now:.3f}".encode()]}


def test_default_tail_runs_about_ten_seconds_and_chain_verifies():
    """Unset env is 10 s past the first final_screen, then auto-stop. The tail is on the chain."""
    tail = qm.post_final_tail_s({})
    assert tail == qm.POST_FINAL_TAIL_DEFAULT_S == 10
    detect = 2.0

    def source(now, state):
        if now >= detect and state.get("final_screen_mono") is None:
            qm.note_final_screen(state, now, wall_ms=int(round(now * 1000)))
        return _frames(now)

    reason, stopped, state, chain, cps = _run(tail, source)
    first = state["final_screen_mono"]
    poll = 1.0 / qm.TAIL_HZ
    assert reason == "final"
    assert stopped - first == pytest.approx(tail)
    assert stopped - first <= tail + poll
    rows = chain["rows"]
    first_wall = state["final_screen_wall_ms"]
    tail_rows = [r for r in rows if r["wall_ms"] > first_wall]
    assert tail_rows, "tail frames were not committed"
    boundary = next(i for i, r in enumerate(rows) if r["wall_ms"] == first_wall)
    assert rows[boundary + 1]["seq"] == rows[boundary]["seq"] + 1
    assert rows[boundary + 1]["prev"] == rows[boundary]["chain_head"]
    assert qm.verify_capture_chain(rows, cps["rows"]) == []
    assert cps["rows"][-1]["qorcap"]["chain_head"] == rows[-1]["chain_head"]
    assert any(cp["qorcap"]["chain_head"] == tail_rows[-1]["chain_head"] for cp in cps["rows"])
    assert len(rows) == int(round(stopped / poll)) + 1
    # A tail byte that is not the committed payload must fail the same verifier.
    broken = [dict(r) for r in rows]
    broken[-1] = dict(broken[-1], payload=b"dropped-tail-frame")
    assert qm.verify_capture_chain(broken, cps["rows"]) != []
    gapped = rows[:boundary + 1] + rows[boundary + 2:]
    assert qm.verify_capture_chain(gapped, cps["rows"]) != []


def test_zero_tail_stops_on_todays_end_menu_hold():
    """0 keeps the 4 s end-menu hold, not the 10 s tail. Those frames are still one chain."""
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "0"}) == 0

    def source(now, state):
        if state.get("final_screen_mono") is None:
            qm.note_final_screen(state, now, wall_ms=int(round(now * 1000)))
        state["end_menu_seen"] = True
        if state.get("final_seen_at") is None:
            state["final_seen_at"] = now
        state["final"] = {"type": "final", "basis": "end_menu", "method": "end_menu_read",
                          "home": 7, "away": 3}
        return _frames(now)

    reason, stopped, state, chain, cps = _run(0.0, source)
    poll = 1.0 / qm.TAIL_HZ
    assert reason == "final"
    assert stopped == pytest.approx(qm.END_MENU_HOLD_S + poll)
    assert stopped < qm.POST_FINAL_TAIL_DEFAULT_S
    rows = chain["rows"]
    assert len(rows) > 1
    assert any(r["wall_ms"] > state["final_screen_wall_ms"] for r in rows)
    assert rows[-1]["wall_ms"] == int(round(stopped * 1000))
    assert qm.verify_capture_chain(rows, cps["rows"]) == []


def test_early_stop_during_tail_stops_immediately_and_chain_verifies():
    """ENTER or Q during the tail stops on that tick. Frames already captured still verify."""
    tail = qm.post_final_tail_s({})

    def source(now, state):
        if state.get("final_screen_mono") is None:
            qm.note_final_screen(state, now, wall_ms=0)
        if now >= 3:
            state["user_stop"] = True
        return _frames(now)

    reason, stopped, state, chain, cps = _run(tail, source)
    poll = 1.0 / qm.TAIL_HZ
    rows = chain["rows"]
    assert reason == "enter"
    assert stopped == pytest.approx(3)
    assert stopped < tail
    assert rows[-1]["wall_ms"] == 3000
    assert any(r["wall_ms"] > state["final_screen_wall_ms"] for r in rows)
    assert len(rows) == int(round(stopped / poll)) + 1
    assert qm.verify_capture_chain(rows, cps["rows"]) == []


def test_out_of_range_tail_clamps():
    """Below 0 becomes 0 (today's hold). Above 60 becomes 60. Capture uses that clamped value."""
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "-5"}) == 0
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "90"}) == 60
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "60"}) == 60
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "0"}) == 0
    assert qm.post_final_tail_s({}) == 10
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "nope"}) == 10

    def source(now, state):
        if state.get("final_screen_mono") is None:
            qm.note_final_screen(state, now, wall_ms=0)
        return {"segments": [b"x"]}

    hi = qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "90"})
    reason, stopped, *_ = _run(hi, source)
    assert reason == "final"
    assert stopped == pytest.approx(60)

    lo = qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "-1"})
    state = {"end_menu_seen": True, "final_seen_at": 0.0, "final_screen_mono": 0.0,
             "final_screen_wall_ms": 0}
    reason, stopped, _, chain, cps = _run(lo, lambda now, state: {"segments": [b"x"]}, state)
    poll = 1.0 / qm.TAIL_HZ
    assert reason == "final"
    assert stopped == pytest.approx(qm.END_MENU_HOLD_S + poll)
    assert stopped < 10
    assert qm.verify_capture_chain(chain["rows"], cps["rows"]) == []


def test_source_error_during_tail_stops_fail_closed():
    """A broken capture during the tail stops at once and does not hash the failing tick."""
    def source(now, state):
        if state.get("final_screen_mono") is None:
            qm.note_final_screen(state, now, wall_ms=0)
        if now >= 4:
            return {"error": "pipe_broken", "segments": [b"do-not-hash-this"]}
        return _frames(now)

    reason, stopped, state, chain, cps = _run(10.0, source)
    rows = chain["rows"]
    assert reason == "pipe_broken"
    assert stopped == pytest.approx(4)
    assert stopped < 10
    assert rows
    assert all(r["wall_ms"] < 4000 for r in rows)
    assert all(r["payload"] != b"do-not-hash-this" for r in rows)
    assert rows[-1]["seq"] == len(rows) - 1
    assert qm.verify_capture_chain(rows, cps["rows"]) == []
    assert qm.died_during_post_final_tail(state, stopped, 10.0) is True
    assert qm.died_during_post_final_tail(state, 10.0, 10.0) is False
    assert qm.died_during_post_final_tail(state, stopped, 0.0) is False


def test_tail_duration_is_recorded_on_bundle_not_receipt(tmp_path):
    """The duration and the first final_screen time live on bundle.json, not receipt.v0."""
    bundle = tmp_path / "match-x"
    bundle.mkdir()
    (bundle / "bundle.json").write_text(json.dumps({
        "schema": "qorcap-bundle/v0", "qorcap_version": "0.3.27", "role": "home"}) + "\n")
    qm.record_post_final_tail(bundle, secs=10.0, final_screen_wall_ms=1_700_000_000_000)
    qm.opt_in_challenge_rule(bundle)
    meta = json.loads((bundle / "bundle.json").read_text())
    assert meta["schema"] == "qorcap-bundle/v0" and meta["role"] == "home"
    assert meta["challenge_rule"] == 2
    assert meta["post_final_tail"] == {"secs": 10.0, "final_screen_wall_ms": 1_700_000_000_000}
    schema = json.loads((HERE.parent / "contracts" / "receipt.v0.json").read_text())
    result_props = schema["properties"]["result"]["properties"]
    assert "post_final_tail" not in schema["properties"]
    assert "post_final_tail" not in result_props
    assert "final_screen_wall_ms" not in result_props
    missing = tmp_path / "absent"
    qm.record_post_final_tail(missing, secs=10, final_screen_wall_ms=1)
    assert not (missing / "bundle.json").exists()
    # No final_screen: record the duration only.
    (bundle / "bundle.json").write_text(json.dumps({
        "schema": "qorcap-bundle/v0", "qorcap_version": "0.3.27"}) + "\n")
    qm.record_post_final_tail(bundle, secs=0.0, final_screen_wall_ms=None)
    meta = json.loads((bundle / "bundle.json").read_text())
    assert meta["post_final_tail"] == {"secs": 0.0}
