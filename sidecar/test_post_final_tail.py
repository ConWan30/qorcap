"""Post-final capture tail.

The stop decision is the production one (``observe_final_screen`` and ``capture_stop_reason``).
Reader cases below drive a real ``ReaderCore``. The segment helpers at the bottom model
``evidence::link`` so a test can show tail bytes sit on one chain. ``main()`` does not call
them. Production hashing is the engine, which keeps sealing segments until this sidecar
shuts it down.

QORTIME must be importable. CI sets it. A missing checkout errors here; it must not skip.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
import qorcap_match as qm  # noqa: E402
from qorcap_endmenu import EndMenuAgreement  # noqa: E402
from qorcap_reader import (  # noqa: E402
    FINAL_SCREEN_MIN_FRAMES,
    FINAL_SCREEN_MIN_SPAN_MS,
    ReaderCore,
    end_menu_delta_corroborated,
    madden_final_screen,
)


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
    chain = new_segment_chain()
    cps = new_checkpoint_chain()
    reason, stopped = run_match_capture(
        state, tail_s=tail_s, source=source, chain=chain, checkpoints=cps,
        clock=clock.clock, sleep=clock.sleep)
    return reason, stopped, state, chain, cps


def _frames(now):
    return {"segments": [f"frame-{now:.3f}".encode()]}


def _arm_end_menu(state, now, wall_ms=0):
    """Posted end-menu final plus the screen clock, the way a finished menu looks mid-tail."""
    if state.get("final_screen_mono") is None:
        qm.note_final_screen(state, now, wall_ms=wall_ms)
        state["final_screen_start_reason"] = "posted_final"
    if state.get("final") is None:
        state["final"] = {"type": "final", "basis": "end_menu", "method": "end_menu_read",
                          "home": 0, "away": 7, "final_confirmed": True, "t_ms": wall_ms}
        state["end_menu_seen"] = True
        state["final_seen_at"] = now


def test_default_tail_adds_ten_seconds_and_chain_verifies():
    """An end-menu final at t=0 keeps the 4 s hold and also the 10 s tail. Stop is the later one."""
    tail = qm.post_final_tail_s({})
    assert tail == qm.POST_FINAL_TAIL_DEFAULT_S == 10

    def source(now, state):
        _arm_end_menu(state, now if state.get("final_seen_at") is None else state["final_seen_at"],
                      wall_ms=0)
        if state.get("final_seen_at") != 0.0:
            # Pin both clocks at the first tick so the deadlines are 4 s and 10 s.
            state["final_seen_at"] = 0.0
            state["final_screen_mono"] = 0.0
        return _frames(now)

    reason, stopped, state, chain, cps = _run(tail, source)
    poll = 1.0 / qm.TAIL_HZ
    assert reason == "final"
    assert stopped == pytest.approx(tail)
    assert stopped > qm.END_MENU_HOLD_S
    rows = chain["rows"]
    tail_rows = [r for r in rows if r["wall_ms"] > 0]
    assert tail_rows, "tail frames were not committed"
    boundary = next(i for i, r in enumerate(rows) if r["wall_ms"] == 0)
    assert rows[boundary + 1]["seq"] == rows[boundary]["seq"] + 1
    assert rows[boundary + 1]["prev"] == rows[boundary]["chain_head"]
    assert verify_capture_chain(rows, cps["rows"]) == []
    assert cps["rows"][-1]["qorcap"]["chain_head"] == rows[-1]["chain_head"]
    assert len(rows) == int(round(stopped / poll)) + 1
    broken = [dict(r) for r in rows]
    broken[-1] = dict(broken[-1], payload=b"dropped-tail-frame")
    assert verify_capture_chain(broken, cps["rows"]) != []
    gapped = rows[:boundary + 1] + rows[boundary + 2:]
    assert verify_capture_chain(gapped, cps["rows"]) != []


def test_zero_tail_stops_on_todays_end_menu_hold():
    """0 keeps the 4 s end-menu hold, not the 10 s tail. Those frames are still one chain."""
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "0"}) == 0

    def source(now, state):
        _arm_end_menu(state, 0.0, wall_ms=0)
        state["final_seen_at"] = 0.0
        state["final_screen_mono"] = 0.0
        return _frames(now)

    reason, stopped, state, chain, cps = _run(0.0, source)
    poll = 1.0 / qm.TAIL_HZ
    assert reason == "final"
    assert stopped == pytest.approx(qm.END_MENU_HOLD_S + poll)
    assert stopped < qm.POST_FINAL_TAIL_DEFAULT_S
    rows = chain["rows"]
    assert len(rows) > 1
    assert verify_capture_chain(rows, cps["rows"]) == []


def test_scorebug_wait_is_not_cut_by_the_tail():
    """A final_screen final still waits 120 s. The 10 s tail does not stop the recording first."""
    state = {
        "final_seen_at": 0.0,
        "final_screen_mono": 0.0,
        "final_screen_start_reason": "final_screen",
        "final": {"type": "final", "basis": "final_screen", "home": 0, "away": 7, "t_ms": 1},
    }
    assert qm.capture_stop_reason(state, 10.0, tail_s=10) is None
    assert qm.capture_stop_reason(state, 120.0, tail_s=10) is None
    assert qm.capture_stop_reason(state, 120.0 + 0.5, tail_s=10) == "final"


def test_screen_alone_does_not_stop_and_a_stop_without_a_final_is_not_final():
    """No posted-final clock, no auto-stop. If that clock is set and no final object exists, the reason is distinct."""
    screen_only = {"final_screen_mono": 0.0, "final_screen_start_reason": "final_screen"}
    assert qm.capture_stop_reason(screen_only, 30.0, tail_s=10) is None
    armed = {"final_seen_at": 0.0, "end_menu_seen": True, "final_screen_mono": 0.0}
    assert qm.capture_stop_reason(armed, 10.0, tail_s=10) == "post_final_tail"
    armed["final"] = {"type": "final", "basis": "end_menu", "home": 0, "away": 7}
    assert qm.capture_stop_reason(armed, 10.0, tail_s=10) == "final"


def test_early_stop_during_tail_stops_immediately_and_chain_verifies():
    """ENTER or Q during the tail stops on that tick. Frames already captured still verify."""
    tail = qm.post_final_tail_s({})

    def source(now, state):
        _arm_end_menu(state, 0.0, wall_ms=0)
        state["final_seen_at"] = 0.0
        state["final_screen_mono"] = 0.0
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
    assert len(rows) == int(round(stopped / poll)) + 1
    assert verify_capture_chain(rows, cps["rows"]) == []


def test_out_of_range_tail_clamps():
    """Below 0 becomes 0 (today's hold). Above 60 becomes 60. Capture uses that clamped value."""
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "-5"}) == 0
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "90"}) == 60
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "60"}) == 60
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "0"}) == 0
    assert qm.post_final_tail_s({}) == 10
    assert qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "nope"}) == 10

    def source(now, state):
        _arm_end_menu(state, 0.0, wall_ms=0)
        state["final_seen_at"] = 0.0
        state["final_screen_mono"] = 0.0
        return {"segments": [b"x"]}

    hi = qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "90"})
    reason, stopped, *_ = _run(hi, source)
    assert reason == "final"
    assert stopped == pytest.approx(60)

    lo = qm.post_final_tail_s({qm.POST_FINAL_TAIL_ENV: "-1"})
    reason, stopped, _, chain, cps = _run(lo, source)
    poll = 1.0 / qm.TAIL_HZ
    assert reason == "final"
    assert stopped == pytest.approx(qm.END_MENU_HOLD_S + poll)
    assert stopped < 10
    assert verify_capture_chain(chain["rows"], cps["rows"]) == []


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
    assert verify_capture_chain(rows, cps["rows"]) == []
    assert qm.died_during_post_final_tail(state, stopped, 10.0) is True
    assert qm.died_during_post_final_tail(state, 10.0, 10.0) is False
    assert qm.died_during_post_final_tail(state, stopped, 0.0) is False


