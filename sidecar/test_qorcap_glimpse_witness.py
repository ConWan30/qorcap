"""Cheap-tier Glimpse witness, disagreement burst, and post-match attestation.

No network. Model replies are fixtures. Flag-off advisory bytes match a second flag-off run.
Observer output never reaches a lock: feeds_lock stays false, and TouchdownJump is not given a veto.
"""
import hashlib
import inspect
import json
import os
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
import sys
sys.path.insert(0, str(HERE))
import qorcap_glimpse_attest as A  # noqa: E402
import qorcap_glimpse_side as side  # noqa: E402
import qorcap_glimpse_watch as W  # noqa: E402
import qorcap_reader as reader  # noqa: E402

FX = HERE / "fixtures"
FRAME = (FX / "m27_mnp_q1.jpg").read_bytes()
FRAME2 = (FX / "m27_mnp_q2.jpg").read_bytes()
FLAGS = (side.WITNESS_ENV, side.BURST_ENV, side.RING_ENV, side.ATTEST_ENV, side.CLIP_ENV, side.COPY_ENV,
         side.WEEK_ENV, side.GAME_ENV, side.WITNESS_CAP_ENV, side.BURST_CAP_ENV, side.CLIP_CAP_ENV,
         side.WITNESS_INTERVAL_ENV)


def reply(region="SCOREBUG", a=0, h=7, q=1, clock="3:10", title=None, values=None):
    obj = {"region": region, "stat_title": title, "stat_values": values,
           "away_score": a if region == "SCOREBUG" else None,
           "home_score": h if region == "SCOREBUG" else None,
           "quarter": q if region == "SCOREBUG" else None,
           "clock": clock if region == "SCOREBUG" else None, "notes": "x"}
    return json.dumps(obj)


def fake_post(text_fn, cost=0.0008):
    calls = []

    def post(body, timeout):
        calls.append(body)
        text = text_fn(len(calls))
        if isinstance(text, BaseException):
            raise text
        return {"id": f"gen-{len(calls)}", "usage": {"cost": cost}, "choices": [{"message": {"content": text}}]}
    post.calls = calls
    return post


class DetClock:
    def __init__(self, t=1_792_272_000.0):
        self.t, self.p = t, 0.0

    def time(self):
        return self.t

    def perf_counter(self):
        self.p += 0.001
        return self.p

    def sleep(self, s):
        self.t += max(0.0, float(s))
        self.p += max(0.0, float(s))


class FrameTap:
    def __init__(self, clock, frames):
        self.clock, self.frames = clock, frames

    def get(self):
        now_ms = int(self.clock.time() * 1000)
        cur = [(i, f) for i, f in enumerate(self.frames) if f[0] <= now_ms]
        if not cur:
            return 503, None, {}
        i, (ms, jpg) = cur[-1]
        if now_ms - ms > 3000:
            return 503, None, {}
        return 200, jpg, {"x-qorcap-frame-wall-ms": str(ms), "x-qorcap-frame-seq": str(i + 1)}

    def get_ring(self, timeout=0.4):
        raise AssertionError("burst ring was polled while the test did not arm it")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(reader.TD_VETO_ENV, raising=False)
    yield
    W.POLICY.active = False
    W.POLICY.match_dir = W.POLICY.out_dir = None
    W.POLICY.violations.clear()


