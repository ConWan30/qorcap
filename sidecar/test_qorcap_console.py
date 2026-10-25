"""Snapshots and fallbacks for the Start Match console. Presentation only."""
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_console as qc  # noqa: E402
import qorcap_match as qm  # noqa: E402
import qorcap_submit as qs  # noqa: E402

GOLD = "255;194;61"
BANNED = ("attested", "attestation", "hardware-verified", "hardware verified")


@pytest.fixture(autouse=True)
def _clear_say_hook():
    qc.set_say_hook(None)
    yield
    qc.set_say_hook(None)


class Tty(io.StringIO):
    def isatty(self):
        return True


def _text(grid) -> str:
    lines = qc.grid_text(grid).splitlines()
    assert lines
    assert len({len(line) for line in lines}) == 1
    return "\n".join(lines)


def _low(text: str) -> str:
    return text.lower()


def test_glyphs_are_fixed_ascii_boxes():
    for name in ("CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT", "OPTIONS", "L1"):
        rows = qc.button_glyph(name)
        assert len(rows) == 5
        assert all(len(row) == 9 and row[0] == "|" and row[-1] == "|" for row in rows[1:4])
        assert rows[0] == rows[-1] == "+-------+"
        assert all(ord(ch) < 128 for row in rows for ch in row)
    cross = "\n".join(qc.button_glyph("CROSS"))
    assert "X" in cross and "\\" in cross


def test_dashboard_snapshot():
    grid = qc.layout_dashboard(qc.sample_snap(), width=78, height=18, tick=1, unicode=True)
    text = _text(grid)
    assert len(text.splitlines()[0]) == 78
    for bit in (
        "QORCAP", "HDMI IN", "side HOME", "REC", "14:02", "AWAY 14", "HOME 21", "Q2  8:41",
        "29.7 / 30", "25,920", "connected", "DualSense Edge", "linked", "checkpoints 410",
        "anchors 7", "82.1 GB", "read-only",
    ):
        assert bit in text, bit
    for word in BANNED:
        assert word not in _low(text)
    assert "┌" in text
    ascii_text = qc.grid_text(qc.layout_dashboard(qc.sample_snap(), width=78, height=18, tick=1, unicode=False))
    assert "┌" not in ascii_text
    assert ascii_text.splitlines()[0].startswith("+")
    assert "tpm_claimed" not in _low(text)


def test_status_line_stays_the_classic_one_liner():
    line = qm.status_line(
        {"state": "capturing", "recording_secs": 75, "disk_free_mb": 1500, "warning": "AUDIO FAILED"},
        {"score": {"home": 7, "away": 3, "quarter": 2, "clock_s": 65}, "segments": 4, "clips": 1}, 4)
    assert line.startswith("[REC] 01:15")
    assert "AWAY 3 - HOME 7" in line and "Q2 1:05" in line and "disk 1500MB" in line and "AUDIO FAILED" in line
    plain = _text(qc.layout_plain_screen(qc.status_line_sample(), width=78, height=8))
    assert "[REC]" in plain and "AWAY 14 - HOME 21" in plain
    assert "QORCAP" not in plain and "┌" not in plain