def test_tail_record_is_a_sidecar_and_recover_keeps_it(tmp_path):
    """What happened lives in post_final_tail.json. bundle.json and receipt.v0 gain no field."""
    bundle = tmp_path / "match-x"
    bundle.mkdir()
    (bundle / "bundle.json").write_text(json.dumps({
        "schema": "qorcap-bundle/v0", "qorcap_version": "0.3.27", "role": "home"}) + "\n")
    qm.record_post_final_tail(
        bundle, configured_s=10.0, start_reason="final_screen",
        start_t_ms=1_700_000_000_000, stop_t_ms=1_700_000_004_000, actual_s=4.0)
    side = json.loads((bundle / "post_final_tail.json").read_text())
    assert side == {
        "configured_s": 10.0,
        "start_reason": "final_screen",
        "start_t_ms": 1_700_000_000_000,
        "stop_t_ms": 1_700_000_004_000,
        "actual_s": 4.0,
    }
    raw = (bundle / "post_final_tail.json").read_bytes()
    qm.opt_in_challenge_rule(bundle)
    meta = json.loads((bundle / "bundle.json").read_text())
    assert meta["schema"] == "qorcap-bundle/v0" and meta["role"] == "home"
    assert meta["challenge_rule"] == 2
    assert "post_final_tail" not in meta
    assert (bundle / "post_final_tail.json").read_bytes() == raw
    schema = json.loads((HERE.parent / "contracts" / "receipt.v0.json").read_text())
    result_props = schema["properties"]["result"]["properties"]
    assert "post_final_tail" not in schema["properties"]
    assert "post_final_tail" not in result_props
    bundle_schema = json.loads((HERE.parent / "contracts" / "qorcap-bundle.v0.json").read_text())
    assert "post_final_tail" not in bundle_schema.get("properties", {})
    missing = tmp_path / "absent"
    qm.record_post_final_tail(missing, configured_s=10, start_reason="posted_final",
                              start_t_ms=1, stop_t_ms=2, actual_s=1)
    assert not missing.exists()

    kept = (bundle / "post_final_tail.json").read_bytes()
    (bundle / "video.mp4").write_bytes(b"hashed-bytes" + b"TAILDATA")
    seg = {"type": "segment", "seq": 0, "file": "video.mp4", "offset": 0, "bytes": len(b"hashed-bytes"),
           "sha256": "ab", "prev": "0" * 64, "chain_head": "cd", "wall_ms": 1000, "device": "USB3.0 Video"}
    (bundle / "manifest.jsonl").write_text(json.dumps(seg) + "\n", encoding="utf-8")
    (bundle / "checkpoints.jsonl").write_text(json.dumps({
        "seq": 0, "t_ms": 1500, "hash": "ee",
        "qorcap": {"trigger": "segment", "match_id": "m-tail", "role": "home",
                   "score": {"home": 0, "away": 7, "quarter": 4}}}) + "\n", encoding="utf-8")
    (bundle / "timeline.jsonl").write_text(json.dumps(
        {"t_ms": 1400, "home": 0, "away": 7, "quarter": 4}) + "\n", encoding="utf-8")
    draft = qm.recover_bundle(bundle)
    assert draft is not None and draft["recovered"] is True
    recovered = json.loads((bundle / "bundle.json").read_text())
    assert "post_final_tail" not in recovered
    assert (bundle / "post_final_tail.json").read_bytes() == kept