def _args(tmp, md, **kw):
    a = W.build_parser().parse_args(["--layout", "monday_night", "--match-dir", str(md), "--out", str(tmp / "glimpse-out"),
                                     "--max-minutes", "3", "--weekly-floor", "0", "--sync-calls"])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _match(tmp, rows, name="match-20261017-999999"):
    md = tmp / name
    (md / ".qorcap-run").mkdir(parents=True)
    (md / "timeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (md / "receipt.json").write_bytes(b'{"schema":"qor-receipt/v0","result":"FAIL"}\n')
    for n in ("inputs.jsonl", "bundle.json"):
        (md / n).write_bytes(b"qorcap-owned")
    return md


def _tree(d):
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def _watcher(tmp, md, post, clock=None, frames=None, env=None, monkeypatch=None, **kw):
    if env and monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    clock = clock or DetClock()
    t0 = int(clock.time() * 1000)
    if frames is None:
        frames = [(t0, FRAME)]
    w = W.Watcher(_args(tmp, md, **kw), tap=FrameTap(clock, frames), clock=clock, post=post,
                  key_reader=lambda: 0.0, printer=lambda *_: None)
    return w, clock, t0


def _hdr(ms, seq, bundle=None):
    h = {"x-qorcap-frame-wall-ms": str(ms), "x-qorcap-frame-seq": str(seq)}
    if bundle:
        h["x-qorcap-bundle"] = str(bundle)
    return h


def _user(body):
    return body["messages"][1]["content"][0]["text"]


def _ctx(text):
    return text.split("Context for this crop", 1)[-1]


def test_witness_flag_off_sends_no_known_frames(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 8, "quarter": 2, "clock_s": 400, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=0, q=2))
    w, clock, t0 = _watcher(tmp_path, md, post, clock=clock, monkeypatch=monkeypatch)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert post.calls == [] and w.c["known_skipped"] == 1 and w.c["witness_sent"] == 0
    assert not (w.out / "burst.jsonl").exists()
    monkeypatch.setenv(side.WITNESS_ENV, "0")
    monkeypatch.setenv(side.BURST_ENV, "0")
    w2, _, t2 = _watcher(tmp_path, md, post, monkeypatch=monkeypatch)
    w2._open_match(str(md))
    w2.on_frame(FRAME, _hdr(t2, 1, md))
    assert w2.c["known_skipped"] == 1 and post.calls == []


def test_witness_flag_off_advisory_bytes_match(tmp_path, monkeypatch):
    def once(root, flag):
        W.POLICY.active = False
        if flag is not None:
            monkeypatch.setenv(side.WITNESS_ENV, flag)
            monkeypatch.setenv(side.BURST_ENV, flag)
            monkeypatch.setenv(side.RING_ENV, flag)
        clock = DetClock()
        t0 = int(clock.time() * 1000)
        rows = [{"t_ms": t0 + 2000, "away": 0, "home": 0, "quarter": 1, "clock_s": 880, "clock_red": False},
                {"t_ms": t0 + 20000, "away": 0, "home": 6, "quarter": None, "clock_s": 700, "clock_red": False}]
        md = _match(root, rows)
        frames = [(t0 + 2000, FRAME), (t0 + 20000, FRAME2), (t0 + 40000, FRAME), (t0 + 44500, FRAME), (t0 + 60000, FRAME2)]
        texts = [reply(a=0, h=7, q=1), "not json", reply(a=0, h=7), reply(a=0, h=13)]
        post = fake_post(lambda n: texts[min(n - 1, 3)])
        w = W.Watcher(_args(root, md, max_minutes=1.5, idle_exit_s=999, min_interval=6.0, confirm_hold=0),
                      tap=FrameTap(clock, frames), clock=clock, post=post, key_reader=lambda: 0.5, printer=lambda *_: None)
        w.run()
        adv = (root / "glimpse-out" / md.name / "advisory.jsonl").read_bytes()
        assert b'"kind": "witness"' not in adv and b'"kind": "burst"' not in adv
        assert not (root / "glimpse-out" / md.name / "burst.jsonl").exists()
        assert w.c["known_skipped"] == 1 and w.c["sent"] == 3
        return adv

    first = once(tmp_path / "a", None)
    for name in (side.WITNESS_ENV, side.BURST_ENV, side.RING_ENV):
        monkeypatch.delenv(name, raising=False)
    second = once(tmp_path / "b", "0")
    assert first == second


def test_witness_prompt_withholds_lock_digits(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 8, "quarter": 2, "clock_s": 400, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=0, q=2))
    w, clock, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"},
                            monkeypatch=monkeypatch, confirm_hold=20, min_interval=6)
    w._open_match(str(md))
    before = w.last_call_t
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert len(post.calls) == 1 and w.last_call_t == before
    text = _user(post.calls[0])
    assert side.LOCK_WITHHELD in text and "AWAY 13 - HOME 8" not in text
    ctx = _ctx(text)
    assert not __import__("re").search(r"(?<!\d)13(?!\d)", ctx)
    assert not __import__("re").search(r"(?<!\d)8(?!\d)", ctx)
    # The ordinary clock is still free: an unknown frame inside the 6 s window is sent.
    w.on_frame(FRAME2, _hdr(t0 + 4000, 2, md))
    assert len(post.calls) == 2 and "AWAY 13 - HOME 8" in _user(post.calls[1])