def test_challenge_cards_pending_pass_fail_expired():
    now = 19_000
    pending = _text(qc.layout_challenge(qc.sample_card(matched=1), width=78, height=30, tick=0, now_ms=now))
    assert "REFEREE CHALLENGE" in pending
    assert "72 seconds left" in pending and "entered 1 of 5" in pending
    assert "At the line, before the snap" in pending
    assert "####" in pending  # block title, not the one-line fallback
    for name in ("OPTIONS", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE"):
        assert name in pending
    assert qc.PAUSE_MENU_HINT in pending
    assert "Enter them last" not in pending
    passed = _text(qc.layout_challenge(qc.sample_card("pass"), width=78, height=30, tick=1, now_ms=now))
    assert "stay until done" not in passed
    assert "Buttons matched on this pad. Not a referee result." in passed
    assert "The referee reads the controller log and the pause." in passed
    failed = _text(qc.layout_challenge(qc.sample_card("fail"), width=78, height=30, tick=1, now_ms=now))
    assert "did not match the pattern" in failed
    expired = _text(qc.layout_challenge(qc.sample_card("expired"), width=78, height=30, tick=1, now_ms=now))
    assert "Time window closed before the pattern was entered." in expired
    for text in (pending, passed, failed, expired):
        for word in BANNED:
            assert word not in _low(text)


def test_pause_menu_hint_only_when_the_buttons_fit():
    now = 19_000
    card = qc.ChallengeCard("kickoff", "game start", ["OPTIONS", "UP", "DOWN", "LEFT"], 1_000, 91_000, matched=1)
    plain = _text(qc.layout_challenge(card, width=78, height=30, tick=0, now_ms=now))
    assert "stay until done" not in plain
    assert "UP" in plain
    narrow = _text(qc.layout_challenge(qc.sample_card(matched=1), width=40, height=30, tick=0, now_ms=now))
    assert "stay until done" not in narrow
    assert "CROSS" in narrow and "TRIANGLE" in narrow
    floor = _text(qc.layout_challenge(qc.sample_card(matched=1), width=60, height=24, tick=0, now_ms=now))
    assert qc.PAUSE_MENU_HINT in floor
    assert "Enter them last" not in floor
    for word in BANNED:
        assert word not in _low(plain) and word not in _low(narrow)


def test_short_console_keeps_the_pattern_and_the_seconds():
    text = _text(qc.layout_challenge(qc.sample_card(matched=1), width=78, height=20, tick=0, now_ms=19_000))
    assert "GAME START" in text
    assert "72 seconds left" in text and "entered 1 of 5" in text
    assert "CROSS" in text and "TRIANGLE" in text
    full = _text(qc.layout_challenge(qc.sample_card(matched=1), width=78, height=24, tick=0, now_ms=19_000))
    assert qc.PAUSE_MENU_HINT in full
    assert "Enter them last" not in full
    assert "72 seconds left" in full and "snap comes first" in full and "TRIANGLE" in full
    packed = _text(qc.layout_challenge(qc.sample_card(matched=1), width=60, height=24, tick=0, now_ms=19_000, unicode=False))
    assert len(packed.splitlines()[0]) == 60
    assert "72 seconds left" in packed and "█" not in packed and "#" in packed
    assert "┌" not in packed


def test_gold_is_only_the_referee_verified_card():
    def painted(card, level="truecolor"):
        grid = qc.layout_challenge(card, width=78, height=30, tick=1, now_ms=19_000)
        return qc.paint(grid, level)

    for card in (qc.sample_card(matched=1), qc.sample_card("pass"), qc.sample_card("fail"), qc.sample_card("expired")):
        blob = painted(card)
        assert GOLD not in blob
        assert "38;2" in blob
    verified = painted(qc.sample_card("pass", verified=True))
    assert GOLD in verified
    assert "Referee verified." in qc.grid_text(qc.layout_challenge(
        qc.sample_card("pass", verified=True), width=78, height=30, tick=1, now_ms=19_000))
    none = painted(qc.sample_card("pass"), "none")
    assert "38;2" not in none and GOLD not in none

    def frame(card):
        return qc.layout_challenge(card, width=78, height=30, tick=0, now_ms=19_000)[0][0][1]

    assert frame(qc.sample_card(matched=1)) == "cyan"
    assert frame(qc.sample_card("pass")) == "cyan"
    assert frame(qc.sample_card("fail")) == "red"
    assert frame(qc.sample_card("expired")) == "mute"
    assert frame(qc.sample_card("pass", verified=True)) == "gold"


def test_dashboard_footer_uses_gold_only_when_verified():
    plain = qc.layout_dashboard(qc.sample_snap(), width=78, height=18, last=("game start", "pass", False))
    text = qc.grid_text(plain)
    assert "buttons on this pad" in text and "referee verified" not in text
    colors = {color for row in plain for _, color in row}
    assert "gold" not in colors
    verified = qc.layout_dashboard(qc.sample_snap(), width=78, height=18, last=("game start", "pass", True))
    assert "referee verified" in qc.grid_text(verified)
    assert any(color == "gold" for row in verified for _, color in row)


def test_color_and_plain_policy():
    assert qc.branded_requested(True, Tty(), {}) is False
    assert qc.branded_requested(False, Tty(), {"QORCAP_PLAIN": "yes"}) is False
    assert qc.branded_requested(False, Tty(), {"QORCAP_PLAIN": "1"}) is False
    assert qc.branded_requested(False, Tty(), {"TERM": "dumb"}) is False
    assert qc.branded_requested(False, io.StringIO(), {"TERM": "xterm-256color"}) is False
    assert qc.branded_requested(False, Tty(), {"NO_COLOR": "1", "TERM": "xterm-256color"}) is True
    assert qc._color_level({"NO_COLOR": "", "QORCAP_COLOR": "truecolor"}) == "none"
    assert qc._color_level({"WT_SESSION": "1"}) == "truecolor"
    assert qc._color_level({"COLORTERM": "truecolor"}) == "truecolor"
    assert qc._color_level({"TERM": "xterm-256color"}) == "256"
    assert qc._color_level({"QORCAP_COLOR": "16", "TERM": "xterm-256color"}) == "16"
    ui = qc.MatchConsole(out=Tty(), env={"NO_COLOR": "1", "TERM": "xterm-256color"})
    assert ui.branded and ui.level == "none"
    buf = io.StringIO()
    plain = qc.MatchConsole(plain=True, out=buf, env={"TERM": "xterm-256color"})
    assert not plain.branded
    plain.start()
    assert plain._thread is None and buf.getvalue() == ""
    ascii_ui = qc.MatchConsole(force=True, out=io.StringIO(), env={"QORCAP_ASCII": "1"})
    assert ascii_ui.unicode is False
    assert qc._spinner(0, False) == "|"
    assert qc._spinner(0, True) == "*"


def test_tint_only_a_verified_banner():
    env = {"COLORTERM": "truecolor"}
    assert GOLD in qc.tint_verified_banner("RESULT: VERIFIED\n", env, Tty())
    assert GOLD in qc.tint_verified_banner("  OFFICIAL RESULT: VERIFIED", env, Tty())
    for msg in ("RESULT: SELF_ATTESTED", "Challenge kickoff PASSED", "RESULT: FAIL", "SELF-ATTESTED"):
        assert GOLD not in qc.tint_verified_banner(msg, env, Tty())
    assert GOLD not in qc.tint_verified_banner("RESULT: VERIFIED", {"NO_COLOR": "1"}, Tty())
    assert GOLD not in qc.tint_verified_banner("RESULT: VERIFIED", env, io.StringIO())


def test_snapshot_maps_status_without_inventing_a_device_claim():
    class Live:
        anchors = 2
        issued = {"kickoff"}
        failures = 1

    snap = qc.console_snapshot(
        {"state": "disk_full", "recording_secs": 10, "fps_actual": 29.7, "fps_target": 30,
         "frames": 100, "dup_frames": 1, "drop_frames": 0, "disk_free_mb": 1500,
         "warning": "AUDIO FAILED", "role": "away", "res": "1280x720",
         "input": {"state": "connected", "device": "DualSense", "buttons": ["CROSS"]}},
        {"score": None, "segments": 2, "clips": 1}, 4, Live())
    assert snap["referee"] == "linked" and snap["retries"] == 1 and snap["disk_mb"] == 1500
    text = qc.grid_text(qc.layout_dashboard(snap, width=78, height=18))
    assert "DISK FULL" in text and "1500 MB" in text and "AUDIO FAILED" in text
    assert "waiting for scorebug" in text and "retries 1" in text
    assert qc.console_snapshot({}, {}, 0, None)["referee"] == "off"

    class Down:
        anchors = 0
        issued = ()
        failures = 2

    assert qc.console_snapshot({}, {}, 0, Down())["referee"] == "no link"

    class Wait:
        anchors = 0
        issued = ()
        failures = 0

    assert qc.console_snapshot({}, {}, 0, Wait())["referee"] == "waiting"
    assert "NO SIGNAL" in qc.grid_text(qc.layout_dashboard(
        qc.console_snapshot({"state": "waiting_for_device"}, {}, 0, None), width=78, height=18))


def test_button_edges_follow_the_referee_rule_and_settle_is_honest():
    def card():
        return qc.ChallengeCard("kickoff", "game start", ["OPTIONS", "CROSS", "CIRCLE"], 0, 5_000)

    # An extra press inside the pattern breaks the try for the referee, so the card resets too
    # (it used to skip the stray press and show PASS while the referee said pattern_mismatch).
    stray = card()
    for press in ("OPTIONS", "UP", "CROSS", "CIRCLE"):
        qc.note_held(stray, [press], trusted=True)
        qc.note_held(stray, [], trusted=True)
    qc.settle(stray, 1_000, trusted=True, now_s=1.0)
    assert stray.result is None and stray.matched == 0 and stray.wrong == "UP"
    qc.settle(stray, 9_000, trusted=True, now_s=9.0)
    assert stray.result == "fail"

    done = card()
    for press in ("OPTIONS", "CROSS", "CIRCLE"):
        qc.note_held(done, [press], trusted=True)
        qc.note_held(done, [], trusted=True)
    qc.settle(done, 1_000, trusted=True, now_s=1.0)
    assert done.result == "pass" and done.matched == 3
    assert "Not a referee result" in done.note and not done.referee_verified

    open_window = card()
    qc.note_held(open_window, ["OPTIONS"], trusted=True)
    qc.settle(open_window, 1_000, trusted=True, now_s=1.0)
    assert open_window.result is None

    missed = card()
    qc.note_held(missed, ["OPTIONS"], trusted=True)
    qc.note_held(missed, [], trusted=True)
    qc.note_held(missed, ["UP"], trusted=True)
    qc.settle(missed, 9_000, trusted=True, now_s=9.0)
    assert missed.result == "fail"

    quiet = card()
    qc.settle(quiet, 9_000, trusted=True, now_s=9.0)
    assert quiet.result == "expired" and "before the pattern was entered" in quiet.note

    blind = card()
    qc.note_held(blind, ["OPTIONS"], trusted=True)
    blind.unhealthy = True
    qc.settle(blind, 9_000, trusted=True, now_s=9.0)
    assert blind.result == "expired" and "may have missed presses" in blind.note

    untrusted = card()
    qc.note_held(untrusted, ["OPTIONS"], trusted=False)
    qc.settle(untrusted, 9_000, trusted=False, now_s=9.0)
    assert untrusted.result == "expired" and "may have missed presses" in untrusted.note

    ui = qc.MatchConsole(force=True, out=io.StringIO(), columns=78, rows=24, clock=lambda: 100.0)
    ui.show_challenge(phase="kickoff", label="game start", pattern=["OPTIONS", "CROSS"], issued_ms=0, deadline_ms=1_000)
    ui.observe_buttons(["OPTIONS"], trusted=False)
    ui._settle_locked(ui._card)
    assert ui._card.result == "expired"
    ui._sampler_ready = True
    ui.show_challenge(phase="kickoff", label="game start", pattern=["OPTIONS", "CROSS"], issued_ms=0, deadline_ms=1_000)
    ui.observe_buttons(["OPTIONS"], trusted=False)
    ui._settle_locked(ui._card)
    assert ui._card.result == "fail"


def test_show_challenge_writes_nothing_and_stays_fast():
    buf = io.StringIO()
    release = threading.Event()
    started = threading.Event()

    def getter():
        started.set()
        release.wait(2)
        return {"input": {"buttons": ["OPTIONS"]}}

    ui = qc.MatchConsole(force=True, out=buf, columns=78, rows=30, env={}, status_getter=getter, clock=lambda: 1.0)
    try:
        t0 = time.perf_counter()
        shown = ui.show_challenge(phase="kickoff", label="game start", pattern=["OPTIONS", "CROSS"],
                                  issued_ms=1_000, deadline_ms=91_000)
        assert shown is True
        assert time.perf_counter() - t0 < 0.05
        assert buf.getvalue() == ""
        ui.start()
        assert started.wait(1.0)
        t0 = time.perf_counter()
        assert ui.show_challenge(phase="halftime", label="second half", pattern=["OPTIONS", "SQUARE"],
                                 issued_ms=1_000, deadline_ms=91_000) is True
        assert time.perf_counter() - t0 < 0.05
    finally:
        release.set()
        ui.close()


def test_sampler_starts_only_after_a_challenge_is_posted():
    calls = []

    def getter():
        calls.append(1)
        return {"input": {"buttons": []}}

    ui = qc.MatchConsole(force=True, out=io.StringIO(), columns=78, rows=24,
                         env={"QORCAP_REDUCE_MOTION": "1"}, status_getter=getter)
    try:
        ui.start()
        time.sleep(0.35)
        assert calls == []
        ui.show_challenge(phase="end", label="4th quarter", pattern=["OPTIONS", "UP"],
                          issued_ms=0, deadline_ms=10**12)
        deadline = time.monotonic() + 1.5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls
    finally:
        ui.close()


def test_render_failure_falls_back_to_plain_text():
    class Boom(qc.MatchConsole):
        def _tick(self):
            raise RuntimeError("paint broke")

    buf = io.StringIO()
    ui = Boom(force=True, out=buf, columns=78, rows=24, env={})
    try:
        ui.start()
        ui._thread.join(2)
        assert ui.branded is False
        assert "QorCap console display stopped. Plain status follows." in buf.getvalue()
        assert ui.hook("later") is False
    finally:
        ui.close()


def test_say_hook_reprints_when_the_dashboard_closes():
    buf = io.StringIO()
    ui = qc.MatchConsole(force=True, out=buf, columns=78, rows=22, env={"QORCAP_REDUCE_MOTION": "1"})
    try:
        ui.start()
        assert ui.hook("FINAL on the scorebug") is True
        assert "\x1b[?1049l" not in buf.getvalue()
    finally:
        ui.close()
    tail = buf.getvalue().split("\x1b[?1049l", 1)[-1]
    assert "FINAL on the scorebug" in tail
    assert qc._SAY_HOOK is None


class _Api:
    def __init__(self):
        self.posted = []

    def get(self, path, timeout=3):
        return {"chain_head": "c" * 64, "segments": 7, "checkpoint_head": "d" * 64}

    def post(self, path, body=None, timeout=5):
        self.posted.append((path, body))
        return {"ok": True}


class _Client:
    def post_json(self, path, body):
        if path == "/v1/anchors":
            return {"type": "anchor"}
        return {"nonce": "ab" * 16, "pattern": ["OPTIONS", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE"],
                "issued_ms": 1_000, "deadline_ms": 91_000, "phase": body["phase"]}


class _Gate:
    def __init__(self):
        self.events = []

    def latest(self, after_ms=0):
        event = self.events[-1] if self.events else None
        return event if event is not None and event.t_ms > after_ms else None


class _Ev:
    t_ms = 10
    phase = "kickoff"
    quarter = 1

    def to_dict(self):
        return {"basis": "play_call", "phase": self.phase, "t_ms": self.t_ms, "quarter": self.quarter}


def test_official_live_shows_a_card_and_keeps_the_prompt(tmp_path, capsys):
    buf = io.StringIO()
    ui = qc.MatchConsole(force=True, out=buf, columns=78, rows=30, env={})
    beeps = []
    api = _Api()
    gate = _Gate()
    gate.events.append(_Ev())
    live = qs.OfficialLive("http://referee.test", tmp_path, "m1", "home", api, challenges=True, client=_Client(),
                           clock=lambda: 0.0, wall=lambda: 1_000_000, gate=lambda: gate,
                           beep=lambda: beeps.append(1), console=ui)
    live.step()
    out = capsys.readouterr().out
    assert "QORTIME CHALLENGE" not in out
    assert "QORTIME CHALLENGE" not in buf.getvalue()
    assert live.prompts[0].startswith("\n\n  >>> QORTIME CHALLENGE (game start):")
    assert "at the line, before the snap" in live.prompts[0]
    assert [path for path, _ in api.posted] == ["/challenge"]
    assert beeps == [1]
    assert ui._card.pattern == ["OPTIONS", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE"]
    saved = json.loads((tmp_path / "official_challenges.jsonl").read_text().splitlines()[0])
    assert saved["deadline_ms"] - saved["issued_ms"] == 90_000
    assert not (tmp_path / "challenge_results.jsonl").exists()
    ui.close()


def test_official_live_prints_the_prompt_when_the_card_is_unavailable(tmp_path, capsys):
    bundle = tmp_path / "plain"
    bundle.mkdir()
    gate = _Gate()
    gate.events.append(_Ev())
    ui = qc.MatchConsole(plain=True, out=io.StringIO(), env={})
    live = qs.OfficialLive("http://referee.test", bundle, "m1", "home", _Api(), challenges=True, client=_Client(),
                           clock=lambda: 0.0, wall=lambda: 1_000_000, gate=lambda: gate,
                           beep=lambda: None, console=ui)
    live.step()
    assert "QORTIME CHALLENGE" in capsys.readouterr().out
    assert "at the line, before the snap" in live.prompts[0]

    class Bad:
        def show_challenge(self, **kwargs):
            raise RuntimeError("display down")

    other = tmp_path / "bad"
    other.mkdir()
    gate = _Gate()
    gate.events.append(_Ev())
    live = qs.OfficialLive("http://referee.test", other, "m1", "home", _Api(), challenges=True, client=_Client(),
                           clock=lambda: 0.0, wall=lambda: 1_000_000, gate=lambda: gate,
                           beep=lambda: None, console=Bad())
    live.step()
    assert "QORTIME CHALLENGE" in capsys.readouterr().out


def test_plain_flag_parses_for_live_and_finalize():
    live = subprocess.run([sys.executable, str(HERE / "qorcap_match.py"), "--help"],
                          capture_output=True, text=True, check=False)
    assert live.returncode == 0 and "--plain" in live.stdout
    final = subprocess.run([sys.executable, str(HERE / "qorcap_match.py"), "finalize", "--plain", "--help"],
                           capture_output=True, text=True, check=False)
    assert final.returncode == 0, final.stderr


def _referee_ok(pattern, presses):
    """Copy of the shape of qor_receipt.challenge.response_start + verify_response's pattern check."""
    for i, b in enumerate(presses):
        if b == "OPTIONS" and presses[i:i + len(pattern)] == pattern:
            return True
    return False


def test_card_pass_matches_the_referee_pattern_rule():
    import random

    rng = random.Random(20261025)
    alphabet = ["CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT", "OPTIONS", "R2"]
    for _ in range(4000):
        body = [rng.choice(alphabet[:8]) for _ in range(4)]
        pattern = ["OPTIONS"] + body
        presses = [rng.choice(alphabet) for _ in range(rng.randint(0, 14))]
        if rng.random() < 0.3:
            k = rng.randint(0, len(presses))
            presses[k:k] = pattern
        c = qc.ChallengeCard("end", "4th quarter", pattern, 0, 90_000)
        for n, b in enumerate(presses):
            qc.note_press(c, b, 1_000 + n * 300)
        assert (c.matched == len(pattern)) == _referee_ok(pattern, presses), (pattern, presses)


# match-20261025-115533 (QorCap 0.3.34), rising edges from inputs.jsonl, ms after issue.
_HALFTIME = [(835, "OPTIONS"), (3070, "UP"), (3103, "RIGHT"), (3515, "SQUARE"), (3842, "LEFT"),
             (5121, "LEFT"), (5231, "SQUARE"), (8053, "LEFT"), (8341, "SQUARE"), (11203, "CIRCLE")]
_KICKOFF = [(1420, "OPTIONS"), (2543, "TRIANGLE"), (3010, "UP"), (3542, "LEFT"), (4271, "SQUARE"),
            (6595, "LEFT"), (6676, "UP"), (6731, "SQUARE"), (9662, "CIRCLE")]


def _edges(rows, issued):
    return [{"seq": i + 1, "t_ms": issued + t, "button": b} for i, (t, b) in enumerate(rows)]


def test_recorded_halftime_shows_the_break_instead_of_pass():
    issued = 1_792_947_832_685
    c = qc.ChallengeCard("halftime", "second half", ["OPTIONS", "UP", "SQUARE", "LEFT", "SQUARE"], issued, issued + 90_000)
    edges = _edges(_HALFTIME, issued)
    # Delivered in two polls; the second repeats the ring and must not double count.
    assert qc.note_edges(c, edges[:3], trusted=True)
    assert c.wrong == "RIGHT" and c.rolled and c.matched == 0
    line = qc.press_feedback(c)
    assert line == ("Extra RIGHT (d-pad roll): not counted. Redo OPTIONS + all at next snap.", "red")
    assert len(line[0]) <= 72, "fits a 78-column card without clipping"
    qc.note_edges(c, edges, trusted=True)
    assert c.matched == 0 and c.result is None
    qc.settle(c, issued + 90_001, trusted=True, now_s=1.0)
    assert c.result == "fail"
    assert not _referee_ok(c.pattern, [b for _, b in _HALFTIME])


def test_recorded_kickoff_locks_after_the_answer():
    issued = 1_792_947_450_099
    c = qc.ChallengeCard("kickoff", "game start", ["OPTIONS", "TRIANGLE", "UP", "LEFT", "SQUARE"], issued, issued + 90_000)
    qc.note_edges(c, _edges(_KICKOFF, issued), trusted=True)
    assert c.matched == 5
    assert c.presses == ["OPTIONS", "TRIANGLE", "UP", "LEFT", "SQUARE"], "presses after the answer are not counted"
    qc.settle(c, issued + 5_000, trusted=True, now_s=1.0)
    assert c.result == "pass" and c.note.startswith("Got it.")


def test_short_taps_between_polls_still_count_and_old_edges_are_ignored():
    c = qc.ChallengeCard("end", "4th quarter", ["OPTIONS", "LEFT", "DOWN"], 10_000, 100_000)
    # A press before issue and an edge-only button do not count; held list is empty at every poll.
    qc.note_edges(c, [{"seq": 7, "t_ms": 9_000, "button": "OPTIONS"}], trusted=True)
    qc.note_held(c, [], trusted=True)
    assert c.matched == 0 and c.edges
    qc.note_edges(c, [{"seq": 8, "t_ms": 11_000, "button": "OPTIONS"}, {"seq": 9, "t_ms": 11_050, "button": "MUTE"},
                      {"seq": 10, "t_ms": 11_400, "button": "LEFT"}], trusted=True)
    qc.note_held(c, ["LEFT"], trusted=True)  # the held path must not count LEFT a second time
    assert c.matched == 2 and qc.press_feedback(c) == ("Got LEFT. Next: DOWN. Press each button once.", "ok")
    qc.note_edges(c, [{"seq": 30, "t_ms": 12_000, "button": "DOWN"}], trusted=True)
    assert c.unhealthy, "a seq gap means the ring overflowed between polls"
    assert c.matched == 3


def test_observe_buttons_prefers_recent_presses():
    ui = qc.MatchConsole(force=True, out=io.StringIO(), columns=78, rows=24, clock=lambda: 100.0)
    ui.show_challenge(phase="end", label="4th quarter", pattern=["OPTIONS", "DOWN"], issued_ms=0, deadline_ms=10**12)
    ui.observe_buttons([], trusted=False, recent=[{"seq": 1, "t_ms": 5, "button": "OPTIONS"},
                                                 {"seq": 2, "t_ms": 90, "button": "DOWN"}])
    assert ui._card.matched == 2
