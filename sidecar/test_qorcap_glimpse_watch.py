"""Glimpse shadow watcher (v0.3.21): advisory-only, reads only unknown frames, never touches QorCap's files.
No network: the model is a fake; the frame tap is a fake or a local stub server."""
import hashlib
import http.server
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import qorcap_glimpse_watch as W  # noqa: E402

FX = HERE / "fixtures"
FRAME = (FX / "m27_mnp_q1.jpg").read_bytes()
FRAME2 = (FX / "m27_mnp_q2.jpg").read_bytes()


class SimClock:
    def __init__(self, t=1_792_272_000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def reply(region="SCOREBUG", a=0, h=7, q=1, clock="3:10", title=None, values=None):
    obj = {"region": region, "stat_title": title, "stat_values": values, "away_score": a if region == "SCOREBUG" else None,
           "home_score": h if region == "SCOREBUG" else None, "quarter": q if region == "SCOREBUG" else None,
           "clock": clock if region == "SCOREBUG" else None, "notes": "x"}
    return json.dumps(obj)


def fake_post(text_fn, cost=0.0008):
    calls = []

    def post(body, timeout):
        calls.append(body)
        return {"id": f"gen-{len(calls)}", "usage": {"cost": cost}, "choices": [{"message": {"content": text_fn(len(calls))}}]}
    post.calls = calls
    return post


@pytest.fixture(autouse=True)
def _guard_off():
    yield
    W.POLICY.active = False
    W.POLICY.match_dir = W.POLICY.out_dir = None
    W.POLICY.violations.clear()


def test_prompts_are_the_pinned_glimpse_shadow_prompts():
    for name, sha in W.PROMPT_SHA256.items():
        assert hashlib.sha256((W.PROMPTS / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == sha
    assert "ADVISORY OBSERVER" in W._prompt("ROLE_v1.md")


@pytest.mark.parametrize("lock,new,ok,flag", [
    ((0, 6), (0, 6), True, "agrees_with_lock"),
    ((0, 6), (0, 7), True, "legal_change_vs_lock"),       # PAT after a TD
    ((0, 7), (0, 8), False, "home_plus1_without_td"),
    ((0, 7), (0, 14), True, "legal_change_vs_lock"),
    ((0, 7), (0, 12), False, "home_delta_5_not_a_football_score"),
    ((0, 7), (6, 14), False, "both_sides_changed_vs_lock"),
    ((6, 14), (14, 6), False, "sides_swapped_vs_lock"),
    ((6, 21), (0, 0), False, "score_decrease_vs_lock"),
    ((20, 20), (20, 40), False, "home_delta_20_not_a_football_score"),  # the 20-0 -> 20-20 OCR echo shape
])
def test_jev_style_plausibility(lock, new, ok, flag):
    hist = [{"away": 0, "home": 0}, {"away": 0, "home": 6}] if lock == (0, 6) else [{"away": 0, "home": 0}, {"away": lock[0], "home": lock[1]}]
    got, flags = W.plausible_score({"away": lock[0], "home": lock[1]}, hist, *new)
    assert (got, flag in flags) == (ok, True)
    assert W.plausible_score(None, [], 0, 0) == (False, ["no_reference_lock"])


def test_stats_box_stays_stats_and_bad_replies_are_unknown():
    lock = {"away": 0, "home": 7}
    schema = json.loads(W._prompt("crop_v1.json"))
    st = W.assess(W.parse_strict(reply("STATS_OVERLAY", title="RUSHING YDS", values=["0", "20"]), schema), lock, [lock], 1)
    assert st["region"] == "STATS_OVERLAY" and st["advisory_score"] is None and st["stat"]["title"] == "RUSHING YDS"
    mixed = json.loads(reply(a=0, h=20))
    mixed["stat_title"] = "TOTAL YDS"
    assert W.assess(mixed, lock, [lock], 1)["advisory_score"] is None
    for bad in ["```json\n" + reply() + "\n```", "Score is 0-7", reply()[:-1], json.dumps({**json.loads(reply()), "confidence": 0.9}), ""]:
        with pytest.raises(W.Rejected):
            W.parse_strict(bad, schema)
    assert W.assess(None, lock, [lock], 1)["flags"] == ["rejected_or_unknown"]
    q = W.assess(json.loads(reply(a=0, h=7, q=1)), lock, [lock], 2)
    assert q["advisory_score"] == {"away": 0, "home": 7} and q["advisory_quarter"] is None and "quarter_backwards_vs_reader" in q["flags"]
    q3 = json.loads(reply(a=0, h=7, q=3))
    assert W.assess(q3, lock, [lock], 1, 60_000)["advisory_quarter"] is None  # 1ST -> 3RD while the reader is current
    stale = W.assess(q3, lock, [lock], 1, 10 * 60_000)  # 162055: the reader read 1ST, then null for 10+ minutes
    assert stale["advisory_quarter"] == 3 and "quarter_jump_reader_quarter_stale" in stale["flags"]


def test_timeline_tail_reads_only_new_bytes_and_classifies(tmp_path):
    tl = tmp_path / "timeline.jsonl"
    tl.write_text(json.dumps({"t_ms": 1000, "away": 0, "home": 0, "quarter": 1, "clock_s": 900, "clock_red": False}) + "\n"
                  + json.dumps({"t_ms": 2000, "until_ms": 9000, "obs": "scorebug_absent", "n": 4}) + "\n"
                  + '{"t_ms": 3000, "away": 0, "ho', encoding="utf-8")
    t = W.TimelineTail(tl)
    assert t.poll() == 1
    with open(tl, "a", encoding="utf-8") as f:
        f.write('me": 6, "quarter": null, "clock_s": 800, "clock_red": false}\n')
    assert t.poll() == 1 and [(x["away"], x["home"]) for x in t.locks] == [(0, 0), (0, 6)]
    assert t.state_at(1500)[0] == "known"
    assert t.state_at(3200)[0] == "quarter_unknown"
    assert t.state_at(3000 + W.STALE_MS + 1)[0] == "score_unknown"
    assert t.state_at(500)[0] == "known"  # the reader's row for this very frame (grace)
    assert t.state_at(-5000)[0] == "score_unknown"


def test_guard_refuses_qorcap_live_files_and_match_folder_writes(tmp_path):
    md = tmp_path / "match-x"
    (md / ".qorcap-run").mkdir(parents=True)
    for n in ("timeline.jsonl", "inputs.jsonl", "receipt.draft.json", ".qorcap-run/snapshot.jpg"):
        (md / n).write_text("{}\n")
    W.install_guard(md, tmp_path / "out")
    assert (md / "timeline.jsonl").read_text() == "{}\n"
    for n in (".qorcap-run/snapshot.jpg", "inputs.jsonl", "receipt.draft.json"):
        with pytest.raises(W.AccessViolation):
            open(md / n, "rb")
    with pytest.raises(W.AccessViolation):
        open(md / "timeline.jsonl", "a")
    with pytest.raises(W.AccessViolation):
        open(md / "glimpse.jsonl", "w")
    with pytest.raises(W.AccessViolation):
        os.replace(md / "timeline.jsonl", md / "t2")
    with pytest.raises(W.AccessViolation):
        open(tmp_path / "elsewhere" / ".qorcap-run" / "snapshot.jpg", "rb")
    with pytest.raises(SystemExit):
        W.install_guard(md, md / "glimpse")


class FakeTap:
    """Serves (wall_ms, jpeg) frames like GET /frame/scorebug: newest frame <= now, fresh for 3 s."""

    def __init__(self, clock, frames):
        self.clock, self.frames, self.bundle = clock, frames, None

    def get(self):
        now_ms = int(self.clock.time() * 1000)
        cur = [(i, f) for i, f in enumerate(self.frames) if f[0] <= now_ms]
        if not cur:
            return 503, None, {}
        i, (ms, jpg) = cur[-1]
        if now_ms - ms > 3000:
            return 503, None, {}
        return 200, jpg, {"x-qorcap-frame-wall-ms": str(ms), "x-qorcap-frame-seq": str(i + 1), "x-qorcap-bundle": self.bundle}


def args(tmp_path, md, **kw):
    a = W.build_parser().parse_args(["--layout", "monday_night", "--match-dir", str(md), "--out", str(tmp_path / "glimpse-out"),
                                     "--max-minutes", "3", "--weekly-floor", "0"])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def make_match(tmp_path, rows):
    md = tmp_path / "match-20261017-999999"
    (md / ".qorcap-run").mkdir(parents=True)
    (md / "timeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    for n in ("inputs.jsonl", "receipt.draft.json", "bundle.json", ".qorcap-run/snapshot.jpg"):
        (md / n).write_bytes(b"qorcap-owned " + n.encode())
    return md


def tree_hash(d):
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def test_live_loop_sends_only_unknown_frames_and_never_writes_the_match(tmp_path):
    clock = SimClock()
    t0 = int(clock.time() * 1000)
    # Reader: known 0-0 Q1 at +2 s; quarter null at +20 s; nothing (score unknown) from +40 s.
    rows = [{"t_ms": t0 + 2000, "away": 0, "home": 0, "quarter": 1, "clock_s": 880, "clock_red": False},
            {"t_ms": t0 + 20000, "away": 0, "home": 6, "quarter": None, "clock_s": 700, "clock_red": False}]
    md = make_match(tmp_path, rows)
    before = tree_hash(md)
    frames = [(t0 + 2000, FRAME), (t0 + 20000, FRAME2), (t0 + 40000, FRAME), (t0 + 44500, FRAME), (t0 + 60000, FRAME2)]
    post = fake_post(lambda n: [reply(a=0, h=7, q=1), "not json", reply(a=0, h=7), reply(a=0, h=13)][min(n - 1, 3)])
    w = W.Watcher(args(tmp_path, md, max_minutes=1.5, idle_exit_s=999, min_interval=6.0, confirm_hold=0), tap=FakeTap(clock, frames), clock=clock, post=post,
                  key_reader=lambda: 0.5, printer=lambda *_: None)
    c = w.run()
    assert c["known_skipped"] == 1 and c["quarter_unknown"] == 1 and c["score_unknown"] == 3
    assert c["sent"] == len(post.calls) == 3 and c["rate_skipped"] == 1 and c["rejected"] == 1
    sent_tasks = [b["messages"][1]["content"][0]["text"] for b in post.calls]
    assert all("Monday Night" in t and "AWAY 0 - HOME 6" in t for t in sent_tasks)
    recs = [json.loads(x) for x in (tmp_path / "glimpse-out" / md.name / "advisory.jsonl").read_text().splitlines()]
    assert all(r["tag"] == "ADVISORY_AI" and r["advisory_only"] and r["feeds_lock"] is False and r["ai_assisted"] for r in recs)
    assert [r["advisory_score"] for r in recs] == [{"away": 0, "home": 7}, None, {"away": 0, "home": 7}]
    assert "Cost this game" in (tmp_path / "glimpse-out" / md.name / "REPORT.md").read_text()
    assert W.POLICY.active and not W.POLICY.violations
    W.POLICY.active = False  # the guard is still on after the run; switch it off to hash QorCap's files here
    assert tree_hash(md) == before


def test_game_cap_and_no_key(tmp_path, monkeypatch):
    clock = SimClock()
    t0 = int(clock.time() * 1000)
    md = make_match(tmp_path, [{"t_ms": t0, "away": 0, "home": 0, "quarter": None, "clock_s": None, "clock_red": False}])
    frames = [(t0 + i * 5000, FRAME if i % 2 else FRAME2) for i in range(40)]
    post = fake_post(lambda n: reply(a=0, h=0), cost=0.01)
    w = W.Watcher(args(tmp_path, md, game_cap=0.05, dedupe_threshold=-1, idle_exit_s=999, confirm_hold=0), tap=FakeTap(clock, frames), clock=clock,
                  post=post, key_reader=lambda: 0.5, printer=lambda *_: None)
    w.run()
    assert w.budget.game_spent() <= 0.05 and len(post.calls) == 5  # the 6th would pass $0.05
    monkeypatch.delenv(W.KEY_ENV, raising=False)
    c = W.Client(w.budget)
    assert c.ask("s", "t", b"x", {})["refused"] == "no_key"


def test_frame_tap_client_against_a_stub_server():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if len(hits) == 1:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("X-Qorcap-Frame-Wall-Ms", "123")
                self.send_header("X-Qorcap-Frame-Seq", "9")
                self.end_headers()
                self.wfile.write(FRAME)
            else:
                self.send_response(429)
                self.send_header("Retry-After", "2")
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tap = W.FrameTap(f"http://127.0.0.1:{srv.server_address[1]}")
    code, jpg, hdr = tap.get()
    assert (code, jpg == FRAME, hdr["x-qorcap-frame-seq"]) == (200, True, "9")
    assert tap.get()[0] == 429 and hits == ["/frame/scorebug", "/frame/scorebug"]
    srv.shutdown()
    assert W.FrameTap("http://127.0.0.1:1").get()[0] == 0


def test_confirm_hold_skips_until_the_lock_changes(tmp_path):
    clock = SimClock()
    t0 = int(clock.time() * 1000)
    rows = [{"t_ms": t0 + i * 1000, "away": 0, "home": 0 if i < 30 else 6, "quarter": None, "clock_s": 800 - i, "clock_red": False} for i in range(60)]
    md = make_match(tmp_path, rows)
    frames = [(t0 + i * 2500, FRAME if i % 2 else FRAME2) for i in range(24)]
    post = fake_post(lambda n: reply(a=0, h=0 if n < 3 else 6, q=1))
    w = W.Watcher(args(tmp_path, md, dedupe_threshold=-1, idle_exit_s=999, min_interval=0.0, confirm_hold=20.0, max_minutes=1.0),
                  tap=FakeTap(clock, frames), clock=clock, post=post, key_reader=lambda: 0.5, printer=lambda *_: None)
    c = w.run()
    assert c["quarter_unknown"] == c["frames_seen"] and c["hold_skipped"] > 0
    # one confirmation per 20 s on 0-0, and the lock change to 0-6 at +30 s releases the hold at once
    assert len(post.calls) <= 5 and any("HOME 6" in b["messages"][1]["content"][0]["text"] for b in post.calls)


class DownTap:
    """QorCap absent: connection refused on every poll."""

    def get(self):
        return 0, None, {}


class SimClock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_waits_for_qorcap_then_exits_cleanly_when_it_never_comes(tmp_path):
    """Started by Start Match before recording: waits --wait-start-s for the tap, then stops, writing nothing anywhere."""
    a = W.build_parser().parse_args(["--layout", "auto", "--out", str(tmp_path / "out"), "--wait-start-s", "900",
                                     "--idle-exit-s", "60"])
    clk = SimClock()
    w = W.Watcher(a, tap=DownTap(), clock=clk, printer=lambda m: None)
    t0 = clk.time()
    c = w.run()
    assert 900 <= clk.time() - t0 <= 905 and c["tap_down"] > 300 and c["sent"] == 0
    assert w.match_dir is None and not (tmp_path / "out").exists()


def test_layouts_include_thursday_night_and_auto():
    for lay in ("thursday_night", "auto"):
        assert W.build_parser().parse_args(["--layout", lay]).layout == lay


def test_stops_when_qorcap_moves_to_another_match(tmp_path):
    md = make_match(tmp_path, [])
    other = tmp_path / "match-20261018-090000"
    other.mkdir()
    clk = SimClock()
    tap = FakeTap(clk, [(int(clk.time() * 1000) + i * 2500, b"\xff\xd8x") for i in range(40)])
    tap.bundle = str(md)
    a = W.build_parser().parse_args(["--layout", "auto", "--out", str(tmp_path / "out"), "--max-minutes", "3"])
    w = W.Watcher(a, tap=tap, clock=clk, printer=lambda m: None)
    w._open_match(str(md))
    tap.bundle = str(other)
    w.run()
    assert w.stop_reason and "another match" in w.stop_reason


def test_start_match_launches_the_watcher_safely():
    """Start Match starts the watcher detached, minimized, below normal, only when QORCAP_GLIMPSE_FRAME=1, inside a
    try/catch so a Glimpse problem never stops the match, strips --no-glimpse, and never prints the API key."""
    ps = (Path(__file__).resolve().parents[1] / "start-match.ps1").read_text(encoding="utf-8")
    blk = ps[ps.index("# Advisory Glimpse watcher"):ps.index("& $vpy (Join-Path $here \"sidecar\\qorcap_match.py\")")]
    for s in ('try {', '} catch {', '-WindowStyle Minimized', '"BelowNormal"', '$gFlag -eq "1"', '"--no-glimpse"',
              '"--wait-start-s", "900"', '"--idle-exit-s", "60"', 'Start-Process', 'Remove-Item Env:OPENROUTER_API_KEY'):
        assert s in blk, s
    assert "Write-Host" in blk and not any("OPENROUTER_API_KEY" in ln for ln in blk.splitlines() if "Write-Host" in ln)
    assert "Wait-Process" not in blk and "-Wait" not in blk
