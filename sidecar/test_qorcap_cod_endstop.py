"""Practice CoD auto stop (qorcap_cod_endstop): debounce, arming, tail, and the stop wiring.

Pure logic with fake reads and an injected clock, plus the committed 720p end-screen fixtures.
No capture card, no QorCap process.
"""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import pytest

import qorcap_cod as C
import qorcap_cod_endstop as ES
import qorcap_match as qm

FIX = Path(__file__).resolve().parent / "fixtures" / "cod"


def word(reason, confirm=True, ok=True, calibrated=True):
    return NS(ok=ok, calibrated=calibrated, confirm=confirm, reason=reason, home=None, away=None)


def row(tag="Qortrola30", ok=True):
    return NS(ok=ok, calibrated=ok, player_tag=tag, kills=10, deaths=5, assists=None, confirms=None)


NOT_END = NS(ok=False, calibrated=False, confirm=False, reason="not_present", home=None, away=None)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


ARMED = 200_000  # well past the 120 s arm
# The sustained path alone (the splash path would confirm these runs earlier).
SUSTAIN = ES.Config(splash=False)


def feed(es, start_ms, step_ms, reads, owner=None):
    """Feed reads at a fixed step. Returns the index that confirmed, or None."""
    hit = None
    for i, rd in enumerate(reads):
        if es.observe(start_ms + i * step_ms, rd, owner(i) if callable(owner) else owner, {"seq": 100 + i}) and hit is None:
            hit = i
    return hit


def armed_watcher(cfg=None, clock=None):
    es = ES.EndStop(cfg or ES.Config(), clock=clock or Clock(), wall=lambda: 1_794_000_000_000)
    es.observe(0, NOT_END)  # first frame starts the arm clock
    return es


def test_flag_lane_and_game_gate():
    on = {"QORCAP_AUTOCLOSE": "1", "QORCAP_LANE": "practice"}
    assert ES.enabled("cod", on)
    assert not ES.enabled("madden", on)
    assert not ES.enabled("cod", {"QORCAP_AUTOCLOSE": "1"})
    assert not ES.enabled("cod", {"QORCAP_LANE": "practice"})
    assert not ES.enabled("cod", {"QORCAP_AUTOCLOSE": "0", "QORCAP_LANE": "practice"})
    assert not ES.enabled("cod", {"QORCAP_AUTOCLOSE": "1", "QORCAP_LANE": "official"})
    assert not ES.enabled("cod", {})


def test_config_defaults_and_clamps():
    c = ES.config_from_env({})
    assert (c.frames, c.solo_frames, c.span_ms, c.solo_span_ms, c.arm_ms, c.tail_s) == (8, 16, 3000, 6000, 120_000, 12.0)
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_TAIL_S": "3"}).tail_s == 10.0
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_TAIL_S": "99"}).tail_s == 15.0
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_TAIL_S": "13.5"}).tail_s == 13.5
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_TAIL_S": "nan"}).tail_s == 12.0
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_TAIL_S": "x"}).tail_s == 12.0
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_FRAMES": "1"}).frames == 3
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_FRAMES": "12"}).frames == 12
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_ARM_S": "-5"}).arm_ms == 0
    assert ES.config_from_env({"QORCAP_AUTOCLOSE_ARM_S": "300"}).arm_ms == 300_000


@pytest.mark.parametrize("result", ["victory", "defeat"])
def test_end_word_with_owner_row_confirms_after_frames_and_span(result):
    es = armed_watcher(SUSTAIN)
    # 8 reads 500 ms apart span 3.5 s; the 7th read (3.0 s span) is still short of 8 frames.
    hit = feed(es, ARMED, 500, [word(result)] * 10, owner=row())
    assert hit == 7
    c = es.confirmed
    assert c["result"] == result.upper() and c["owner_row_seen"] is True
    assert c["reader"] == "cod_end_screen+owner_row" and c["consecutive"] == 8
    assert c["confirm_frame_seq"] == 107 and c["first_frame_seq"] == 100 and c["confirm_t_ms"] == ARMED + 3500