# ---------------------------------------------------------------- ReaderCore


def _menu(items=6, bg=2):
    img = np.full((360, 640, 3), bg, np.uint8)
    img[22:42, 20:150] = 230
    for k in range(1, items):
        y = 30 + 15 * k
        img[y:y + 6, 25:110] = 200
    img[30:60, 300:420] = 220
    return img


class _Paused:
    paused = True

    def update(self, t, img):
        pass


class _Fin:
    def __init__(self, **k):
        self.__dict__.update(k)


def _detect_final(timeline):
    """Stand-in for QorTime detect_final: Q4, clock 0:00, final_screen span of 3 frames and 3 s."""
    scores = [r for r in timeline if isinstance(r, dict) and "home" in r and "obs" not in r]
    if not scores:
        return _Fin(final=False)
    last = scores[-1]
    q = last.get("quarter")
    if isinstance(q, bool) or not isinstance(q, int) or q < 4 or last.get("clock_s") != 0:
        return _Fin(final=False)
    if last.get("home") == last.get("away"):
        return _Fin(final=False)
    for o in timeline:
        if (o.get("obs") == "final_screen" and (o.get("n") or 0) >= FINAL_SCREEN_MIN_FRAMES
                and int(o["until_ms"]) - int(o["t_ms"]) >= FINAL_SCREEN_MIN_SPAN_MS and o["t_ms"] > last["t_ms"]):
            return _Fin(final=True, basis="final_screen", home=last["home"], away=last["away"],
                        quarter=q, t_ms=last["t_ms"])
    return _Fin(final=False)


class _MenuRead:
    ok = True
    reason = "ok"
    conf = 1.0
    margin = 1.0
    digits = []

    def __init__(self, away, home):
        self.away, self.home = away, home


class _Unread:
    ok = False
    reason = "unreadable"
    away = home = None
    conf = 0.0
    margin = 0.0
    digits = []