def test_witness_record_is_advisory_only(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 8, "quarter": 2, "clock_s": 300, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=0, q=2))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"},
                        monkeypatch=monkeypatch)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    rec = json.loads((w.out / "advisory.jsonl").read_text().splitlines()[-1])
    assert rec["kind"] == "witness" and rec["feeds_lock"] is False and rec["advisory_only"] is True
    assert rec["tag"] == "ADVISORY_AI" and rec["reference_lock"]["away"] == 13
    assert "td_verdicts" not in inspect.getsource(W.Watcher._witness_call)
    assert "td_verdicts" not in inspect.getsource(W.Watcher._seal_burst)


def test_witness_stops_before_call_when_reservation_would_exceed(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 0, "home": 0, "quarter": 1, "clock_s": 800, "clock_red": False}])
    post = fake_post(lambda n: reply())
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock,
                        env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0", side.WITNESS_CAP_ENV: "0.001"},
                        monkeypatch=monkeypatch)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert post.calls == []
    rec = json.loads((w.out / "advisory.jsonl").read_text().splitlines()[-1])
    assert rec["kind"] == "witness" and str(rec["refused"]).startswith("budget") and rec["advisory_score"] is None
    assert rec["feeds_lock"] is False


def test_witness_missing_cap_refuses(tmp_path, monkeypatch):
    for bad in ("0", "", "nope"):
        monkeypatch.setenv(side.GAME_ENV, bad)
        clock = DetClock()
        t0 = int(clock.time() * 1000)
        md = _match(tmp_path / bad or "empty", [{"t_ms": t0, "away": 0, "home": 0, "quarter": 1, "clock_s": 1, "clock_red": False}])
        post = fake_post(lambda n: reply())
        w = W.Watcher(_args(tmp_path, md), tap=FrameTap(clock, [(t0, FRAME)]), clock=clock, post=post,
                      key_reader=lambda: 0.0, printer=lambda *_: None)
        w.witness_on = True
        w.witness_interval = 0
        w._open_match(str(md))
        w.on_frame(FRAME, _hdr(t0, 1, md))
        assert post.calls == []
        rec = json.loads((w.out / "advisory.jsonl").read_text().splitlines()[-1])
        assert rec["refused"] == "cap_unconfigured" and rec["feeds_lock"] is False
        W.POLICY.active = False


def test_witness_timeout_books_worst_case_and_opens_circuit(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 0, "home": 0, "quarter": 1, "clock_s": 100, "clock_red": False}])
    post = fake_post(lambda n: TimeoutError("slow"))
    w, clock, t0 = _watcher(tmp_path, md, post, clock=clock,
                            env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"}, monkeypatch=monkeypatch,
                            read_grace=0)
    w._open_match(str(md))
    for i in range(3):
        w.on_frame(FRAME, _hdr(int(clock.time() * 1000), i + 1, md))
    assert len(post.calls) == 3
    assert w.budget.game_spent() == pytest.approx(3 * W.worst_case())
    w.on_frame(FRAME, _hdr(int(clock.time() * 1000), 4, md))
    assert len(post.calls) == 3
    rec = json.loads((w.out / "advisory.jsonl").read_text().splitlines()[-1])
    assert rec["refused"] == "circuit_open" and rec["feeds_lock"] is False


def test_witness_does_not_write_match_folder_or_td_verdicts(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 8, "quarter": 2, "clock_s": 200, "clock_red": False}])
    before = _tree(md)
    receipt = hashlib.sha256((md / "receipt.json").read_bytes()).hexdigest()
    post = fake_post(lambda n: reply(a=13, h=0, q=2))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"},
                        monkeypatch=monkeypatch)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    W.POLICY.active = False
    assert _tree(md) == before
    assert hashlib.sha256((md / "receipt.json").read_bytes()).hexdigest() == receipt
    assert not (w.out / "td_verdicts.jsonl").exists()
    assert reader.td_veto_enabled() is False


def test_witness_stats_overlay_has_null_advisory_score(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 8, "quarter": 2, "clock_s": 200, "clock_red": False}])
    post = fake_post(lambda n: reply("STATS_OVERLAY", title="RUSHING YDS", values=["10", "20"]))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"},
                        monkeypatch=monkeypatch)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    rec = json.loads((w.out / "advisory.jsonl").read_text().splitlines()[-1])
    assert rec["region"] == "STATS_OVERLAY" and rec["advisory_score"] is None and rec["feeds_lock"] is False
    assert not (w.out / "burst.jsonl").exists()