@pytest.mark.parametrize("result", ["victory", "defeat"])
def test_end_word_without_owner_row_needs_twice_the_run(result):
    es = armed_watcher(SUSTAIN)
    hit = feed(es, ARMED, 400, [word(result)] * 20)
    # 16 reads and >= 6 s: read index 15 spans 6.0 s.
    assert hit == 15
    assert es.confirmed["owner_row_seen"] is False and es.confirmed["consecutive"] == 16


def test_owner_row_on_one_frame_of_the_run_is_enough():
    es = armed_watcher(SUSTAIN)
    hit = feed(es, ARMED, 500, [word("victory")] * 10, owner=lambda i: row() if i == 5 else None)
    assert hit == 7 and es.confirmed["owner_row_seen"]


def test_another_players_row_does_not_corroborate():
    es = armed_watcher(SUSTAIN)
    assert feed(es, ARMED, 500, [word("victory")] * 10, owner=row(tag="Chan")) is None


def test_sustained_path_alternating_flicker_never_confirms():
    es = armed_watcher(SUSTAIN)
    reads = [word("defeat") if i % 2 == 0 else NOT_END for i in range(80)]
    assert feed(es, ARMED, 250, reads, owner=row()) is None
    assert es.confirmed is None and es.runs_broken >= 39


def test_one_non_end_frame_restarts_the_run():
    es = armed_watcher(SUSTAIN)
    reads = [word("victory")] * 7 + [NOT_END] + [word("victory")] * 7
    assert feed(es, ARMED, 500, reads, owner=row()) is None


def test_killcam_tab_and_gameplay_reads_do_not_count():
    """Killcam / gameplay / mid-match Tab board: the end reader has no confirmed end word.

    A Tab board can still show Qortrola30's row; without the word that is not a trigger.
    """
    es = armed_watcher()
    killcam = NOT_END
    tab = NS(ok=False, calibrated=False, confirm=False, reason="not_present", home=None, away=None)
    assert feed(es, ARMED, 250, [killcam] * 100 + [tab] * 100, owner=row()) is None
    assert es.positives == 0 and es.confirmed is None


def test_unconfirmed_or_uncalibrated_word_does_not_count():
    es = armed_watcher()
    for rd in (word("victory", confirm=False), word("defeat", calibrated=False), word("defeat", ok=False),
               word("final"), word(None)):
        assert feed(es, ARMED, 500, [rd] * 30, owner=row()) is None
    assert es.positives == 0


def test_switching_word_restarts_the_run():
    es = armed_watcher(SUSTAIN)
    reads = [word("victory")] * 6 + [word("defeat")] * 6
    assert feed(es, ARMED, 500, reads, owner=row()) is None


def test_gap_longer_than_three_seconds_restarts_the_run():
    es = armed_watcher(SUSTAIN)
    for i in range(6):
        es.observe(ARMED + i * 500, word("victory"), row())
    es.observe(ARMED + 2500 + 3001, word("victory"), row())  # gap > 3 s
    assert es.confirmed is None and es._n == 1


def test_repeated_or_earlier_timestamp_restarts_the_run():
    es = armed_watcher(SUSTAIN)
    for i in range(6):
        es.observe(ARMED + i * 500, word("victory"), row())
    es.observe(ARMED + 2500, word("victory"), row())
    assert es._n == 1


def test_lobby_end_word_before_arm_does_not_confirm():
    es = armed_watcher(SUSTAIN)
    # A VICTORY screen left from the previous match, inside the first 120 s.
    assert feed(es, 1000, 500, [word("victory")] * 200, owner=row()) is None
    # Once armed, a fresh run is needed; the pre-arm reads do not carry over.
    es2 = armed_watcher(SUSTAIN)
    hit = feed(es2, 116_000, 500, [word("victory")] * 20, owner=row())
    # first armed read is at 120_000 (index 8); 8 reads later is index 15
    assert hit == 15 and es2.confirmed["first_t_ms"] == 120_000