class _MenuReader:
    def __init__(self, away, home):
        self.away, self.home = away, home

    def read(self, _img):
        return _MenuRead(self.away, self.home)


class _LateDigits:
    """End-menu digits stay unreadable until ``ready_at``."""

    def __init__(self, away, home, ready_at):
        self.away, self.home, self.ready_at = away, home, ready_at
        self.t = 0

    def read(self, _img):
        if self.t < self.ready_at:
            return _Unread()
        return _MenuRead(self.away, self.home)


class _SwitchScore:
    def __init__(self, first, second, at):
        self.first, self.second, self.at = first, second, at
        self.t = 0

    def read(self, _img):
        pair = self.first if self.t < self.at else self.second
        return _MenuRead(*pair)


def _core(score, reader=None, corr=None):
    posts = []
    core = ReaderCore(posts.append, None, None, _Paused(), _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=reader,
                      end_menu_agree=EndMenuAgreement() if reader is not None else None)
    core.timeline = [dict(score)]
    if corr:
        core.delta_corroboration = list(corr)
    return core, posts


def _sync_final_clock(state, now):
    core = state.get("core")
    fin = getattr(core, "final", None) if core is not None else None
    if isinstance(fin, dict):
        state["final"] = fin
    f = state.get("final")
    if f and qm.is_end_menu_final(f) and not state.get("end_menu_seen"):
        state["end_menu_seen"] = True
        state["final_seen_at"] = now
    elif f and state.get("final_seen_at") is None:
        state["final_seen_at"] = now


def _drive(core, frames, tail_s, reader=None):
    """Feed real frames and poll the production stop, the same order as the live loop."""
    state = {"core": core}
    reason = None
    stopped = None
    seen = []
    for t_ms, img in frames:
        if reader is not None:
            reader.t = t_ms
        core.on_frame(t_ms, img)
        now = t_ms / 1000.0
        qm.observe_final_screen(state, now, wall_ms=t_ms)
        _sync_final_clock(state, now)
        reason = qm.capture_stop_reason(state, now, tail_s=tail_s)
        seen.append(t_ms)
        if reason:
            stopped = now
            break
    return reason, stopped, state, seen


def _menu_frames(t0, t1, step=500):
    img = _menu()
    t = t0
    out = []
    while t <= t1:
        out.append((t, img))
        t += step
    return out


def _committed_screen(timeline):
    return any(
        r.get("obs") == "final_screen" and (r.get("n") or 0) >= FINAL_SCREEN_MIN_FRAMES
        and int(r.get("until_ms") or 0) - int(r.get("t_ms") or 0) >= FINAL_SCREEN_MIN_SPAN_MS
        for r in timeline)


def test_single_frame_in_ot_or_q4_tie_does_not_stop():
    """One end-menu-like frame must not start the tail or stop. A tied picture is still labelled.

    OT 14-14 with 4:00 left, and Q4 0:00 tied 21-21, are ``final_screen`` frames. Holding
    that picture commits a span. The tail does not start from it, and nothing stops,
    because the scorebug is still tied. Restoring the tie rule in ``_accept_final_screen``
    leaves ``stats['final_screen']`` at 0 and commits no span.
    A single frame of a real Q4 0:00 lead is counted, and it still must not start the
    clock: ``stats['final_screen']`` is not the trigger. Replacing ``observe_final_screen``
    with ``return False`` does not satisfy the later test that the tail runs past the 4 s
    hold. Starting the clock from ``stats['final_screen'] > 0`` fails the single-frame lead.
    """
    cases = [
        {"t_ms": 1_000, "home": 14, "away": 14, "quarter": 5, "clock_s": 240, "clock_red": False},
        {"t_ms": 1_000, "home": 21, "away": 21, "quarter": 4, "clock_s": 0, "clock_red": True},
    ]
    for score in cases:
        core, _posts = _core(score)
        core.on_frame(5_000, _menu())
        state = {"core": core}
        assert qm.observe_final_screen(state, 5.0, wall_ms=5_000) is False
        assert state.get("final_screen_mono") is None
        assert qm.capture_stop_reason(state, 20.0, tail_s=10) is None
        assert core.final is None
        assert core.stats.get("final_screen", 0) > 0
        long_core, _ = _core(score)
        reason, stopped, long_state, seen = _drive(long_core, _menu_frames(5_000, 10_000), 10)
        assert reason is None and stopped is None
        assert long_state.get("final_screen_mono") is None
        assert long_core.final is None
        assert long_core.stats.get("final_screen", 0) > 0
        assert seen[-1] == 10_000
        assert _committed_screen(long_core.timeline)

    lead = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    core, _posts = _core(lead)
    core.on_frame(5_000, _menu())
    state = {"core": core}
    assert core.stats.get("final_screen", 0) == 1
    assert qm.observe_final_screen(state, 5.0, wall_ms=5_000) is False
    assert state.get("final_screen_mono") is None
    assert qm.capture_stop_reason(state, 20.0, tail_s=10) is None
    assert core.final is None