def test_witness_receipt_bytes_unchanged(tmp_path, monkeypatch):
    test_witness_does_not_write_match_folder_or_td_verdicts(tmp_path, monkeypatch)


def test_witness_drops_when_lane_busy(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 0, "home": 0, "quarter": 1, "clock_s": 100, "clock_red": False}])
    started, release = threading.Event(), threading.Event()
    calls = []

    def post(body, timeout):
        calls.append(body)
        started.set()
        assert release.wait(5)
        return {"id": "g", "usage": {"cost": 0.0001}, "choices": [{"message": {"content": reply(a=0, h=0)}}]}

    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.WITNESS_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"},
                        monkeypatch=monkeypatch, read_grace=0)
    w.lane.sync = False
    w._open_match(str(md))
    t = threading.Thread(target=lambda: w.on_frame(FRAME, _hdr(t0, 1, md)))
    t.start()
    assert started.wait(5)
    w.on_frame(FRAME2, _hdr(t0, 2, md))
    assert w.c["lane_dropped"] >= 1
    release.set()
    t.join(5)
    w.lane.drain()
    assert len(calls) == 1


def test_burst_flag_off_creates_nothing(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": None, "clock_s": 300, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=8, q=2))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, monkeypatch=monkeypatch, confirm_hold=0, min_interval=0, read_grace=0)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert len(post.calls) == 1
    assert not (w.out / "burst.jsonl").exists() and not (w.out / "burst").exists()


def test_burst_does_not_write_td_verdicts_or_match_folder(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": None, "clock_s": 300, "clock_red": False}])
    before = _tree(md)
    post = fake_post(lambda n: reply(a=13, h=8, q=2))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.BURST_ENV: "1"}, monkeypatch=monkeypatch,
                        confirm_hold=0, min_interval=0, read_grace=0)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert (w.out / "burst.jsonl").is_file()
    row = json.loads((w.out / "burst.jsonl").read_text().splitlines()[-1])
    assert row["feeds_lock"] is False and row["kind"] == "burst"
    assert not (w.out / "td_verdicts.jsonl").exists()
    W.POLICY.active = False
    assert _tree(md) == before
    assert "td_verdicts" not in inspect.getsource(W.Watcher._burst_run)


