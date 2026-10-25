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
    end_screen_evidence_cutoff_ms,
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


# The sentence main signs when the later menu disagrees and no scorebug read sits between the two menus.
_NO_SCOREBUG_DISPUTE_NOTE = (
    "later end-of-game menu read AWAY 10 - HOME 0 disagreed with the posted "
    "final AWAY 7 - HOME 0; score not replaced; final_confirmed=false")
_SCOREBUG_DISPUTE_NOTE = (
    "later end-of-game menu read AWAY 10 - HOME 0 disagreed with the posted "
    "final AWAY 7 - HOME 0 after a scorebug read came back between the two menus; "
    "score not replaced; final_confirmed=false")


def test_later_end_menu_score_does_not_replace_a_posted_final():
    """A different end-menu score during the tail keeps the posted score and drops confirmation.

    No scorebug read lies between the two menus. The signed note is the previous sentence.
    It does not claim a scorebug came back.
    """
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _SwitchScore((7, 0), (10, 0), at=9_000)
    core, posts = _core(score, reader=reader, corr=[{"kind": "haptic", "t_ms": 1_500}])
    _drive(core, _menu_frames(5_000, 12_000), 10, reader)
    assert (core.final["away"], core.final["home"]) == (7, 0)
    assert core.final["final_confirmed"] is False
    assert core.final["notes"][0] == _NO_SCOREBUG_DISPUTE_NOTE
    assert "scorebug read came back" not in core.final["notes"][0]
    scores = [r for r in core.timeline if isinstance(r, dict) and "home" in r and "obs" not in r]
    start, later = core.end_menu["until_ms"], 9_000
    assert not any(start < int(r["t_ms"]) < later for r in scores)
    assert not any(p.get("home") == 0 and p.get("away") == 10 and p.get("final_confirmed") is True for p in posts)


def _feed_menu_scorebug_return_then_other_menu():
    """Exact 7-0 menu, then the scorebug comes back, then a different 10-0 menu.

    The haptic is early enough to confirm a fresh +3 if the first menu were forgotten.
    """
    menu_reader = _SwitchScore((7, 0), (10, 0), at=20_000)
    posts = []
    core = ReaderCore(posts.append, _Scorebug(7, 0, 4, 0, True), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=menu_reader,
                      end_menu_agree=EndMenuAgreement())
    core.delta_corroboration = [{"kind": "haptic", "t_ms": 1_500}]
    menu, blank = _menu(), _blank()
    for t in (1_000, 2_000):
        menu_reader.t = t
        core.on_frame(t, blank)
    for t, img in _menu_frames(5_000, 8_000):
        menu_reader.t = t
        core.on_frame(t, img)
    for t in (12_000, 13_000, 14_000):
        menu_reader.t = t
        core.on_frame(t, blank)
    for t, img in _menu_frames(25_000, 28_000):
        menu_reader.t = t
        core.on_frame(t, img)
    return core, posts


def test_scorebug_between_menus_keeps_the_first_score_unconfirmed():
    """A scorebug read between two different end menus does not reopen the final.

    The first score stays. The later score is not adopted. Confirmation stays false.
    Clearing ``end_menu`` when the scorebug returns posts that menu again. Posting the
    later score instead of noting the disagreement confirms 10-0 from the early haptic.
    """
    core, posts = _feed_menu_scorebug_return_then_other_menu()
    assert core.end_menu is not None
    assert (core.end_menu["away"], core.end_menu["home"]) == (7, 0)
    assert (core.final["away"], core.final["home"]) == (7, 0)
    assert core.final["final_confirmed"] is False
    assert core.final.get("end_menu_disputed") is True
    assert "amends" not in core.final
    assert core.final["notes"][0] == _SCOREBUG_DISPUTE_NOTE
    start = core.end_menu["until_ms"]
    scores = [r for r in core.timeline if isinstance(r, dict) and "home" in r and "obs" not in r]
    assert any(start < int(r["t_ms"]) < 25_000 for r in scores)
    # A scorebug row on either bound is not between the menus. The note must not claim one.
    bare = ReaderCore.__new__(ReaderCore)
    bare.end_menu = {"until_ms": start}
    bare.timeline = [{"t_ms": start, "home": 0, "away": 7}]
    assert bare._scorebug_between_menus(25_000) is False
    bare.timeline = [{"t_ms": 25_000, "home": 0, "away": 7}]
    assert bare._scorebug_between_menus(25_000) is False
    bare.timeline = [{"t_ms": start + 1, "home": 0, "away": 7}]
    assert bare._scorebug_between_menus(25_000) is True
    finals = [p for p in posts if p.get("type") == "final"]
    # One confirmed post, then the dispute. A scorebug read must not post the menu again.
    assert len(finals) == 2
    assert finals[0]["final_confirmed"] is True and "amends" not in finals[0]
    assert (finals[0]["away"], finals[0]["home"]) == (7, 0)
    assert finals[1]["final_confirmed"] is False and finals[1].get("end_menu_disputed") is True
    assert not any(
        p.get("away") == 10 and p.get("home") == 0 and p.get("final_confirmed") is True for p in posts)


def _dispute_note(timeline, later_t=25_000):
    """The signed sentence for a 10-0 menu after a posted 7-0, with ``timeline`` between them."""
    bare = ReaderCore.__new__(ReaderCore)
    bare.end_menu = {"until_ms": 8_000, "away": 7, "home": 0}
    bare.final = {"away": 7, "home": 0, "notes": [], "final_confirmed": True,
                  "basis": "end_menu", "method": "end_menu_read"}
    bare.timeline = list(timeline)
    bare.post = lambda *_a, **_k: None
    bare._note_end_menu_disagreement({"away": 10, "home": 0, "t_ms": later_t})
    return bare.final["notes"][0]


def test_a_non_scorebug_row_between_menus_does_not_claim_a_scorebug_read():
    """An observation, or a row with no home score, is not a scorebug read that came back.

    The obs row carries a home score, so dropping the observation check would name a
    scorebug. The other row has no observation and no home score, so dropping the home
    check would name one too.
    """
    obs = {"t_ms": 12_000, "until_ms": 14_000, "obs": "final_screen", "n": 3, "home": 0, "away": 7}
    no_home = {"t_ms": 12_000, "away": 7, "quarter": 4, "clock_s": 0}
    assert _dispute_note([obs]) == _NO_SCOREBUG_DISPUTE_NOTE
    assert _dispute_note([no_home]) == _NO_SCOREBUG_DISPUTE_NOTE
    assert "scorebug read came back" not in _dispute_note([obs, no_home])


class _NoBar:
    """No scorebug on this frame. A flushed hold can still post."""

    def read(self, _img):
        return _Fin(ok=False, reason="layout_unknown", pair=None)