def test_arm_zero_allows_an_immediate_run():
    es = ES.EndStop(ES.Config(arm_ms=0, splash=False), clock=Clock())
    assert feed(es, 0, 500, [word("defeat")] * 10, owner=row()) == 7


def test_tail_timing_with_injected_clock():
    clk = Clock()
    es = armed_watcher(ES.Config(tail_s=12.0), clock=clk)
    assert not es.stop_due()
    feed(es, ARMED, 500, [word("defeat")] * 8, owner=row())
    assert es.confirmed is not None
    assert not es.stop_due()
    clk.now += 11.999
    assert not es.stop_due() and es.tail_elapsed_s() == 11.999
    clk.now += 0.001
    assert es.stop_due()


def test_confirmed_is_final_and_later_frames_do_not_move_it():
    clk = Clock()
    es = armed_watcher(clock=clk)
    feed(es, ARMED, 500, [word("victory")] * 8, owner=row())
    first = dict(es.confirmed)
    clk.now += 5
    feed(es, ARMED + 10_000, 500, [NOT_END] * 5 + [word("defeat")] * 30, owner=row())
    assert es.confirmed == first
    clk.now += 7
    assert es.stop_due()


def _state_with(es, final=None):
    st = {"cod_end_stop": es}
    if final is not None:
        st["final"] = final
    return st


def test_stop_reason_wiring_adds_time_only():
    clk = Clock()
    es = armed_watcher(clock=clk)
    said = []
    st = _state_with(es)
    # Off: unchanged.
    assert qm.cod_end_stop_reason({}, "final") == "final"
    assert qm.cod_end_stop_reason({}, None) is None
    # On but not confirmed: unchanged.
    assert qm.cod_end_stop_reason(st, None) is None
    assert qm.cod_end_stop_reason(st, "final") == "final"
    feed(es, ARMED, 500, [word("defeat")] * 8, owner=row())
    # Confirmed, tail running: keep capturing, and an older final stop waits.
    assert qm.cod_end_stop_reason(st, None, say=said.append) is None
    assert said and "DEFEAT end screen confirmed" in said[0]
    assert qm.cod_end_stop_reason(st, "final") is None
    # Enter and capture errors still stop at once.
    assert qm.cod_end_stop_reason(st, "enter") == "enter"
    assert qm.cod_end_stop_reason(st, "disk_full") == "disk_full"
    clk.now += 12
    # No final posted: the existing post_final_tail reason.
    assert qm.cod_end_stop_reason(st, None) == "post_final_tail"
    assert st["cod_end_stop_fired"] is True
    st2 = _state_with(es, final={"type": "final", "basis": "end_menu", "method": "end_menu_read"})
    assert qm.cod_end_stop_reason(st2, None) == "final"
    assert len(said) == 1


def test_shutdown_body_note_is_optional():
    assert qm.shutdown_body("enter") == {"reason": "enter"}
    assert qm.shutdown_body("post_final_tail", "") == {"reason": "post_final_tail"}
    assert qm.shutdown_body("post_final_tail", "a note") == {"reason": "post_final_tail", "note": "a note"}


def test_log_note_and_sidecar_file(tmp_path):
    clk = Clock()
    es = armed_watcher(SUSTAIN, clock=clk)
    feed(es, ARMED, 500, [word("victory")] * 8, owner=row())
    note = es.log_note("post_final_tail")
    assert note.isascii() and "\n" not in note and len(note) <= 240
    assert "cod end screen VICTORY" in note and "path sustained" in note and "reads 8" in note and "frame_seq 107" in note
    low = note.lower()
    for needle in ("stop reason:", "ctrl+c", "launcher process", "recorder pipe broken", "--duration reached"):
        assert needle not in low  # qm.stop_reason_from_log must not read this line as a stop cause
    assert qm.stop_reason_from_log("stop note: " + note) == "not_recorded"
    es.record(tmp_path, stop_reason="post_final_tail", stop_wall_ms=123, fired=True)
    doc = json.loads((tmp_path / ES.FILE_NAME).read_text())
    assert doc["schema"] == "qorcap-cod-end-stop/v0" and doc["info_only"] is True and doc["auto_stopped"] is True
    assert doc["confirmed"]["result"] == "VICTORY" and doc["config"]["tail_s"] == 12.0
    es.record(tmp_path / "missing", stop_reason="enter", stop_wall_ms=None, fired=False)
    assert not (tmp_path / "missing").exists()