def test_committed_end_menu_keeps_the_tail_past_the_hold():
    """A posted end-menu final extends the stop to screen+10 s, past the 4 s hold.

    ``observe_final_screen`` has to run. If it never fires, the screen clock stays unset
    and the recorder stops on the 4 s hold instead of at +10 s from that clock.
    """
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _MenuReader(7, 0)
    core, _posts = _core(score, reader=reader)
    reason, stopped, state, _seen = _drive(core, _menu_frames(5_000, 20_000), 10, reader)
    assert reason == "final"
    assert state.get("final_screen_mono") == pytest.approx(6.0)
    assert stopped - state["final_screen_mono"] == pytest.approx(10)
    assert stopped - state["final_seen_at"] > qm.END_MENU_HOLD_S
    assert core.final["basis"] == "end_menu"
    assert core.final["final_confirmed"] is True
    assert (core.final["away"], core.final["home"]) == (7, 0)


def test_digits_readable_at_20s_still_confirm_end_menu():
    """Digits that only resolve 20 s after the end screen still produce a confirmed end-menu final.

    The recording is still going at screen+10 s and at the committed-span+10 s mark.
    """
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _LateDigits(7, 0, ready_at=25_000)  # menu starts at 5_000; digits at +20 s
    core, _posts = _core(score, reader=reader)
    reason = None
    state = {"core": core}
    img = _menu()
    for t_ms in range(5_000, 28_000, 500):
        reader.t = t_ms
        core.on_frame(t_ms, img)
        now = t_ms / 1000.0
        qm.observe_final_screen(state, now, wall_ms=t_ms)
        _sync_final_clock(state, now)
        reason = qm.capture_stop_reason(state, now, tail_s=10)
        if t_ms <= 25_000:
            assert reason is None, t_ms
        if reason:
            break
    fin = core.final
    assert isinstance(fin, dict) and fin.get("basis") == "end_menu", fin
    assert fin["final_confirmed"] is True
    assert (fin["away"], fin["home"]) == (7, 0)
    assert reason is None


def test_scorebug_return_resets_the_tail_clock():
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    core, _posts = _core(score)
    state = {"core": core}
    img = _menu()
    started = False
    for t_ms in range(5_000, 10_000, 250):
        core.on_frame(t_ms, img)
        if qm.observe_final_screen(state, t_ms / 1000.0, wall_ms=t_ms):
            started = True
            break
    assert started and state.get("final_screen_mono") is not None
    core.timeline.append({"t_ms": 20_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 30, "clock_red": False})
    assert qm.observe_final_screen(state, 20.0, wall_ms=20_000) is False
    assert state.get("final_screen_mono") is None
    assert "final_screen_mono" not in state