def test_burst_expired_event_makes_no_call(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": 2, "clock_s": 300, "clock_red": False}])
    w, _, t0 = _watcher(tmp_path, md, fake_post(lambda n: reply()), clock=clock, env={side.BURST_ENV: "1"},
                        monkeypatch=monkeypatch, max_minutes=0.05, idle_exit_s=999, confirm_hold=0)
    out = tmp_path / "glimpse-out" / md.name
    out.mkdir(parents=True)
    (out / "td_jumps.jsonl").write_text(json.dumps({"event": "pending", "id": "old", "t_ms": t0 - 60_000,
                                                    "cand": [13, 8], "old": [13, 0]}) + "\n", encoding="utf-8")
    c = w.run()
    assert c["burst_expired"] >= 1 and c["td_expired"] == 1 and w.post is not None
    assert len(w.client.post.calls) == 0


def test_burst_cap_seals_budget_refusal(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": None, "clock_s": 300, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=8, q=2))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock,
                        env={side.BURST_ENV: "1", side.BURST_CAP_ENV: "0.001"}, monkeypatch=monkeypatch,
                        confirm_hold=0, min_interval=0, read_grace=0)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert len(post.calls) == 1  # the hinted ordinary call; the burst reservation never reaches the socket
    row = json.loads((w.out / "burst.jsonl").read_text().splitlines()[-1])
    assert row["refused"] == "budget" and row["verdict"] == "no_read" and row["feeds_lock"] is False


def test_burst_one_event_respects_crop_cap(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": None, "clock_s": 300, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=8, q=2))

    class RingTap(FrameTap):
        def get_ring(self, timeout=0.4):
            return {"frames": [{"seq": i, "wall_ms": t0, "jpeg_hex": FRAME.hex()} for i in range(4)]}

    monkeypatch.setenv(side.BURST_ENV, "1")
    monkeypatch.setenv(side.RING_ENV, "1")
    w = W.Watcher(_args(tmp_path, md, confirm_hold=0, min_interval=0, read_grace=0), tap=RingTap(clock, [(t0, FRAME)]),
                  clock=clock, post=post, key_reader=lambda: 0.0, printer=lambda *_: None)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    withheld = [c for c in post.calls if side.LOCK_WITHHELD in _user(c)]
    assert len(withheld) == 1
    assert len(list((w.out / "burst").glob("*.jpg"))) == 1


def test_burst_disagreement_does_not_change_fast_path_post(tmp_path):
    class Fr:
        def __init__(self, pair, margin):
            self.pair, self.score_margin, self.quarter, self.clock_seconds = pair, margin, 2, 100

    jump = reader.TouchdownJump(veto=None)
    last = {"away": 0, "home": 0, "clock_s": 200}
    for t in (1000, 1200, 1400, 1750):
        jump.offer(Fr((0, 7), 0.30), t, last, 2)
    assert jump.pending is not None and jump.veto is None
    (tmp_path / "burst.jsonl").write_text(json.dumps({"kind": "burst", "verdict": "disagree", "feeds_lock": False,
                                                     "id": jump.pending["id"]}) + "\n", encoding="utf-8")
    got = jump.due(1750 + reader.TD_HOLD_MS)
    assert got is not None and tuple(got.pair) == (0, 7)
    weak = reader.TouchdownJump(veto=None)
    old = {"away": 13, "home": 0, "clock_s": 400}
    for t in (1000, 1200, 1400, 1750):
        weak.offer(Fr((13, 8), 0.09), t, old, 2)
    assert weak.pending is None and weak.due(10_000) is None


def test_burst_prompt_withholds_lock_digits(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": None, "clock_s": 300, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=8, q=2))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.BURST_ENV: "1"}, monkeypatch=monkeypatch,
                        confirm_hold=0, min_interval=0, read_grace=0)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert "AWAY 13 - HOME 0" in _user(post.calls[0])
    text = _user(post.calls[1])
    assert side.LOCK_WITHHELD in text and "AWAY 13 - HOME 0" not in text
    ctx = _ctx(text)
    for n in (13, 0, 8):
        assert not __import__("re").search(rf"(?<!\d){n}(?!\d)", ctx)


def test_burst_bad_json_is_no_read(tmp_path, monkeypatch):
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 0, "quarter": None, "clock_s": 300, "clock_red": False}])
    post = fake_post(lambda n: reply(a=13, h=8, q=2) if n == 1 else "not json")
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock, env={side.BURST_ENV: "1"}, monkeypatch=monkeypatch,
                        confirm_hold=0, min_interval=0, read_grace=0)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    row = json.loads((w.out / "burst.jsonl").read_text().splitlines()[-1])
    assert row["verdict"] == "no_read" and row["feeds_lock"] is False and row["valid"] is False


def test_burst_021003_thirteen_eight_versus_thirteen_zero(tmp_path, monkeypatch):
    """Saved MNP crops stand in for the missing 021003 scorebug. Lock 13-8, blind reply 13-0."""
    clock = DetClock()
    t0 = int(clock.time() * 1000)
    md = _match(tmp_path, [{"t_ms": t0, "away": 13, "home": 8, "quarter": 2, "clock_s": 330, "clock_red": False}],
                name="match-20261017-021003")
    receipt = (md / "receipt.json").read_bytes()
    before = _tree(md)
    post = fake_post(lambda n: reply(a=13, h=0, q=2, clock="5:29"))
    w, _, t0 = _watcher(tmp_path, md, post, clock=clock,
                        env={side.WITNESS_ENV: "1", side.BURST_ENV: "1", side.WITNESS_INTERVAL_ENV: "0"},
                        monkeypatch=monkeypatch)
    w._open_match(str(md))
    w.on_frame(FRAME, _hdr(t0, 1, md))
    assert len(post.calls) == 1  # the blind witness is reused; the burst does not buy a second call
    assert side.LOCK_WITHHELD in _user(post.calls[0])
    adv = [json.loads(x) for x in (w.out / "advisory.jsonl").read_text().splitlines()]
    wit = [r for r in adv if r["kind"] == "witness"]
    assert len(wit) == 1 and wit[0]["feeds_lock"] is False and wit[0]["model_reply"]["home_score"] == 0
    assert wit[0]["reference_lock"] == {"away": 13, "home": 8, "t_ms": t0}
    burst = json.loads((w.out / "burst.jsonl").read_text().splitlines()[-1])
    assert burst["verdict"] == "disagree" and burst["reused_unhinted"] is True and burst["feeds_lock"] is False
    assert not (w.out / "td_verdicts.jsonl").exists()
    W.POLICY.active = False
    assert (md / "receipt.json").read_bytes() == receipt and _tree(md) == before