class _FixedEnd:
    def __init__(self, rd):
        self.rd = rd

    def read(self, img):
        return self.rd


class _NoHud:
    def read(self, img):
        return []


def test_runner_hook_sees_reads_and_cannot_break_the_frame_loop():
    seen = []
    r = C.CodSetRunner(lambda b: {}, hud=_NoHud(), end=_FixedEnd(word("victory")), log_path=None, side=None)
    r.end_watch = lambda t, end, owner, src, hud, img: seen.append((t, end.reason, owner is not None))
    r.on_frame(1000, None)
    assert seen and seen[0][0] == 1000 and seen[0][1] == "victory"

    def boom(*a):
        raise RuntimeError("watcher bug")
    r2 = C.CodSetRunner(lambda b: {}, hud=_NoHud(), end=_FixedEnd(NOT_END), log_path=None, side=None)
    r2.end_watch = boom
    r2.on_frame(1000, None)  # must not raise
    assert r2.stats["frames"] == 1


def _fx(name):
    img = cv2.imread(str(FIX / name))
    assert img is not None, name
    return img


def test_committed_fixtures_victory_confirms_mid_match_does_not():
    """Real 720p frames through the real readers. Repeating one frame models a held end screen."""
    from qorcap_cod_hud import read_end, read_owner_row
    board = _fx("end_173922_019.jpg")   # VICTORY header + Qortrola30's row
    splash = _fx("end_173922_017.jpg")  # VICTORY splash, no board
    mid = _fx("mid_173922_120s.jpg")    # gameplay, 120 s in
    pre = [_fx("end_173922_007.jpg"), _fx("end_173922_010.jpg")]  # transition frames, no word

    es = armed_watcher(SUSTAIN)
    for i in range(20):
        es.observe(ARMED + i * 250, read_end(mid), read_owner_row(mid))
    for f in pre:
        es.observe(ARMED + 6000, read_end(f), read_owner_row(f))
    assert es.confirmed is None and es.positives == 0

    es = armed_watcher(SUSTAIN)
    t = ARMED
    for i in range(6):
        es.observe(t, read_end(splash), read_owner_row(splash)); t += 500
    assert es.confirmed is None
    for i in range(4):
        es.observe(t, read_end(board), read_owner_row(board)); t += 500
    assert es.confirmed is not None and es.confirmed["result"] == "VICTORY" and es.confirmed["owner_row_seen"]

    es = armed_watcher(SUSTAIN)
    for i in range(20):
        es.observe(ARMED + i * 250, read_end(board) if i % 2 == 0 else read_end(mid), read_owner_row(board))
    assert es.confirmed is None


def test_attach_is_gated_and_reader_gets_the_hook():
    st = {}
    assert qm.attach_cod_end_stop(st, "cod", environ={}) is None and "cod_end_stop" not in st
    assert qm.attach_cod_end_stop(st, "madden", environ={"QORCAP_AUTOCLOSE": "1", "QORCAP_LANE": "practice"}) is None
    said = []
    es = qm.attach_cod_end_stop(st, "cod", environ={"QORCAP_AUTOCLOSE": "1", "QORCAP_LANE": "practice",
                                                    "QORCAP_AUTOCLOSE_TAIL_S": "14"}, say=said.append)
    assert es is st["cod_end_stop"] and es.cfg.tail_s == 14.0 and "14 s" in said[0]


# ---------------------------------------------------------------- splash path (match-20261111-165658)

def hud_event(region="killfeed"):
    return [NS(region=region, ok=True, calibrated=True), NS(region="crosshair", ok=False, calibrated=False)]


NO_HUD = [NS(region=r, ok=False, calibrated=False) for r in ("crosshair", "hitmarker", "kill_popup", "killfeed", "pause", "scoreboard")]
READ_MS = 385  # ~2.6 reads per second, the rate in cod_reader_stats.json of that match