def test_tail_button_and_haptic_do_not_confirm():
    """A one-step delta confirms from a haptic before the end screen, and not from one during the tail."""
    st = {"away": 0, "home": 20, "t_ms": 8_000}
    last = {"away": 0, "home": 17, "t_ms": 1_000}
    timeline = [{"t_ms": 1_000, "home": 17, "away": 0, "quarter": 4, "clock_s": 0},
                {"t_ms": 5_000, "until_ms": 8_000, "obs": "final_screen", "n": 4}]
    late = [{"kind": "haptic", "t_ms": 6_000}, {"kind": "input_activity", "t_ms": 7_000}]
    assert end_menu_delta_corroborated(st, last, timeline, late) is None
    early = [{"kind": "haptic", "t_ms": 2_000}]
    got = end_menu_delta_corroborated(st, last, timeline, early)
    assert got and got["kind"] == "haptic"
    button = [{"kind": "input_activity", "t_ms": 1_500}]
    assert end_menu_delta_corroborated(st, last, timeline, button)["kind"] == "input_activity"
    assert end_menu_delta_corroborated(st, last, timeline, [{"kind": "input_activity", "t_ms": 5_000}]) is None

    score = {"t_ms": 1_000, "home": 17, "away": 0, "quarter": 4, "clock_s": 0, "clock_red": True}
    early_core, _ = _core(score, reader=_MenuReader(0, 20), corr=[{"kind": "haptic", "t_ms": 1_500}])
    _drive(early_core, _menu_frames(5_000, 8_000), 10)
    assert early_core.final["basis"] == "end_menu"
    assert early_core.final["final_confirmed"] is True
    assert (early_core.final["away"], early_core.final["home"]) == (0, 20)

    late_core, _ = _core(score, reader=_MenuReader(0, 20), corr=[{"kind": "input_activity", "t_ms": 6_000},
                                                                 {"kind": "haptic", "t_ms": 7_000}])
    _drive(late_core, _menu_frames(5_000, 8_000), 10)
    assert late_core.final["basis"] == "end_menu"
    assert (late_core.final["away"], late_core.final["home"]) == (0, 20)
    assert late_core.final["final_confirmed"] is False


def test_later_end_menu_score_does_not_replace_a_posted_final():
    """A different end-menu score during the tail keeps the posted score and drops confirmation."""
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _SwitchScore((7, 0), (10, 0), at=9_000)
    core, posts = _core(score, reader=reader, corr=[{"kind": "haptic", "t_ms": 1_500}])
    _drive(core, _menu_frames(5_000, 12_000), 10, reader)
    assert (core.final["away"], core.final["home"]) == (7, 0)
    assert core.final["final_confirmed"] is False
    assert any("not replaced" in n for n in core.final.get("notes") or [])
    assert not any(p.get("home") == 0 and p.get("away") == 10 and p.get("final_confirmed") is True for p in posts)


def test_disputed_final_stays_unconfirmed_after_stop(tmp_path):
    """7-0 menu, then a 10-0 menu, then stop. The re-read must not confirm the first score.

    Live keeps 7-0 with final_confirmed false and the dispute note. ``tail_final_check``
    stops at the first end-menu agreement, so a reader that only sees 7-0 would exact-match
    and upgrade it. Deleting that guard makes this re-read post confirmed=True and drops the note.
    """
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _SwitchScore((7, 0), (10, 0), at=9_000)
    core, _posts = _core(score, reader=reader)
    _drive(core, _menu_frames(5_000, 12_000), 10, reader)
    assert (core.final["away"], core.final["home"]) == (7, 0)
    assert core.final["final_confirmed"] is False
    assert core.final.get("end_menu_disputed") is True
    note = " ".join(n for n in core.final.get("notes") or [] if isinstance(n, str))
    assert "disagreed with the posted" in note and "not replaced" in note

    bundle = tmp_path / "match-dispute"
    bundle.mkdir()
    (bundle / "timeline.jsonl").write_text(json.dumps(score) + "\n", encoding="utf-8")
    (bundle / "video.mp4").write_bytes(b"not-a-real-video")
    (bundle / "manifest.jsonl").write_text(json.dumps({
        "type": "segment", "seq": 0, "file": "video.mp4", "wall_ms": 12_000,
    }) + "\n", encoding="utf-8")
    draft = {"result": qm.apply_final(
        {"home_score": 0, "away_score": 7, "quarter": 4, "winner": "away"}, core.final)}
    assert draft["result"]["final_confirmed"] is False
    assert "not replaced" in " ".join(draft["result"].get("notes") or [])

    def factory(_post):
        # A re-read that only sees the first menu exact-matches 7-0 and would confirm it.
        fresh, _ = _core(score, reader=_MenuReader(7, 0))
        return fresh

    out = qm.tail_final_check(
        bundle, draft, ffmpeg="ffmpeg", core_factory=factory,
        frames=_menu_frames(5_000, 12_000), live_final=core.final)
    res = out["result"]
    assert res["final_confirmed"] is False
    assert (res["away_score"], res["home_score"]) == (7, 0)
    kept = " ".join(n for n in res.get("notes") or [] if isinstance(n, str))
    assert "disagreed with the posted" in kept and "not replaced" in kept