def _attest_match(tmp, *, receipt=b'{"schema":"qor-receipt/v0","result":"FAIL"}\n', clips=True):
    md = tmp / "match-20261017-021003"
    (md / ".qorcap-run").mkdir(parents=True)
    (md / "highlights").mkdir()
    (md / "receipt.json").write_bytes(receipt)
    (md / "checkpoints.jsonl").write_text(json.dumps({"hash": "abc", "seq": 1}) + "\n", encoding="utf-8")
    blob = b"clip-bytes-021003"
    (md / "highlights" / "td1.mp4").write_bytes(blob)
    if clips:
        man = {"schema": "qorcap-highlights/v0", "clips": [{
            "file": "td1.mp4", "sha256": hashlib.sha256(blob).hexdigest(),
            "lock": {"score_after": {"away": 13, "home": 8}}}]}
        (md / "highlights" / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return md


def _key(dirpath, name, seed=None):
    p = Path(dirpath) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((seed or os.urandom(32)).hex() + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def test_attest_refuses_while_recording_lock_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path)
    (md / ".qorcap-run" / "recording.lock").write_text("live", encoding="utf-8")
    os.utime(md / ".qorcap-run" / "recording.lock", (time.time(), time.time()))
    out = tmp_path / "out"
    assert A.run_attest(md, out, key_dir=tmp_path / "keys") == 3
    assert not out.exists() or not any(out.rglob("*"))


def test_attest_receipt_and_manifest_bytes_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path)
    rh, mh = hashlib.sha256((md / "receipt.json").read_bytes()).hexdigest(), hashlib.sha256((md / "highlights" / "manifest.json").read_bytes()).hexdigest()
    ch = hashlib.sha256((md / "highlights" / "td1.mp4").read_bytes()).hexdigest()
    _key(tmp_path / "keys", "glimpse-observer.key")
    assert A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys") == 0
    assert hashlib.sha256((md / "receipt.json").read_bytes()).hexdigest() == rh
    assert hashlib.sha256((md / "highlights" / "manifest.json").read_bytes()).hexdigest() == mh
    assert hashlib.sha256((md / "highlights" / "td1.mp4").read_bytes()).hexdigest() == ch
    assert not list(md.rglob("*.c2pa")) and not (md / "td_verdicts.jsonl").exists()
    assert not (tmp_path / "out" / "td_verdicts.jsonl").exists()


def test_attest_does_not_create_c2pa_or_touch_clips(tmp_path, monkeypatch):
    test_attest_receipt_and_manifest_bytes_unchanged(tmp_path, monkeypatch)


def test_attest_does_not_write_td_verdicts(tmp_path, monkeypatch):
    test_attest_receipt_and_manifest_bytes_unchanged(tmp_path, monkeypatch)
    assert "td_verdicts" not in Path(A.__file__).read_text()


def test_clip_audit_row_is_advisory_only(tmp_path, monkeypatch):
    monkeypatch.setenv(side.CLIP_ENV, "1")
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path)
    _key(tmp_path / "keys", "glimpse-observer.key")
    post = fake_post(lambda n: reply(a=13, h=0, q=2))
    assert A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys", post=post, extract_frame=lambda p: FRAME) == 0
    row = json.loads((tmp_path / "out" / "highlights_audit.jsonl").read_text().splitlines()[-1])
    assert row["kind"] == "clip_audit" and row["feeds_lock"] is False and row["advisory_only"] is True
    assert row["verdict"] == "disagree"


def test_clip_audit_prompt_withholds_lock_digits(tmp_path, monkeypatch):
    monkeypatch.setenv(side.CLIP_ENV, "1")
    md = _attest_match(tmp_path)
    post = fake_post(lambda n: reply(a=13, h=8, q=2))
    A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys", post=post, extract_frame=lambda p: FRAME)
    text = _user(post.calls[0])
    assert side.LOCK_WITHHELD in text
    ctx = _ctx(text)
    for n in (13, 8):
        assert not __import__("re").search(rf"(?<!\d){n}(?!\d)", ctx)


def test_attest_rejects_authority_key(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path)
    seed = os.urandom(32)
    _key(tmp_path / "keys", "glimpse-observer.key", seed)
    _key(tmp_path / "keys", "device.key", seed)
    assert A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys") == 0
    doc = json.loads((tmp_path / "out" / "glimpse_attestation.json").read_text())
    assert doc["signed"] is False and doc["reason"] == "key_collides_with_authority" and "signature" not in doc