def replay(es, frames, img=None):
    """frames: list of (t_ms, end, hud). Returns the t_ms that confirmed, or None."""
    for t, end, hud in frames:
        if es.observe(t, end, None, {"seq": t // READ_MS}, hud, img):
            return t
    return None


def match_165658(after_ms=40_000, hud_after=None):
    """Arm at 0, gameplay to 182.1 s, 2 confirmed reads at 182.486 / 183.222 s, then board/menu frames."""
    frames = [(t, NOT_END, NO_HUD) for t in range(0, 182_100, READ_MS)]
    frames += [(182_486, word("victory"), NO_HUD), (183_222, word("victory"), NO_HUD)]
    t = 183_222 + READ_MS
    while t < 183_222 + after_ms:
        frames.append((t, NOT_END, hud_after(t) if hud_after else NO_HUD))
        t += READ_MS
    return frames


def test_regression_match_165658_short_splash_then_board_stops():
    clk = Clock()
    es = ES.EndStop(ES.Config(), clock=clk, owner_reader=lambda img: row(ok=False))
    hit = replay(es, match_165658(), img=object())
    assert hit is not None
    c = es.confirmed
    assert c["path"] == "splash+quiet" and c["result"] == "VICTORY" and c["consecutive"] == 2
    assert c["span_ms"] == 736 and c["owner_row_seen"] is False and c["hud_events_in_candidate"] == 0
    assert 8000 <= hit - 183_222 < 8000 + READ_MS
    assert es.owner_reads_attempted > 0  # the watcher tried the row on the board frames
    clk.now += 12
    assert es.stop_due()  # tail from confirmation: about 20 s after the word, not 65 s


def test_splash_then_owner_row_on_the_board_confirms_at_once():
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=lambda img: row())
    hit = replay(es, match_165658(), img=object())
    assert hit == 183_222 + READ_MS and es.confirmed["path"] == "splash+owner_row"


def test_splash_then_word_again_on_the_board_header_confirms():
    frames = match_165658(after_ms=3000)
    frames.append((183_222 + 3000, word("victory"), NO_HUD))
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=None)
    assert replay(es, frames) == 186_222 and es.confirmed["path"] == "splash+word_again"


def test_splash_followed_by_gameplay_hud_does_not_stop():
    """A word misread mid-match, then play continues: feed / hit events keep resetting the quiet count."""
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=None)
    frames = match_165658(after_ms=60_000, hud_after=lambda t: hud_event() if (t // 1000) % 5 == 0 else NO_HUD)
    assert replay(es, frames) is None
    assert es.candidates_cancelled.get("window_expired") == 1


def test_killcam_with_hud_after_splash_only_delays_the_stop():
    """A final killcam right after the splash may replay hitmarkers / feed. It restarts the quiet count."""
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=None)
    killcam_end = 183_222 + 6000
    frames = match_165658(after_ms=30_000, hud_after=lambda t: hud_event("hitmarker") if t < killcam_end and (t // 1000) % 2 == 0 else NO_HUD)
    hit = replay(es, frames)
    assert hit is not None and es.confirmed["hud_events_in_candidate"] > 0
    assert hit - killcam_end >= 8000 - 2 * READ_MS


def test_killcam_without_an_end_word_never_opens_a_candidate():
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=lambda img: row())
    frames = [(t, NOT_END, hud_event() if t % 3 == 0 else NO_HUD) for t in range(0, 400_000, READ_MS)]
    assert replay(es, frames, img=object()) is None and es.candidates == 0 and es.owner_reads_attempted == 0


def test_single_read_splash_does_not_open_a_candidate():
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=lambda img: row())
    frames = [(t, NOT_END, NO_HUD) for t in range(0, 182_100, READ_MS)]
    frames.append((182_486, word("defeat"), NO_HUD))
    frames += [(t, NOT_END, NO_HUD) for t in range(182_486 + READ_MS, 240_000, READ_MS)]
    assert replay(es, frames, img=object()) is None and es.candidates == 0


def test_two_reads_further_apart_than_the_pair_window_do_not_open_a_candidate():
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=None)
    frames = [(182_000, word("defeat"), NO_HUD), (183_000, NOT_END, NO_HUD), (183_600, word("defeat"), NO_HUD)]
    frames += [(t, NOT_END, NO_HUD) for t in range(184_000, 230_000, READ_MS)]
    es.observe(0, NOT_END)
    assert replay(es, frames) is None and es.candidates == 0