def test_flushed_touchdown_between_menus_is_not_a_scorebug_that_came_back():
    """A held touchdown posted between the menus is not the scorebug coming back.

    The score event stays a normal score. The note keeps the previous sentence.
    Counting that row would claim a scorebug read came back.
    """
    menu_reader = _SwitchScore((7, 0), (10, 0), at=20_000)
    posts = []
    core = ReaderCore(posts.append, _NoBar(), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=menu_reader,
                      end_menu_agree=EndMenuAgreement())
    core.timeline = [{"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}]
    for t, img in _menu_frames(5_000, 8_000):
        menu_reader.t = t
        core.on_frame(t, img)
    assert (core.final["away"], core.final["home"]) == (7, 0)
    assert core.end_menu["until_ms"] == 8_000
    hold = _Fin(ok=True, reason="ok", left_score=7, right_score=6, quarter=4,
                clock_seconds=0, clock_red=True, pair=(7, 6))
    core.touchdown.pending = {
        "cand": (7, 6), "old": (7, 0), "read": hold, "t0": 9_000, "due": 12_000,
        "last_t": 9_000, "confirms": 0, "need": 0, "confusable": False,
        "min_margin": 0.15, "id": "td-between", "verdict": None,
    }
    core.on_frame(12_000, _blank())
    posted = [p for p in posts if p.get("type") == "score" and p.get("t_ms") == 12_000]
    assert len(posted) == 1
    assert posted[0]["home"] == 6 and posted[0]["away"] == 7
    assert "touchdown_hold" not in posted[0]
    held = [r for r in core.timeline if r.get("t_ms") == 12_000 and r.get("home") == 6]
    assert len(held) == 1 and held[0].get("touchdown_hold") is True
    for t, img in _menu_frames(25_000, 28_000):
        menu_reader.t = t
        core.on_frame(t, img)
    assert (core.final["away"], core.final["home"]) == (7, 0)
    assert core.final["final_confirmed"] is False
    assert core.final["notes"][0] == _NO_SCOREBUG_DISPUTE_NOTE
    assert "scorebug read came back" not in core.final["notes"][0]


def test_disputed_final_stays_unconfirmed_after_stop(tmp_path):
    """7-0 menu, then a 10-0 menu, then stop. The re-read must not confirm the first score.

    Live keeps 7-0 with final_confirmed false and the dispute note. A disputed live final
    is not re-read. A reader that only sees 7-0 would exact-match and upgrade it. Deleting
    that guard makes this re-read post confirmed=True and drops the note.
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
    # The flag and the note are one dispute. Either one is enough, and the sentence the
    # reader writes is the sentence the matcher looks for.
    sentence = next(n for n in core.final["notes"] if "not replaced" in n)
    assert qm.end_menu_was_disputed({"basis": "end_menu", "end_menu_disputed": True, "notes": []}) is True
    assert qm.end_menu_was_disputed({"method": "end_menu_read", "notes": [sentence]}) is True
    assert qm.end_menu_was_disputed(
        {"basis": "end_menu", "final_confirmed": False, "notes": ["final_confirmed=false"]}) is False
    # The same flag and sentence on a scorebug final are not a dispute.
    assert qm.end_menu_was_disputed(
        {"basis": "clock_zero", "end_menu_disputed": True, "notes": [sentence]}) is False

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


def _missed_tail(tmp_path, name, frames, reader, scorebug=None):
    """Stop-time re-read with no live final. Frame time is the manifest clock."""
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    bundle = tmp_path / name
    bundle.mkdir()
    (bundle / "timeline.jsonl").write_text(json.dumps(score) + "\n", encoding="utf-8")
    (bundle / "video.mp4").write_bytes(b"not-a-real-video")
    (bundle / "manifest.jsonl").write_text(json.dumps({
        "type": "segment", "seq": 0, "file": "video.mp4", "wall_ms": frames[-1][0],
    }) + "\n", encoding="utf-8")
    draft = {"result": {"home_score": 0, "away_score": 7, "quarter": 4, "winner": "away",
                        "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}}

    def factory(post, reader=reader, scorebug=scorebug):
        agree = _Agree() if scorebug is not None else None
        pause = None if scorebug is not None else _Paused()
        core = ReaderCore(post, scorebug, agree, pause, _detect_final,
                          final_screen_detector=madden_final_screen, end_menu_reader=reader,
                          end_menu_agree=EndMenuAgreement())
        real = core.on_frame

        def on_frame(t, img, reader=reader, real=real):
            if hasattr(reader, "t"):
                reader.t = t
            return real(t, img)

        core.on_frame = on_frame
        return core

    got = qm.tail_final_check(
        bundle, draft, ffmpeg="ffmpeg", core_factory=factory, frames=frames)
    tail = json.loads((bundle / "final.tail.json").read_text(encoding="utf-8"))
    return got, tail


def _two_menus(gap_img):
    """7-0 menu, a gap, then another menu. The gap image is blank or a scorebug frame."""
    return (_menu_frames(5_000, 8_000)
            + [(t, gap_img) for t in (12_000, 13_000, 14_000)]
            + _menu_frames(25_000, 28_000))


def test_recorded_menus_that_disagree_stay_unconfirmed_when_live_missed_the_final(tmp_path):
    """No live final. Recorded menus read 7-0 and then 10-0. The signed score stays 7-0, unconfirmed.

    The note is the one live writes. A scorebug row between the menus adds the came-back
    phrase. Without one, the note is the previous sentence. Stopping at the first agreement
    signs 7-0 confirmed. The look-back stays ``TAIL_SECS``.
    """
    assert qm.TAIL_SECS == 240
    frames = _two_menus(_blank())
    got, tail = _missed_tail(tmp_path, "missed-disagree", frames, _SwitchScore((7, 0), (10, 0), at=20_000))
    fin, res = tail["final"], got["result"]
    assert (res["away_score"], res["home_score"]) == (7, 0)
    assert res["final_confirmed"] is False
    assert tail["frames"]["frames"] == len(frames)
    assert res["method"] == "end_menu_read"
    assert res["notes"][0] == _NO_SCOREBUG_DISPUTE_NOTE
    assert fin["final_confirmed"] is False and fin.get("end_menu_disputed") is True
    assert fin["notes"][0] == _NO_SCOREBUG_DISPUTE_NOTE
    assert tail["end_menu"]["until_ms"] == 8_000

    bug = _two_menus(_blank())
    got_b, back = _missed_tail(tmp_path, "missed-scorebug", bug, _SwitchScore((7, 0), (10, 0), at=20_000),
                               scorebug=_Scorebug(7, 0, 4, 0, True))
    assert back["frames"]["frames"] == len(bug)
    assert got_b["result"]["final_confirmed"] is False
    assert (got_b["result"]["away_score"], got_b["result"]["home_score"]) == (7, 0)
    assert got_b["result"]["notes"][0] == _SCOREBUG_DISPUTE_NOTE
    assert back["final"].get("end_menu_disputed") is True
    assert "scorebug read came back" in back["final"]["notes"][0]


def test_two_agreeing_recorded_menus_still_confirm(tmp_path):
    """Two showings of the same menu still confirm. The second showing is read.

    A gap that is not a scorebug read does not dispute the first score. Stopping at the
    first agreement would leave ``until_ms`` on the first showing.
    """
    frames = _two_menus(_blank())
    got, tail = _missed_tail(tmp_path, "two-agree", frames, _MenuReader(7, 0))
    res = got["result"]
    assert tail["frames"]["frames"] == len(frames)
    assert (res["away_score"], res["home_score"]) == (7, 0)
    assert res["final_confirmed"] is True
    assert res["method"] == "end_menu_read"
    assert tail["final"].get("end_menu_disputed") is not True
    assert tail["end_menu"]["until_ms"] == 28_000


def test_menu_then_gameplay_then_the_same_menu_still_confirms(tmp_path):
    """A scorebug read between two showings of the same menu does not drop the confirmation.

    The gameplay score is a different pair. The later menu agrees with the first menu.
    The first score stays confirmed.
    """
    frames = _two_menus(_blank())
    got, tail = _missed_tail(tmp_path, "gameplay-agrees", frames, _MenuReader(7, 0),
                             scorebug=_Scorebug(10, 0, 4, 0, True))
    res = got["result"]
    assert tail["frames"]["frames"] == len(frames)
    assert (res["away_score"], res["home_score"]) == (7, 0)
    assert res["final_confirmed"] is True
    assert tail["final"].get("end_menu_disputed") is not True
    assert tail["end_menu"]["until_ms"] == 28_000
    assert "scorebug read came back" not in " ".join(res.get("notes") or [])


def test_one_recorded_menu_signs_the_same_result_to_the_end_of_the_tail(tmp_path):
    """One menu, read to the end, signs the same result as stopping when it first agrees.

    ``finalize`` and ``--recover`` never call ``tail_final_check``. They already feed every
    frame through ``run_offline_reader``, so a single menu's offline receipt does not change.
    """
    import inspect
    assert "tail_final_check" not in inspect.getsource(qm.finalize)
    assert "tail_final_check" not in inspect.getsource(qm.recover_bundle)
    short_got, short = _missed_tail(tmp_path, "one-menu-short", _menu_frames(5_000, 6_000), _MenuReader(7, 0))
    long_got, long = _missed_tail(tmp_path, "one-menu-long", _menu_frames(5_000, 12_000), _MenuReader(7, 0))
    assert short_got["result"]["final_confirmed"] is True
    assert long["frames"]["frames"] > short["frames"]["frames"]
    assert json.dumps(long_got["result"], sort_keys=True) == json.dumps(short_got["result"], sort_keys=True)
    assert long["final"]["away"] == short["final"]["away"] == 7
    assert long["final"]["home"] == short["final"]["home"] == 0
    assert long["final"]["final_confirmed"] is True and short["final"]["final_confirmed"] is True
    assert long["final"].get("notes") == short["final"].get("notes")
    assert long["final"].get("end_menu_disputed") is None and short["final"].get("end_menu_disputed") is None


def test_reader_core_names_its_quarter_floor():
    """The tail seeds this attribute by name. A rename fails here instead of skipping the seed."""
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    core, _posts = _core(score)
    assert core._max_quarter is None


class _Q3MisreadThenQ4:
    """Q3 on the frame before the menu, then a Q4 scorebug once the menu has gone."""

    def __init__(self):
        self.t = 0

    def read(self, _img):
        if self.t < 10_000:
            q, clock, red = 3, 30, False
        elif 12_000 <= self.t <= 14_000:
            q, clock, red = 4, 0, True
        else:
            return _Fin(ok=False, reason="layout_unknown", pair=None, quarter=None,
                        clock_seconds=None, clock_red=False)
        return _Fin(ok=True, reason="ok", left_score=10, right_score=10, quarter=q,
                    clock_seconds=clock, clock_red=red, pair=(10, 10))


def test_trailing_q3_misread_does_not_let_a_later_showing_confirm(tmp_path):
    """Live already reached Q4. The tail's Q3 misread is dropped, so the first menu posts unconfirmed.

    Bool, None, and a non-int quarter on the live rows are not a floor. Input after the first
    screen does not confirm it. A scorebug return and a later showing of the same menu leave
    that first score unconfirmed. Without the live quarter floor the Q3 read is kept, the Q4
    gate rejects the first showing, and the later showing confirms.
    """
    rows = [
        {"t_ms": 100, "home": 10, "away": 10, "quarter": True, "clock_s": 60},
        {"t_ms": 200, "home": 10, "away": 10, "quarter": None, "clock_s": 50},
        {"t_ms": 300, "home": 10, "away": 10, "quarter": "4", "clock_s": 40},
        {"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True},
    ]
    frames = ([(2_000, _blank())]
              + _menu_frames(5_000, 8_000)
              + [(t, _blank()) for t in (12_000, 13_000, 14_000)]
              + _menu_frames(20_000, 23_000))
    bundle = tmp_path / "trailing-q3"
    bundle.mkdir()
    (bundle / "timeline.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (bundle / "video.mp4").write_bytes(b"not-a-real-video")
    (bundle / "manifest.jsonl").write_text(json.dumps({
        "type": "segment", "seq": 0, "file": "video.mp4", "wall_ms": frames[-1][0],
    }) + "\n", encoding="utf-8")
    draft = {"result": {"home_score": 10, "away_score": 10, "quarter": 4, "winner": "unknown",
                        "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}}
    scorebug = _Q3MisreadThenQ4()
    menu = _MenuReader(10, 16)

    def factory(post, scorebug=scorebug, menu=menu):
        core = ReaderCore(post, scorebug, _Agree(), None, _detect_final,
                          final_screen_detector=madden_final_screen, end_menu_reader=menu,
                          end_menu_agree=EndMenuAgreement())
        core.delta_corroboration = [{"kind": "input_activity", "t_ms": 16_000}]
        real = core.on_frame

        def on_frame(t, img, scorebug=scorebug, real=real):
            scorebug.t = t
            return real(t, img)

        core.on_frame = on_frame
        return core

    got = qm.tail_final_check(
        bundle, draft, ffmpeg="ffmpeg", core_factory=factory, frames=frames)
    tail = json.loads((bundle / "final.tail.json").read_text(encoding="utf-8"))
    res, fin = got["result"], tail["final"]
    assert (res["away_score"], res["home_score"]) == (10, 16)
    assert res["final_confirmed"] is False
    assert res["method"] == "end_menu_read"
    assert res["final_t_ms"] == 5_000
    assert fin["t_ms"] == 5_000 and fin["final_confirmed"] is False
    assert fin.get("end_menu_disputed") is not True
    assert tail["end_menu"]["t_ms"] == 5_000
    assert tail["end_menu"]["until_ms"] == 23_000


def _quarter_rows(quarters):
    """Live score rows in this order. The last row is the latest ``t_ms``."""
    rows = []
    last_i = len(quarters) - 1
    for i, q in enumerate(quarters):
        last = i == last_i
        clock = 0 if last and isinstance(q, int) and not isinstance(q, bool) and q >= 4 else 40 - i
        row = {"t_ms": 1_000 if last else 100 * (i + 1), "home": 10, "away": 10,
               "quarter": q, "clock_s": clock}
        if clock == 0:
            row["clock_red"] = True
        rows.append(row)
    return rows


def _q3_then_later_menu():
    """One Q3 frame, the first menu, a scorebug return, then a later showing."""
    return ([(2_000, _blank())]
            + _menu_frames(5_000, 8_000)
            + [(t, _blank()) for t in (12_000, 13_000, 14_000)]
            + _menu_frames(20_000, 23_000))


def _bundle_tail(tmp_path, name, rows, frames, *, menu=None, scorebug=None, corr=None,
                 live_final=None, draft=None):
    """Stop-time re-read. The returned core is the one ``tail_final_check`` seeded."""
    bundle = tmp_path / name
    bundle.mkdir()
    (bundle / "timeline.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (bundle / "video.mp4").write_bytes(b"not-a-real-video")
    (bundle / "manifest.jsonl").write_text(json.dumps({
        "type": "segment", "seq": 0, "file": "video.mp4", "wall_ms": frames[-1][0],
    }) + "\n", encoding="utf-8")
    if draft is None:
        draft = {"result": {"home_score": 10, "away_score": 10, "quarter": 4, "winner": "unknown",
                            "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}}
    held = []

    def factory(post, menu=menu, scorebug=scorebug, corr=corr):
        agree = _Agree() if scorebug is not None else None
        pause = None if scorebug is not None else _Paused()
        core = ReaderCore(post, scorebug, agree, pause, _detect_final,
                          final_screen_detector=madden_final_screen, end_menu_reader=menu,
                          end_menu_agree=EndMenuAgreement() if menu is not None else None)
        if corr:
            core.delta_corroboration = list(corr)
        held.append(core)
        if scorebug is None:
            return core
        real = core.on_frame

        def on_frame(t, img, scorebug=scorebug, real=real):
            scorebug.t = t
            return real(t, img)

        core.on_frame = on_frame
        return core

    got = qm.tail_final_check(
        bundle, draft, ffmpeg="ffmpeg", core_factory=factory, frames=frames, live_final=live_final)
    tail_path = bundle / "final.tail.json"
    tail = json.loads(tail_path.read_text(encoding="utf-8")) if tail_path.is_file() else None
    return got, tail, held[0]


def test_tail_keeps_an_end_screen_from_before_the_last_live_row(tmp_path):
    """Live saw an end screen, the scorebug returned, and live missed the later menu.

    The first screen is in the decoded window but before the last live row, so the tail
    does not read it again. Input during that screen must not confirm the later menu.
    Dropping the ``_note_end_screen_time`` seed signs the later menu confirmed.
    """
    rows = [
        {"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 30, "clock_red": False},
        {"t_ms": 12_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True},
    ]
    frames = _menu_frames(5_000, 8_000) + _menu_frames(20_000, 23_000)
    got, tail, core = _bundle_tail(
        tmp_path, "early-screen", rows, frames, menu=_MenuReader(10, 16),
        corr=[{"kind": "input_activity", "t_ms": 6_000}])
    res = got["result"]
    assert (res["away_score"], res["home_score"]) == (10, 16)
    assert res["final_confirmed"] is False
    assert core._end_screen_seen_ms == 5_000
    assert res["method"] == "end_menu_read"
    assert res["final_t_ms"] == 20_000
    assert tail["final"]["t_ms"] == 20_000 and tail["final"]["final_confirmed"] is False


def test_quarter_floor_is_the_max_across_1_4_3(tmp_path):
    """Quarters 1, then 4, then 3. The floor is 4, so the trailing Q3 misread is not posted.

    The last live row is itself Q3, so the menu gate is not the signal. The minimum of
    these quarters is 1 and the last row is 3. Either mutant posts the misread and
    leaves ``_max_quarter`` below 4.
    """
    got, _tail, core = _bundle_tail(
        tmp_path, "quarters-1-4-3", _quarter_rows([1, 4, 3]), _q3_then_later_menu(),
        menu=_MenuReader(10, 16), scorebug=_Q3MisreadThenQ4(),
        corr=[{"kind": "input_activity", "t_ms": 16_000}])
    assert core._max_quarter == 4
    assert not any(isinstance(r, dict) and r.get("t_ms") == 2_000 and r.get("quarter") == 3
                   for r in core.timeline)
    assert got["result"]["method"] == "end_menu_read"


def test_quarter_floor_is_the_max_across_4_2_4(tmp_path):
    """Quarters 4, then 2, then 4. The floor is 4, so the first menu stays unconfirmed.

    The minimum of these quarters is 2. That mutant keeps the trailing Q3, the Q4 gate
    rejects the first showing, and the later showing confirms. The last row is already 4.
    """
    got, tail, core = _bundle_tail(
        tmp_path, "quarters-4-2-4", _quarter_rows([4, 2, 4]), _q3_then_later_menu(),
        menu=_MenuReader(10, 16), scorebug=_Q3MisreadThenQ4(),
        corr=[{"kind": "input_activity", "t_ms": 16_000}])
    res, fin = got["result"], tail["final"]
    assert core._max_quarter == 4
    assert (res["away_score"], res["home_score"]) == (10, 16)
    assert res["final_confirmed"] is False
    assert res["final_t_ms"] == 5_000
    assert fin["t_ms"] == 5_000 and fin["final_confirmed"] is False
    assert tail["end_menu"]["t_ms"] == 5_000


def test_quarter_below_one_leaves_the_floor_unset(tmp_path):
    """Zero and negative are not a quarter. The floor stays None, the same as live.

    A 4 among them is still the floor. Taking the max without the ``q >= 1`` check, or
    keeping the last row, sets 0 or a negative instead.
    """
    frames = [(2_000, _blank())]
    _got, _tail, none_core = _bundle_tail(
        tmp_path, "quarter-none", _quarter_rows([0, -3]), frames)
    assert none_core._max_quarter is None
    _got, _tail, kept = _bundle_tail(
        tmp_path, "quarter-kept", _quarter_rows([4, 0, -1]), frames)
    assert kept._max_quarter == 4


def _unconfirmed_live_final():
    return {
        "type": "final", "basis": "end_menu", "method": "end_menu_read",
        "home": 16, "away": 10, "quarter": 4, "t_ms": 5_000, "final_confirmed": False,
        "end_menu": {"away": 10, "home": 16, "t_ms": 5_000, "until_ms": 8_000, "frames": 7},
    }


def test_scorebug_after_an_unconfirmed_final_is_not_upgraded(tmp_path):
    """Live posted an unconfirmed menu and a scorebug read returned after it.

    Input after that return would confirm a fresh read of the later menu. The tail
    leaves the final unconfirmed. A flushed touchdown hold, or a scorebug at the
    menu's last millisecond, is not that return, so the same input still confirms.
    """
    live_final = _unconfirmed_live_final()
    base = {"home_score": 10, "away_score": 10, "quarter": 4, "winner": "unknown",
            "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}
    draft = {"result": qm.apply_final(dict(base), live_final)}
    assert draft["result"]["final_confirmed"] is False
    frames = _menu_frames(5_000, 8_000) + _menu_frames(20_000, 23_000)
    corr = [{"kind": "input_activity", "t_ms": 16_000}]
    early = {"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True}

    def run(name, later):
        return _bundle_tail(
            tmp_path, name, [early, later], frames, menu=_MenuReader(10, 16),
            corr=corr, live_final=live_final, draft=draft)

    got, tail, _core = run("after-scorebug", {
        "t_ms": 12_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True})
    assert got["result"]["final_confirmed"] is False
    assert (got["result"]["away_score"], got["result"]["home_score"]) == (10, 16)
    assert got["result"]["method"] == "end_menu_read"
    assert tail["final"]["final_confirmed"] is False

    held, _tail, _core = run("after-hold", {
        "t_ms": 12_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True,
        "touchdown_hold": True})
    assert held["result"]["final_confirmed"] is True

    same_ms, _tail, _core = run("at-until", {
        "t_ms": 8_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True})
    assert same_ms["result"]["final_confirmed"] is True


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


def _blank():
    return np.full((360, 640, 3), 40, np.uint8)


def _feed_end_screen_gap(input_t):
    """Unreadable end screen under 3 s, one non-matching frame, then a readable 10-16 menu.

    The short labelled span is dropped. The cutoff has to be the first match, not the run after the gap.
    """
    score = {"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _LateDigits(10, 16, ready_at=7_000)
    core, posts = _core(score, reader=reader, corr=[{"kind": "input_activity", "t_ms": input_t}])
    menu, blank = _menu(), _blank()
    assert madden_final_screen(menu) and not madden_final_screen(blank)
    for t, img in ((5_000, menu), (5_500, menu), (6_000, menu), (6_500, blank),
                   (7_000, menu), (7_500, menu), (8_000, menu), (8_500, menu)):
        reader.t = t
        core.on_frame(t, img)
    return core, posts


def test_end_screen_gap_does_not_let_the_first_stretch_confirm():
    """Input during the first, unreadable stretch does not confirm the walk-off.

    Resetting the earliest match on the non-matching frame, or dropping ``pixel_match_ms``
    at the live call, lets this input confirm. Input from before the screen still does.
    """
    core, _posts = _feed_end_screen_gap(5_200)
    assert core._end_screen_earliest == 5_000
    assert not any(isinstance(r, dict) and r.get("obs") == "final_screen" and r.get("t_ms") == 5_000
                   for r in core.timeline)
    assert core.final["basis"] == "end_menu"
    assert (core.final["away"], core.final["home"]) == (10, 16)
    assert core.final["final_confirmed"] is False
    assert core.final.get("delta_corroboration") is None

    early, _ = _feed_end_screen_gap(1_500)
    assert early.final["final_confirmed"] is True
    assert early.final["delta_corroboration"]["kind"] == "input_activity"


def test_live_call_passes_the_earliest_end_screen_match(monkeypatch):
    """The live corroboration call passes the first end-screen match, not only the current run.

    Omitting ``pixel_match_ms`` at that call leaves this as the missing sentinel.
    """
    import qorcap_reader as qr

    missing = object()
    seen = {}
    real = qr.end_menu_delta_corroborated

    def wrap(st, last, timeline, external, pixel_match_ms=missing, **rest):
        seen["pixel_match_ms"] = pixel_match_ms
        if pixel_match_ms is missing:
            return real(st, last, timeline, external, **rest)
        return real(st, last, timeline, external, pixel_match_ms=pixel_match_ms, **rest)

    monkeypatch.setattr(qr, "end_menu_delta_corroborated", wrap)
    core, _posts = _feed_end_screen_gap(5_200)
    assert seen.get("pixel_match_ms") == 5_000
    assert core.final["final_confirmed"] is False


class _Agree:
    def offer(self, fr, **_):
        return _Fin(agreed=bool(getattr(fr, "ok", False)), read=fr if getattr(fr, "ok", False) else None)


class _Scorebug:
    """One agreed scorebug read. ``left`` is away, ``right`` is home."""

    def __init__(self, away, home, quarter, clock_s, clock_red=False):
        self.away, self.home, self.quarter = away, home, quarter
        self.clock_s, self.clock_red = clock_s, clock_red

    def read(self, _img):
        return _Fin(ok=True, reason="ok", left_score=self.away, right_score=self.home,
                    quarter=self.quarter, clock_seconds=self.clock_s, clock_red=self.clock_red,
                    pair=(self.away, self.home))


def _feed_flash_then_scorebug_then_menu(input_t):
    """Pause-like end-screen flash, then real scorebug reads, then the real end menu."""
    menu_reader = _LateDigits(10, 16, ready_at=20_000)
    posts = []
    core = ReaderCore(posts.append, _Scorebug(10, 10, 4, 0, True), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=menu_reader,
                      end_menu_agree=EndMenuAgreement())
    core.timeline = [{"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 30, "clock_red": False}]
    core.delta_corroboration = [{"kind": "input_activity", "t_ms": input_t}]
    menu, blank = _menu(), _blank()
    for t in (2_000, 2_500, 3_000):
        menu_reader.t = t
        core.on_frame(t, menu)
    assert core._end_screen_earliest == 2_000
    assert not any(isinstance(r, dict) and r.get("obs") == "final_screen" for r in core.timeline)
    for t in (8_000, 9_000, 10_000):
        menu_reader.t = t
        core.on_frame(t, blank)
    scores = [p for p in posts if p.get("type") == "score"]
    assert scores and scores[-1]["clock_s"] == 0 and (scores[-1]["away"], scores[-1]["home"]) == (10, 10)
    assert core._end_screen_earliest is None
    for t, img in _menu_frames(20_000, 22_000):
        menu_reader.t = t
        core.on_frame(t, img)
    return core


def test_scorebug_return_clears_the_earliest_end_screen_match():
    """A pause-like flash does not keep the cutoff once a real scorebug read comes back.

    Input at 12 s, after that flash and before the real menu, still confirms the one-step
    walk-off. Input after the real end screen does not. Deleting the clear in
    ``_post_agreed`` leaves the flash time in place, so the 12 s input no longer confirms.
    """
    back = _feed_flash_then_scorebug_then_menu(12_000)
    assert back.final["basis"] == "end_menu"
    assert (back.final["away"], back.final["home"]) == (10, 16)
    assert back.final["final_confirmed"] is True
    assert back.final["delta_corroboration"]["kind"] == "input_activity"
    assert back.final["delta_corroboration"]["t_ms"] == 12_000

    after = _feed_flash_then_scorebug_then_menu(21_000)
    assert after.final["basis"] == "end_menu"
    assert (after.final["away"], after.final["home"]) == (10, 16)
    assert after.final["final_confirmed"] is False
    assert after.final.get("delta_corroboration") is None


def _feed_labelled_screen_then_scorebug_then_menu(input_t, corr=None):
    """A committed end screen, then the scorebug comes back, then the real walk-off menu."""
    menu_reader = _LateDigits(10, 16, ready_at=20_000)
    posts = []
    core = ReaderCore(posts.append, _Scorebug(10, 10, 4, 0, True), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=menu_reader,
                      end_menu_agree=EndMenuAgreement())
    core.timeline = [{"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True}]
    core.delta_corroboration = list(corr) if corr is not None else [{"kind": "input_activity", "t_ms": input_t}]
    menu = _menu()
    for t in (2_000, 3_200, 4_400, 5_600, 6_800):
        menu_reader.t = t
        core.on_frame(t, menu)
    for t in (8_000, 9_000, 10_000):
        menu_reader.t = t
        core.on_frame(t, _blank())
    for t, img in _menu_frames(20_000, 22_000):
        menu_reader.t = t
        core.on_frame(t, img)
    return core


def test_cutoff_ignores_end_screens_from_before_the_scorebug_returned():
    """A labelled end screen from before the scorebug came back is not the cutoff.

    Input at 12 s, after that screen and after the scorebug read, still confirms the
    one-step walk-off. Input after the real menu does not. Counting every ``final_screen``
    row again makes the 12 s input miss. With no scorebug row, the old screen still counts.
    """
    old = [{"t_ms": 2_000, "until_ms": 6_800, "obs": "final_screen", "n": 5},
           {"t_ms": 10_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0}]
    assert end_screen_evidence_cutoff_ms(old, menu_t_ms=20_000, pixel_match_ms=2_000) == 20_000
    assert end_screen_evidence_cutoff_ms(
        [{"t_ms": 2_000, "obs": "final_screen", "n": 5}], menu_t_ms=20_000, pixel_match_ms=2_000) == 2_000
    st = {"away": 10, "home": 16, "t_ms": 20_000}
    last = {"away": 10, "home": 10, "t_ms": 10_000}
    got = end_menu_delta_corroborated(
        st, last, old, [{"kind": "input_activity", "t_ms": 12_000}], pixel_match_ms=20_000)
    assert got and got["kind"] == "input_activity" and got["t_ms"] == 12_000
    assert end_menu_delta_corroborated(
        st, last, old, [{"kind": "input_activity", "t_ms": 21_000}], pixel_match_ms=20_000) is None

    back = _feed_labelled_screen_then_scorebug_then_menu(12_000)
    screens = [r for r in back.timeline if isinstance(r, dict) and r.get("obs") == "final_screen"]
    scores = [r for r in back.timeline if isinstance(r, dict) and "home" in r and "obs" not in r]
    assert screens and int(screens[0]["t_ms"]) < int(scores[-1]["t_ms"])
    assert scores[-1]["t_ms"] >= 10_000
    assert back.final["basis"] == "end_menu"
    assert (back.final["away"], back.final["home"]) == (10, 16)
    assert back.final["final_confirmed"] is True
    assert back.final["delta_corroboration"]["kind"] == "input_activity"
    assert back.final["delta_corroboration"]["t_ms"] == 12_000

    after = _feed_labelled_screen_then_scorebug_then_menu(21_000)
    assert after.final["final_confirmed"] is False
    assert after.final.get("delta_corroboration") is None


def test_evidence_at_the_window_edges_does_not_count():
    """Evidence at the first end-screen time, or at the latest scorebug read, does not count.

    The closed window is ``first <= t <= scorebug_t``. An input one millisecond after that
    scorebug read, and before the next screen, still counts. Opening either edge would
    confirm the walk-off from the input that shares that millisecond.
    """
    first, scorebug_t, menu_t = 5_000, 10_000, 20_000
    timeline = [
        {"t_ms": first, "until_ms": 6_800, "obs": "final_screen", "n": 5},
        {"t_ms": scorebug_t, "home": 10, "away": 10, "quarter": 4, "clock_s": 0},
    ]
    st = {"away": 10, "home": 16, "t_ms": menu_t}
    last = {"away": 10, "home": 10, "t_ms": scorebug_t}
    assert end_screen_evidence_cutoff_ms(timeline, menu_t_ms=menu_t) == menu_t

    def at(t, kind="input_activity", **extra):
        return end_menu_delta_corroborated(
            st, last, timeline, [{"kind": kind, "t_ms": t, **extra}])

    assert at(first) is None
    assert at(scorebug_t) is None
    assert at(first, "haptic") is None
    assert at(scorebug_t, "score_change_clip", sha256="a" * 64) is None
    got = at(scorebug_t + 1)
    assert got and got["kind"] == "input_activity" and got["t_ms"] == scorebug_t + 1


def test_offline_finalize_does_not_upgrade_a_disputed_end_menu():
    """The first end-menu event is confirmed. The dispute post is later. Finalize signs the dispute.

    Taking the first end-menu final again confirms 7-0 and drops the note.
    """
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    reader = _SwitchScore((7, 0), (10, 0), at=9_000)
    core, posts = _core(score, reader=reader)
    for t, img in _menu_frames(5_000, 12_000):
        reader.t = t
        core.on_frame(t, img)
    finals = [p for p in posts if p.get("type") == "final"]
    assert len(finals) >= 2
    assert finals[0]["final_confirmed"] is True and finals[0].get("end_menu_disputed") is not True
    assert finals[-1].get("end_menu_disputed") is True and finals[-1]["final_confirmed"] is False
    fin, res = qm.apply_offline_final(
        {"home_score": 0, "away_score": 7, "quarter": 4, "winner": "away"}, posts)
    assert fin is not None and fin.get("end_menu_disputed") is True
    assert res["final_confirmed"] is False
    assert (res["away_score"], res["home_score"]) == (7, 0)
    # Same no-scorebug drive as the live test. Finalize copies this note into the receipt.
    assert res["notes"][0] == _NO_SCOREBUG_DISPUTE_NOTE
    assert fin["notes"][0] == _NO_SCOREBUG_DISPUTE_NOTE


def test_note_only_end_menu_dispute_is_not_confirmed_offline():
    """The dispute note is enough. ``apply_final`` treats a missing ``final_confirmed`` as True.

    Skipping the rewrite in ``apply_offline_final`` signs this confirmed. A scorebug final
    that carries the same note and flag is not a dispute, and an unresolved grammar abstains.
    """
    note = ("later end-of-game menu read AWAY 10 - HOME 0 disagreed with the posted "
            "final AWAY 7 - HOME 0; score not replaced; final_confirmed=false")
    event = {"type": "final", "basis": "end_menu", "method": "end_menu_read",
             "home": 0, "away": 7, "quarter": 4, "t_ms": 5_000, "notes": [note]}
    fin, res = qm.apply_offline_final(
        {"home_score": 0, "away_score": 7, "quarter": 4, "winner": "away"}, [event])
    assert fin is event
    assert res["final_confirmed"] is False
    assert (res["away_score"], res["home_score"]) == (7, 0)
    assert res["method"] == "end_menu_read"
    kept = " ".join(n for n in res.get("notes") or [] if isinstance(n, str))
    assert "disagreed with the posted" in kept and "not replaced" in kept

    bug = {"type": "final", "basis": "clock_zero", "home": 0, "away": 7, "quarter": 4,
           "t_ms": 5_000, "notes": [note], "end_menu_disputed": True}
    assert qm.end_menu_was_disputed(bug) is False
    _fin, abstained = qm.apply_offline_final(
        {"home_score": 0, "away_score": 7, "quarter": 4, "winner": "away"}, [bug], grammar_unresolved=True)
    assert abstained["away_score"] is None and abstained["home_score"] is None
    assert abstained["winner"] == "unknown"
    assert abstained["method"] == "last_scorebug_read_unconfirmed"
    assert any("abstains" in n for n in abstained.get("notes") or [])
    forced = qm._disputed_final_result({"result": {}}, bug, grammar_unresolved=True)["result"]
    assert forced["away_score"] is None and forced["home_score"] is None
    assert forced["method"] == "last_scorebug_read_unconfirmed"
    assert any("abstains" in n for n in forced.get("notes") or [])


def test_cutoff_counts_a_scorebug_that_shares_the_screen_time():
    """A scorebug read at the screen's millisecond is still the cutoff. It is not discarded.

    Excluding that time leaves no candidate. The fallback is the earliest screen, and it is
    not None. This is the case a held touchdown posts on the menu frame.
    """
    tl = [{"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0},
          {"t_ms": 5_000, "home": 10, "away": 16, "quarter": 4, "clock_s": 0}]
    assert end_screen_evidence_cutoff_ms(tl, menu_t_ms=4_500, pixel_match_ms=5_000) == 5_000
    # Every candidate is earlier than the scorebug read, including a timestamp that went backwards.
    back = [{"t_ms": 9_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0}]
    assert end_screen_evidence_cutoff_ms(back, menu_t_ms=2_000, pixel_match_ms=3_000) == 2_000


class _Bar:
    """One scorebug read. ``accepts_time`` so a stop-time re-read sees the frame clock."""

    accepts_time = True

    def __init__(self, script):
        self.script = script

    def read(self, _img, t_ms=0):
        from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead
        spec = self.script(t_ms)
        if spec is None:
            return FrameRead(ok=False, reason="layout_unknown", profile_id="qorcap-test")
        away, home, quarter, clock = spec
        return FrameRead(ok=True, reason="ok", profile_id="qorcap-test", left_score=away, right_score=home,
                         quarter=quarter, clock_seconds=clock, clock_red=clock == 0, score_margin=0.4,
                         score_conf=0.9, quarter_conf=0.9)


class _AgreePair:
    def __init__(self, pair):
        self.pair = pair

    def offer(self, fr, **_):
        if getattr(fr, "ok", False) and getattr(fr, "pair", None) == self.pair:
            return _Fin(agreed=True, read=fr)
        return _Fin(agreed=False, read=None)


class _QueuedMenu:
    def __init__(self, reads):
        self.reads = list(reads)
        self.i = 0

    def read(self, _img):
        if self.i >= len(self.reads):
            return self.reads[-1]
        rd = self.reads[self.i]
        self.i += 1
        return rd


def _td_flush_frames():
    """10-10, a confusable 16-10 hold, two unreadable menu frames, then a readable 17-10 menu.

    10 to 16 is a look-alike, so the hold is two seconds and wants two more bar frames.
    Those land before the menu. The hold then ends on the first readable menu frame, and
    the posted scorebug shares that frame's timestamp.
    """
    blank, menu = _blank(), _menu()
    return ([(1_000, blank),
             (2_000, blank), (2_300, blank), (2_600, blank), (2_900, blank),
             (3_200, blank), (3_500, blank),
             (4_200, menu), (4_500, menu),
             (5_000, menu), (5_500, menu), (6_000, menu), (6_500, menu), (7_000, menu)])


def _td_script(t_ms):
    if t_ms < 1_500:
        return (10, 10, 4, 0)
    if t_ms < 4_000:
        return (16, 10, 4, 0)
    return None


def _drive_td_flush(corr):
    menu = _QueuedMenu([_Unread(), _Unread()] + [_MenuRead(17, 10)] * 8)
    posts = []
    core = ReaderCore(posts.append, _Bar(_td_script), _AgreePair((10, 10)), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=menu,
                      end_menu_agree=EndMenuAgreement())
    core.delta_corroboration = list(corr)
    for t, img in _td_flush_frames():
        core.on_frame(t, img)
    return core, posts


def test_touchdown_flush_on_the_menu_frame_does_not_confirm_later_input(tmp_path):
    """A held touchdown posts on the menu frame. Input after that screen does not confirm.

    Live input is 30 s later. The stop-time re-read loads input 8 s later and flushes the
    same hold again. Dropping the shared timestamp makes the cutoff vanish and both confirm.
    """
    live, posts = _drive_td_flush([{"kind": "input_activity", "t_ms": 35_000}])
    assert live.stats.get("touchdown_jump") == 1
    scores = [p for p in posts if p.get("type") == "score"]
    assert scores[-1]["t_ms"] == 5_000
    assert (scores[-1]["away"], scores[-1]["home"]) == (16, 10)
    assert live.final["basis"] == "end_menu"
    assert (live.final["away"], live.final["home"]) == (17, 10)
    assert live.final["final_confirmed"] is False
    assert live.final.get("delta_corroboration") is None

    bundle = tmp_path / "match-td-flush"
    bundle.mkdir()
    # The re-read replays the hold. The timeline it is seeded with is the score from before it.
    (bundle / "timeline.jsonl").write_text(
        json.dumps({"t_ms": 1_000, "home": 10, "away": 10, "quarter": 4, "clock_s": 0, "clock_red": True}) + "\n",
        encoding="utf-8")
    (bundle / "video.mp4").write_bytes(b"not-a-real-video")
    (bundle / "manifest.jsonl").write_text(json.dumps({
        "type": "segment", "seq": 0, "file": "video.mp4", "wall_ms": 7_000,
    }) + "\n", encoding="utf-8")
    (bundle / "inputs.jsonl").write_text(
        json.dumps({"type": "in", "t_ms": 13_000, "b": 32}) + "\n", encoding="utf-8")
    draft = {"result": {"home_score": 10, "away_score": 10, "quarter": 4, "winner": "tie",
                        "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}}

    def factory(_post):
        menu = _QueuedMenu([_Unread(), _Unread()] + [_MenuRead(17, 10)] * 8)
        return ReaderCore(_post, _Bar(_td_script), _AgreePair((10, 10)), None, _detect_final,
                          final_screen_detector=madden_final_screen, end_menu_reader=menu,
                          end_menu_agree=EndMenuAgreement())

    out = qm.tail_final_check(
        bundle, draft, ffmpeg="ffmpeg", core_factory=factory, frames=_td_flush_frames(),
        live_final=live.final)
    res = out["result"]
    assert res["method"] == "end_menu_read"
    assert (res["away_score"], res["home_score"]) == (17, 10)
    assert res["final_confirmed"] is False


def test_evidence_on_the_earlier_end_screen_does_not_confirm():
    """Input, haptic, and a clip from the first end screen through the scorebug return do not count.

    Input after that return and before the new menu still does. That is the F1 case.
    """
    sha = "ab" * 32
    for corr in (
        [{"kind": "input_activity", "t_ms": 6_000}],
        [{"kind": "haptic", "t_ms": 8_000}],
        [{"kind": "score_change_clip", "t_ms": 7_000, "sha256": sha}],
    ):
        core = _feed_labelled_screen_then_scorebug_then_menu(6_000, corr=corr)
        assert core.final["basis"] == "end_menu"
        assert (core.final["away"], core.final["home"]) == (10, 16)
        assert core.final["final_confirmed"] is False
        assert core.final.get("delta_corroboration") is None


def _forge_script(t_ms):
    if t_ms < 2_000:
        return (7, 10, 4, 30)
    if t_ms == 8_000:
        return (7, 10, 4, 0)
    return None


def test_forged_scorebug_after_a_rejected_menu_does_not_confirm_navigation():
    """A menu rejected for a running clock, one forged 0:00 frame, then the menu again.

    Navigation input at 5 s is on the first screen. It does not confirm the walk-off.
    One unreadable frame after the forged read does not reuse the rejected agreement.
    """
    blank, menu = _blank(), _menu()
    one = _QueuedMenu([_MenuRead(14, 10)] * 8 + [_Unread()])
    posts = []
    core = ReaderCore(posts.append, _Bar(_forge_script), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=one,
                      end_menu_agree=EndMenuAgreement())
    core.delta_corroboration = [{"kind": "input_activity", "t_ms": 5_000}]
    for t, img in ((1_000, blank), (4_000, menu), (4_500, menu), (5_000, menu), (5_500, menu),
                   (6_000, menu), (6_500, menu), (7_000, menu), (7_200, menu), (8_000, blank),
                   (8_500, menu)):
        core.on_frame(t, img)
    assert core.end_menu_rejected and "clock not 0:00" in core.end_menu_rejected.get("why", "")
    assert core.end_menu_agree.stable is None
    assert core.end_menu is None
    assert core.final is None

    again = _QueuedMenu([_MenuRead(14, 10)] * 16)
    posts = []
    core = ReaderCore(posts.append, _Bar(_forge_script), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=again,
                      end_menu_agree=EndMenuAgreement())
    core.delta_corroboration = [{"kind": "input_activity", "t_ms": 5_000}]
    # 4.0 s through 7.2 s is long enough to keep the labelled screen after the forged read.
    for t, img in ((1_000, blank), (4_000, menu), (4_500, menu), (5_000, menu), (5_500, menu),
                   (6_000, menu), (6_500, menu), (7_000, menu), (7_200, menu), (8_000, blank),
                   (8_500, menu), (9_000, menu), (9_500, menu), (10_000, menu)):
        core.on_frame(t, img)
    assert core.final["basis"] == "end_menu"
    assert (core.final["away"], core.final["home"]) == (14, 10)
    assert core.final["final_confirmed"] is False
    assert not any(p.get("final_confirmed") is True for p in posts)


def test_menu_frames_do_not_agree_across_a_scorebug_read():
    """Two menu frames, a scorebug read, then one menu frame is not an agreement.

    Keeping the streak would post 10-16 from a frame that started before the scorebug returned.
    """
    blank, menu = _blank(), _menu()
    posts = []
    core = ReaderCore(posts.append, _Scorebug(10, 10, 4, 0, True), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=_MenuReader(10, 16),
                      end_menu_agree=EndMenuAgreement())
    for t, img in ((1_000, blank), (4_000, menu), (4_250, menu), (4_500, blank), (4_750, menu)):
        core.on_frame(t, img)
    assert core.end_menu_agree.stable is None
    # The single post-return frame is the whole streak. Frames from before the read are gone.
    assert core.end_menu_agree.streak == [(4_750, 10, 16)]
    assert core.end_menu is None
    assert core.final is None
    assert not any(p.get("type") == "final" for p in posts)


def test_stale_replay_misread_is_not_confirmed():
    """A 0-6 replay after a real 7-6, then a menu misread as 8-6, does not confirm.

    The input is on the first end screen, before the replay. It is not evidence for 8-6.
    """
    def script(t_ms):
        if t_ms < 3_000:
            return (7, 6, 4, 0)
        if 9_000 <= t_ms <= 12_000:
            return (0, 6, 4, 0)
        return None

    blank, menu = _blank(), _menu()
    menu_reader = _LateDigits(8, 6, ready_at=14_000)
    posts = []
    core = ReaderCore(posts.append, _Bar(script), _Agree(), None, _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=menu_reader,
                      end_menu_agree=EndMenuAgreement())
    core.delta_corroboration = [{"kind": "input_activity", "t_ms": 5_000}]
    for t, img in ((1_000, blank), (2_000, blank), (4_000, menu), (5_000, menu), (6_000, menu),
                   (7_000, menu), (8_000, menu), (10_000, blank), (11_000, blank),
                   (14_000, menu), (14_500, menu), (15_000, menu), (15_500, menu)):
        if t >= 14_000:
            menu_reader.t = t
        core.on_frame(t, img)
    assert core.final is not None
    assert (core.final["away"], core.final["home"]) == (8, 6)
    assert core.final["final_confirmed"] is False
    assert not any(
        p.get("away") == 8 and p.get("home") == 6 and p.get("final_confirmed") is True for p in posts)


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