def test_tied_walkoff_input_after_the_end_screen_does_not_confirm(tmp_path):
    """Tied Q4 0:00 10-10 to menu 10-16, and tied OT 13-13 to menu 13-16.

    Input one second into the menu does not confirm. The picture is still labelled
    ``final_screen``. The posted menu starts the tail. An input from before the screen
    still confirms. The stop-time re-read, given only that late input, stays unconfirmed.
    """
    cases = [
        {"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True,
         "menu": (10, 16)},
        {"t_ms": 1_000, "home": 13, "away": 13, "quarter": 5, "clock_s": 240, "clock_red": False,
         "menu": (13, 16)},
    ]
    for score in cases:
        away, home = score["menu"]
        row = {k: score[k] for k in ("t_ms", "home", "away", "quarter", "clock_s", "clock_red")}
        late = [{"kind": "input_activity", "t_ms": 6_000}]
        reader = _MenuReader(away, home)
        core, _posts = _core(row, reader=reader, corr=late)
        reason, _stopped, state, _seen = _drive(core, _menu_frames(5_000, 12_000), 10, reader)
        assert core.stats.get("final_screen", 0) > 0
        assert core.final["basis"] == "end_menu"
        assert (core.final["away"], core.final["home"]) == (away, home)
        assert core.final["final_confirmed"] is False
        assert core.final.get("delta_corroboration") is None
        assert state.get("final_screen_mono") is not None
        assert reason is None

        early, _ = _core(row, reader=_MenuReader(away, home), corr=[{"kind": "input_activity", "t_ms": 1_500}])
        _drive(early, _menu_frames(5_000, 8_000), 10)
        assert early.final["final_confirmed"] is True
        assert early.final["delta_corroboration"]["kind"] == "input_activity"

        bundle = tmp_path / f"match-walkoff-{away}-{home}"
        bundle.mkdir()
        (bundle / "timeline.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        (bundle / "video.mp4").write_bytes(b"not-a-real-video")
        (bundle / "manifest.jsonl").write_text(json.dumps({
            "type": "segment", "seq": 0, "file": "video.mp4", "wall_ms": 12_000,
        }) + "\n", encoding="utf-8")
        draft = {"result": {"home_score": home, "away_score": away, "quarter": row["quarter"],
                            "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}}

        def factory(_post, row=row, away=away, home=home, late=late):
            fresh, _ = _core(row, reader=_MenuReader(away, home), corr=late)
            return fresh

        out = qm.tail_final_check(
            bundle, draft, ffmpeg="ffmpeg", core_factory=factory, frames=_menu_frames(5_000, 12_000))
        res = out["result"]
        assert res["method"] == "end_menu_read"
        assert (res["away_score"], res["home_score"]) == (away, home)
        assert res["final_confirmed"] is False, res


def test_end_screen_cutoff_does_not_need_a_final_screen_label():
    """No ``final_screen`` row. The agreeing menu frame, or the first pixel match, is the cutoff.

    Input at or after that time does not confirm. Input from before the screen still does.
    Reading the cutoff only from ``final_screen`` labels makes the late input confirm.
    """
    last = {"away": 10, "home": 10, "t_ms": 1_000}
    timeline = [{"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0}]
    late = [{"kind": "input_activity", "t_ms": 6_000}]
    early = [{"kind": "input_activity", "t_ms": 1_000}]
    st = {"away": 10, "home": 16, "t_ms": 5_000}
    assert end_menu_delta_corroborated(st, last, timeline, late) is None
    assert end_menu_delta_corroborated(st, last, timeline, [{"kind": "input_activity", "t_ms": 5_000}]) is None
    got = end_menu_delta_corroborated(st, last, timeline, early)
    assert got and got["kind"] == "input_activity"
    st_pixel = {"away": 13, "home": 16}
    assert end_menu_delta_corroborated(st_pixel, {"away": 13, "home": 13, "t_ms": 1_000}, timeline, late,
                                       pixel_match_ms=5_000) is None
    haptic = end_menu_delta_corroborated(st_pixel, {"away": 13, "home": 13, "t_ms": 1_000}, timeline, early,
                                         pixel_match_ms=5_000)
    assert haptic and haptic["kind"] == "input_activity"


# ---------------------------------------------------------------- chain model (tests only)
# Same JCS link as evidence::link. Not used by main().

_SEGMENT_GENESIS = "0" * 64


def _jcs_sha(obj) -> str:
    from qor_receipt.jcs import sha256_hex
    return sha256_hex(obj)


def segment_link(prev: str, seq: int, sha: str) -> str:
    return _jcs_sha({"prev": prev, "seq": seq, "sha256": sha})


def new_segment_chain() -> dict:
    return {"seq": 0, "head": _SEGMENT_GENESIS, "rows": []}


def new_checkpoint_chain() -> dict:
    return {"seq": 0, "prev": _SEGMENT_GENESIS, "rows": []}


def commit_capture_segment(chain: dict, checkpoints: dict, payload: bytes, *, wall_ms: int) -> dict:
    import hashlib

    sha = hashlib.sha256(payload).hexdigest()
    prev = chain["head"]
    seq = chain["seq"]
    head = segment_link(prev, seq, sha)
    row = {"type": "segment", "seq": seq, "sha256": sha, "prev": prev, "chain_head": head,
           "bytes": len(payload), "wall_ms": int(wall_ms), "payload": bytes(payload)}
    chain["rows"].append(row)
    chain["seq"] = seq + 1
    chain["head"] = head
    ext = {"trigger": "segment", "segment_seq": seq, "chain_head": head}
    frames_hash = _jcs_sha(ext)
    input_hash = _jcs_sha([])
    body = {"seq": checkpoints["seq"], "t_ms": int(wall_ms), "frames_hash": frames_hash,
            "input_hash": input_hash, "prev": checkpoints["prev"]}
    digest = _jcs_sha(body)
    cp = dict(body, hash=digest, qorcap=ext)
    checkpoints["rows"].append(cp)
    checkpoints["seq"] += 1
    checkpoints["prev"] = digest
    return row


def verify_capture_chain(segments: list[dict], checkpoints: list[dict]) -> list[str]:
    import hashlib

    errors = []
    prev = _SEGMENT_GENESIS
    heads = {_SEGMENT_GENESIS}
    for i, row in enumerate(segments):
        seq = row.get("seq")
        tag = f"segment {i}"
        if seq != i:
            errors.append(f"{tag}: seq gap (expected {i})")
        if row.get("prev") != prev:
            errors.append(f"{tag}: prev link broken")
        payload = row.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            sha = hashlib.sha256(bytes(payload)).hexdigest()
            if sha != row.get("sha256"):
                errors.append(f"{tag}: SHA-256 mismatch")
        else:
            errors.append(f"{tag}: missing payload")
        claimed_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else -1
        claimed_sha = row.get("sha256") if isinstance(row.get("sha256"), str) else ""
        if row.get("chain_head") != segment_link(prev, claimed_seq, claimed_sha):
            errors.append(f"{tag}: chain head mismatch")
        head = row.get("chain_head")
        if isinstance(head, str):
            heads.add(head)
            prev = head
    cp_prev = _SEGMENT_GENESIS
    empty_input = _jcs_sha([])
    for i, cp in enumerate(checkpoints):
        tag = f"checkpoint {i}"
        if cp.get("seq") != i:
            errors.append(f"{tag}: seq gap")
        if cp.get("prev") != cp_prev:
            errors.append(f"{tag}: prev link broken")
        body = {k: cp.get(k) for k in ("seq", "t_ms", "frames_hash", "input_hash", "prev")}
        if _jcs_sha(body) != cp.get("hash"):
            errors.append(f"{tag}: hash mismatch")
        ext = cp.get("qorcap")
        if not isinstance(ext, dict) or _jcs_sha(ext) != cp.get("frames_hash"):
            errors.append(f"{tag}: qorcap evidence does not match frames_hash")
        elif ext.get("chain_head") not in heads:
            errors.append(f"{tag}: refers to unknown video chain head")
        if cp.get("input_hash") != empty_input:
            errors.append(f"{tag}: input_hash is not the empty chunk")
        got = cp.get("hash")
        cp_prev = got if isinstance(got, str) else cp_prev
    return errors


def run_match_capture(state, *, tail_s, source, chain, checkpoints, clock, sleep, poll_s=None,
                      max_ticks: int = 100000):
    if poll_s is None:
        poll_s = 1.0 / qm.TAIL_HZ
    for _ in range(max_ticks):
        now = clock()
        tick = source(now, state) or {}
        err = tick.get("error")
        if isinstance(err, str) and err:
            state["source_error"] = err
        elif tick.get("alive") is False:
            state["source_error"] = "source"
        else:
            for payload in tick.get("segments") or []:
                commit_capture_segment(chain, checkpoints, payload, wall_ms=int(round(now * 1000)))
        reason = qm.capture_stop_reason(state, now, tail_s=tail_s)
        if reason:
            return reason, now
        sleep(poll_s)
    raise RuntimeError("capture did not stop")