def test_pre_arm_splash_does_not_stop():
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=lambda img: row())
    frames = [(t, NOT_END, NO_HUD) for t in range(0, 30_000, READ_MS)]
    frames += [(30_100, word("victory"), NO_HUD), (30_500, word("victory"), NO_HUD)]
    frames += [(t, NOT_END, NO_HUD) for t in range(30_900, 119_000, READ_MS)]
    assert replay(es, frames, img=object()) is None and es.candidates == 0


def test_word_switch_cancels_the_candidate():
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=None)
    frames = match_165658(after_ms=2000)
    frames.append((185_300, word("defeat"), NO_HUD))
    frames += [(t, NOT_END, NO_HUD) for t in range(185_700, 230_000, READ_MS)]
    assert replay(es, frames) is None
    assert es.candidates_cancelled.get("other_word") == 1


def test_splash_record_and_note_name_the_path_and_follow_up(tmp_path):
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=None, wall=lambda: 1)
    replay(es, match_165658())
    note = es.log_note("post_final_tail")
    assert len(note) <= 240 and note.isascii() and "\n" not in note
    assert "path splash+quiet" in note and "follow_up quiet after" in note and "then post_final_tail" in note
    assert qm.stop_reason_from_log("stop note: " + note) == "not_recorded"
    assert "8.0 s without gameplay HUD" in es.describe() or "s without gameplay HUD" in es.describe()
    es.record(tmp_path, stop_reason="post_final_tail", stop_wall_ms=5, fired=True)
    doc = json.loads((tmp_path / ES.FILE_NAME).read_text())
    assert doc["confirmed"]["path"] == "splash+quiet" and doc["confirmed"]["follow_up"] == "quiet"
    assert doc["splash_candidates"] == 1 and doc["config"]["quiet_ms"] == 8000 and doc["config"]["window_ms"] == 30_000


def test_gameplay_hud_event_ignores_stubs_and_the_board_hook():
    assert not ES.gameplay_hud_event(NO_HUD)
    assert not ES.gameplay_hud_event([NS(region="scoreboard", ok=True, calibrated=True)])
    assert not ES.gameplay_hud_event([NS(region="killfeed", ok=True, calibrated=False)])
    for r in ("hitmarker", "kill_popup", "killfeed"):
        assert ES.gameplay_hud_event([NS(region=r, ok=True, calibrated=True)])


def test_owner_reader_error_is_not_a_read():
    def boom(img):
        raise RuntimeError("reader bug")
    es = ES.EndStop(ES.Config(), clock=Clock(), owner_reader=boom)
    hit = replay(es, match_165658(), img=object())
    assert hit is not None and es.confirmed["path"] == "splash+quiet"


def test_committed_fixtures_splash_then_real_board_confirms_by_owner_row():
    """Real frames: two VICTORY splash reads, then the real end board image; the watcher reads the row itself."""
    from qorcap_cod_hud import read_end
    splash = _fx("end_173922_017.jpg")
    board = _fx("end_173922_019.jpg")
    # The board frame's end read is replaced by NOT_END, so only the watcher's own row read can confirm.
    es = ES.EndStop(ES.Config(), clock=Clock())
    es.observe(0, NOT_END)
    assert not es.observe(ARMED, read_end(splash), None, None, [], splash)
    assert not es.observe(ARMED + 400, read_end(splash), None, None, [], splash)
    assert es.observe(ARMED + 800, NOT_END, None, None, [], board)
    assert es.confirmed["path"] == "splash+owner_row"