def test_attest_observed_requires_signature_and_receipt_pin(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path)
    _key(tmp_path / "keys", "glimpse-observer.key")
    assert A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys") == 0
    doc = json.loads((tmp_path / "out" / "glimpse_attestation.json").read_text())
    raw = (md / "receipt.json").read_bytes()
    assert doc["status"] == "observed" and A.verify_attestation(doc, raw)
    broken = dict(doc)
    broken["signature"] = dict(doc["signature"], sig="00" * 64)
    assert not A.verify_attestation(broken, raw)
    assert not A.verify_attestation(doc, raw + b" ")


def test_clip_audit_stops_when_reservation_would_exceed(tmp_path, monkeypatch):
    monkeypatch.setenv(side.CLIP_ENV, "1")
    monkeypatch.setenv(side.CLIP_CAP_ENV, "0.001")
    md = _attest_match(tmp_path)
    post = fake_post(lambda n: reply(a=13, h=8))
    A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys", post=post, extract_frame=lambda p: FRAME)
    assert post.calls == []
    row = json.loads((tmp_path / "out" / "highlights_audit.jsonl").read_text().splitlines()[-1])
    assert row["status"] == "not_run" and row["reason"] == "budget" and row["feeds_lock"] is False


def test_attest_signs_a_failing_receipt_without_claiming_pass(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path, receipt=b'{"schema":"qor-receipt/v0","result":"FAIL","trust_tier":"none"}\n')
    _key(tmp_path / "keys", "glimpse-observer.key")
    A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys")
    text = (tmp_path / "out" / "glimpse_attestation.json").read_text()
    doc = json.loads(text)
    assert doc["status"] == "observed" and doc["signed"] is True
    assert "PASS" not in text and "trust_tier" not in doc and "winner" not in doc
    assert A.verify_attestation(doc, (md / "receipt.json").read_bytes())


def test_attest_missing_receipt_is_unsigned_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = tmp_path / "match-empty"
    (md / ".qorcap-run").mkdir(parents=True)
    post = fake_post(lambda n: reply())
    assert A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys", post=post) == 0
    doc = json.loads((tmp_path / "out" / "glimpse_attestation.json").read_text())
    assert doc == {"schema": A.SCHEMA, "status": "receipt_absent", "signed": False}
    assert post.calls == []
    assert not (tmp_path / "keys" / "glimpse-observer.key").exists()


def test_clip_audit_bad_json_is_no_read(tmp_path, monkeypatch):
    monkeypatch.setenv(side.CLIP_ENV, "1")
    md = _attest_match(tmp_path)
    post = fake_post(lambda n: "not json at all")
    A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys", post=post, extract_frame=lambda p: FRAME)
    row = json.loads((tmp_path / "out" / "highlights_audit.jsonl").read_text().splitlines()[-1])
    assert row["verdict"] == "no_read" and row["feeds_lock"] is False and row["model_reply"] is None


def test_attest_copy_flag_off_leaves_match_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(side.ATTEST_ENV, "1")
    md = _attest_match(tmp_path)
    before = _tree(md)
    _key(tmp_path / "keys", "glimpse-observer.key")
    A.run_attest(md, tmp_path / "out", key_dir=tmp_path / "keys")
    assert _tree(md) == before
    assert not (md / "glimpse_attestation.json").exists()
    assert (tmp_path / "out" / "glimpse_attestation.json").is_file()


def test_credentials_tool_is_not_invoked():
    src = Path(A.__file__).read_text().lower()
    assert "c2pa" not in src
    for name in ("attach_credentials", "write_c2pa", "credential_attach", "content_credentials"):
        assert not hasattr(A, name)


def test_attest_flag_off_writes_nothing(tmp_path, monkeypatch):
    md = _attest_match(tmp_path)
    assert A.main(["--match-dir", str(md), "--out", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "out").exists()


def test_observer_modules_have_no_path_into_locks():
    reader_src = (HERE / "qorcap_reader.py").read_text()
    for needle in ("qorcap_glimpse_side", "qorcap_glimpse_attest", "burst.jsonl", "glimpse_attestation"):
        assert needle not in reader_src
    side_src = (HERE / "qorcap_glimpse_side.py").read_text()
    assert "td_verdicts" not in side_src and "receipt.json" not in side_src
