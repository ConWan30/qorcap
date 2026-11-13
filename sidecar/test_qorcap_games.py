"""Game set picker and the Call of Duty correlation hooks.

Madden stays the default when --game is omitted. Call of Duty hooks fail closed
until a calibrated reader is wired. Words on the log are agreed, flagged a
mismatch, and not Counted.
"""
import inspect
import io
import json
import threading
from pathlib import Path

import qorcap_cod as C
import qorcap_games as G
import qorcap_match as M


def keys(seq):
    it = iter(seq)
    return lambda timeout: next(it, None)


def ns(**kw):
    base = {"game": None, "side": None, "role": None}
    base.update(kw)
    return type("N", (), base)()


class _Img:
    shape = (720, 1280, 3)
    size = 10

    def __getitem__(self, item):
        return self


def test_game_prompt_names_both_sets_and_the_frame_clock():
    text = "\n".join(M.game_prompt_lines())
    assert "press  M  or  C" in text
    assert "scorebug" in text
    assert "frame clock" in text
    assert "not Counted" in text
    assert "#####" in "\n".join(M.big_text("M = MADDEN"))


def test_omitted_game_stays_madden_and_keeps_the_side_prompt(tmp_path):
    f = tmp_path / G.GAME_FILE_NAME
    assert M.resolve_game(ns(), game_file=f) == ("madden", None)
    side = "\n".join(M.side_prompt_lines())
    assert "AWAY = the LEFT team, listed FIRST on the Madden scorebug." in side
    cod = "\n".join(M.cod_side_prompt_lines())
    assert "scorebug" not in cod
    assert M.resolve_side(ns(side="ask", game="cod"), side_file=tmp_path / "next-side.txt",
                          read_key=keys(["h", "\n"]), out=io.StringIO(), game="cod")[0] == "home"


def test_resolve_game_flag_then_file_then_prompt(tmp_path):
    f = tmp_path / G.GAME_FILE_NAME
    f.write_text("cod\n", encoding="utf-8")
    assert M.resolve_game(ns(game="madden"), game_file=f) == ("madden", "flag")
    assert f.exists()
    assert M.resolve_game(ns(game="ask"), game_file=f, read_key=keys([])) == ("cod", "file")
    assert M.resolve_game(ns(game="ask"), game_file=f, read_key=keys(["c", "\n"]), out=io.StringIO()) == ("cod", "prompt")
    assert M.resolve_game(ns(game="ask"), game_file=f, read_key=keys([]), out=io.StringIO()) == (None, None)


def test_launcher_asks_for_the_game_before_python():
    ps = (Path(__file__).resolve().parents[1] / "start-match.ps1").read_text(encoding="utf-8")
    src = Path(M.__file__).read_text(encoding="utf-8")
    launch = '& $vpy (Join-Path $here "sidecar\\qorcap_match.py")'
    # A one-shot next-game.txt still reaches Python as --game ask. The cards pass madden or cod.
    assert '$fwd = @("--game", "ask") + $fwd' in ps
    assert ps.index('"--game", "ask"') < ps.index(launch)
    assert ps.index("Show-QorCapGamePicker") < ps.index(launch)
    assert '"--game"' in ps
    assert "[Console]::IsInputRedirected" in ps
    assert "last-game.txt" in ps
    assert "[1]  MADDEN  (scorebug - home/away - end menu)" in ps
    assert "[2]  CALL OF DUTY  (HUD - killfeed - Qortrola30 end row)" in ps
    assert "Profile locked: CALL OF DUTY  cod_mw4_hud" in ps
    assert "Profile locked: MADDEN  madden27_standard" in ps
    assert "frame clock ON" in ps and "practice  ·  not sent" in ps
    assert "[Console]::ReadKey($true)" in ps
    assert "-ForegroundColor DarkRed" in ps
    assert "-ForegroundColor Yellow" not in ps and "-ForegroundColor DarkYellow" not in ps
    card = (Path(__file__).resolve().parents[1] / "Test Without Card.cmd").read_text(encoding="utf-8")
    assert "start-match.ps1" in card and "--test-pattern" in card
    assert 'choices=["madden", "cod", "ask"]' in src
    assert src.index("resolve_game(") < src.index("resolve_side(")
    assert 'if game == "cod":\n        start_cod_reader' in src
    assert "start_fake_scores" in src
    cod_branch = src.split('if game == "cod":', 1)[1].split("elif a.test_pattern:", 1)[0]
    assert "start_fake_scores" not in cod_branch


def test_recorded_set_fail_closed(tmp_path):
    assert G.recorded_set(tmp_path).set_id == "madden"
    (tmp_path / "bundle.json").write_text(json.dumps({"game": "cod_mw4"}), encoding="utf-8")
    assert G.recorded_set(tmp_path).set_id == "cod"
    (tmp_path / "bundle.json").unlink()
    path = tmp_path / G.GAME_SET_FILE
    path.write_text("{", encoding="utf-8")
    assert G.recorded_set(tmp_path).set_id == "closed"
    G.write_game_set(path, G.COD, "flag")
    assert G.recorded_set(tmp_path).set_id == "cod"
    G.write_game_set(path, G.MADDEN, None)
    assert G.recorded_set(tmp_path).set_id == "madden"


def test_stamp_keeps_madden_profile_and_closes_an_unread_cod_end(tmp_path):
    draft = {"match": {"game": "madden27", "scorebug_profile": "madden27_sunday_night"},
             "result": {"method": "last_scorebug_read_unconfirmed", "notes": []}}
    stamped = G.stamp_draft(draft, G.MADDEN)
    assert stamped["match"]["scorebug_profile"] == "madden27_sunday_night"
    cod = G.stamp_draft(draft, G.COD)
    assert cod["match"]["game"] == "cod_mw4"
    assert cod["match"]["scorebug_profile"] == "cod_mw4_hud"
    assert cod["result"]["final_confirmed"] is False
    assert G.COD_END_NOTE in cod["result"]["notes"]
    agreed = {"match": {}, "result": {"method": "end_menu_read", "final_confirmed": True, "notes": ["kept"]}}
    kept = G.stamp_draft(agreed, G.COD)
    assert kept["result"]["method"] == "end_menu_read"
    assert kept["result"]["final_confirmed"] is True
    closed = G.stamp_draft(draft, G.CLOSED)
    assert closed["match"]["game"] == "madden27"
    assert G.CLOSED_NOTE in closed["result"]["notes"]


def test_stub_end_screen_does_not_post():
    posted = []
    runner = C.CodSetRunner(posted.append)
    runner.on_frame(1_700_000_000_000, _Img())
    assert posted == []
    assert runner.final is None
    assert runner.stats["posted_final"] == 0


def test_correlate_and_chain_and_recoil():
    stamps = [1_000_000]
    assert C.correlate_pair(calibrated=False, screen_us=1_000_000, input_us=1_000_000, frame_stamps=stamps) == C.NOT_COUNTED
    assert C.correlate_pair(calibrated=True, screen_us=None, input_us=1_000_000, frame_stamps=stamps) == C.NOT_COUNTED
    assert C.correlate_pair(calibrated=True, screen_us=1_000_000, input_us=1_000_000, frame_stamps=[]) == C.NOT_COUNTED
    assert C.correlate_pair(calibrated=True, screen_us=1_000_000, input_us=None, frame_stamps=stamps) == C.MISMATCH
    assert C.correlate_pair(calibrated=True, screen_us=1_000_000, input_us=1_100_000, frame_stamps=stamps) == C.AGREED
    assert C.follows_status(calibrated=False, prior_status=C.AGREED, this_us=1, prior_us=1) == C.NOT_COUNTED
    assert C.follows_status(calibrated=True, prior_status=C.NOT_COUNTED, this_us=1, prior_us=1) == C.MISMATCH
    assert C.follows_status(calibrated=True, prior_status=C.AGREED, this_us=1_000_000, prior_us=1_200_000) == C.AGREED
    assert C.recoil_status({"type": "haptic", "t_us": 1, "confidence_label": "low", "flags": ["grip_jolt"]}, 1, stamps) == C.NOT_COUNTED
    assert C.scoreboard_status(calibrated=False, kills=1, assists=2, board_kills=1, board_assists=2) == C.NOT_COUNTED
    assert C.scoreboard_status(calibrated=True, kills=1, assists=None, board_kills=1, board_assists=0) == C.NOT_COUNTED
    assert C.scoreboard_status(calibrated=True, kills=4, assists=1, board_kills=4, board_assists=2) == C.MISMATCH
    assert C.scoreboard_status(calibrated=True, kills=4, assists=1, board_kills=4, board_assists=1) == C.AGREED


def test_info_only_and_session_tags(tmp_path):
    for row in C.INFO_ONLY.values():
        assert row["label"] == "INFO-ONLY"
        assert row["status"] == C.NOT_COUNTED
    assert C.INFO_ONLY["power_aware_timing"]["power"] == "unknown"
    assert "stick_turn_vs_camera" not in C.INFO_ONLY
    assert C.COUNTED_SIGNALS["stick_turn_vs_camera"]["label"] == "Counted"
    assert C.session_document()["grip"] == "off"
    empty = C.session_document()
    assert empty["power"] == "unknown"
    assert empty["controller_path"] == "unknown"
    assert empty["clock_sync"] == C.NOT_COUNTED
    assert "battery" not in json.dumps(empty)
    doc = C.session_document(
        frame_stamps=[1_000_000],
        inputs=[{"type": "header", "transport": "usb-hid", "wall_anchor_ms": 1},
                {"type": "in", "t_us": 1_000_000, "b": 0, "r2": 0}],
        wall_anchor_ms=1)
    assert doc["controller_path"] == "usb"
    assert doc["power"] == "unknown"
    assert doc["clock_sync"] == C.AGREED
    far = C.clock_sync_status([1_000_000], [{"type": "in", "t_us": 9_000_000}], 1)
    assert far == C.MISMATCH
    (tmp_path / "inputs.jsonl").write_text(
        json.dumps({"type": "header", "transport": "usb-hid", "wall_anchor_ms": 5}) + "\n"
        + json.dumps({"type": "status", "t_us": 10, "state": "connected", "detail": "DualSense (USB)"}) + "\n",
        encoding="utf-8")
    written = C.attach_session(tmp_path)
    assert written["controller_path"] == "usb"
    assert not (tmp_path / "bundle.json").exists()
    (tmp_path / "bundle.json").write_text("{}\n", encoding="utf-8")
    C.attach_session(tmp_path)
    meta = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
    assert meta["clock_sync"] == C.NOT_COUNTED
    assert meta["power"] == "unknown"
    assert meta["game"] == "cod_mw4"
    assert not (tmp_path / "receipt.json").exists()
    session = json.loads((tmp_path / "cod_session.json").read_text(encoding="utf-8"))
    assert session["schema"] == "qorcap-cod-session/v0"
    assert "not a receipt field" in session["note"]


def test_long_input_log_keeps_the_wall_anchor(tmp_path):
    path = tmp_path / "inputs.jsonl"
    header = json.dumps({"type": "header", "transport": "usb-hid", "wall_anchor_ms": 42})
    pad = json.dumps({"type": "in", "t_us": 1, "b": 0, "r2": 0})
    with path.open("w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for _ in range(8000):
            fh.write(pad + "\n")
        fh.write(json.dumps({"type": "in", "t_us": 2_000_000, "b": 0, "r2": 200}) + "\n")
    loaded = C.load_signal_files(tmp_path)
    assert loaded["wall_anchor_ms"] == 42
    assert any(isinstance(r, dict) and r.get("type") == "header" for r in loaded["inputs"])


def _scripted(reads):
    pending = list(reads)

    class End:
        def read(self, img):
            return pending.pop(0)

    return End()


def _scored(reason, home, away, *, confirm=False, ok=True, calibrated=True):
    return C.RegionRead("end_screen", "read", ok, reason, calibrated, home=home, away=away, confirm=confirm)


def _blank_end():
    return C.RegionRead("end_screen", "not_read", False, "not_present", False)


def _run_end(reads, side="home"):
    """Feed scripted end reads. ``side`` is the side chosen at the start.

    The default is home, so a victory with home ahead can still confirm.
    Pass ``side=None`` when the test is about a missing side.
    """
    posted = []
    runner = C.CodSetRunner(posted.append, hud=type("H", (), {"read": lambda self, img: []})(),
                            end=_scripted(rd for _t, rd in reads), side=side)
    for t, _rd in reads:
        runner.on_frame(t, _Img())
    return posted


def test_end_screen_bar_needs_distinct_times_and_resets():
    """The match window counts strictly later timestamps of one result and one score.

    A repeated timestamp, a gap longer than the bar, a non-end frame, or a
    different result or score starts over. Scoreboard digits are not the pair.
    """
    same = _scored("victory", 3, 1, confirm=True)
    assert _run_end([(0, same), (0, same), (0, same), (3000, same)]) == []
    assert _run_end([(0, same), (1000, same), (4001, same)]) == []
    held = _run_end([(0, same), (1000, same), (4000, same)])
    assert len(held) == 1 and held[0]["t_ms"] == 0 and held[0]["home"] == 3 and held[0]["away"] == 1
    assert held[0]["visual_confirmed"] is True and held[0]["final_confirmed"] is False

    restarted = _run_end([
        (0, same),
        (1000, same),
        (1500, _blank_end()),
        (2000, same),
        (3000, same),
        (5000, same),
    ])
    assert len(restarted) == 1
    assert restarted[0]["t_ms"] == 2000
    assert restarted[0]["end_screen"]["until_ms"] == 5000

    gapped = _run_end([(0, same), (1000, same), (10000, same), (11000, same), (14000, same)])
    assert len(gapped) == 1 and gapped[0]["t_ms"] == 10000

    # Home is behind, so this DEFEAT matches the side and can still confirm.
    defeat = _scored("defeat", 1, 3, confirm=True)
    switched = _run_end([(0, same), (1000, same), (4000, defeat), (5000, defeat), (8000, defeat)])
    assert len(switched) == 1 and switched[0]["t_ms"] == 4000
    assert switched[0]["visual_confirmed"] is True and switched[0]["final_confirmed"] is False
    assert switched[0]["home"] == 1 and switched[0]["away"] == 3

    other = _scored("victory", 4, 1, confirm=True)
    rescored = _run_end([(0, same), (1000, same), (2000, other), (3000, other), (5000, other)])
    assert len(rescored) == 1 and rescored[0]["home"] == 4 and rescored[0]["t_ms"] == 2000

    quiet = _scored("victory", 3, 1, confirm=False)
    mixed = _run_end([(0, same), (1000, same), (4000, quiet), (5000, quiet), (8000, quiet)])
    assert len(mixed) == 1 and mixed[0]["t_ms"] == 4000 and mixed[0]["final_confirmed"] is False

    board = C.RegionRead("scoreboard", "read", True, "scoreboard_ka", True, board_kills=90, board_assists=12)
    posted = []

    class Hud:
        def read(self, img):
            return [board]

    class Presence:
        def read(self, img):
            return C.RegionRead("end_screen", "read", True, "defeat", True, confirm=True)

    runner = C.CodSetRunner(posted.append, hud=Hud(), end=Presence())
    for t in (0, 1000, 4000):
        runner.on_frame(t, _Img())
    assert posted == []
    assert runner.final is None


def test_defeat_ahead_of_our_side_is_not_confirmed():
    """Once home and away exist, the word has to match the side chosen at the start.

    DEFEAT with that side ahead does not confirm. VICTORY with that side behind
    does not confirm. A tie does not confirm. A missing side does not confirm
    either word. A matching DEFEAT still confirms. The real reader still leaves
    home and away empty, so this rule does not post a final on the practice frames.
    """
    ahead = _scored("defeat", 8, 3, confirm=True)
    posted = _run_end([(0, ahead), (1500, ahead), (3000, ahead)], side="home")
    assert len(posted) == 1
    assert posted[0]["final_confirmed"] is False
    assert posted[0]["home"] == 8 and posted[0]["away"] == 3
    assert any("does not match the side" in note for note in posted[0]["notes"])

    behind = _scored("victory", 3, 8, confirm=True)
    victory_behind = _run_end([(0, behind), (1500, behind), (3000, behind)], side="home")
    assert victory_behind[0]["final_confirmed"] is False

    matched = _scored("defeat", 3, 8, confirm=True)
    ok = _run_end([(0, matched), (1500, matched), (3000, matched)], side="home")
    assert ok[0]["visual_confirmed"] is True and ok[0]["final_confirmed"] is False

    away_ahead = _scored("defeat", 3, 8, confirm=True)
    away = _run_end([(0, away_ahead), (1500, away_ahead), (3000, away_ahead)], side="away")
    assert away[0]["final_confirmed"] is False

    away_behind = _scored("defeat", 8, 3, confirm=True)
    away_ok = _run_end([(0, away_behind), (1500, away_behind), (3000, away_behind)], side="away")
    assert away_ok[0]["visual_confirmed"] is True and away_ok[0]["final_confirmed"] is False

    tie = _scored("victory", 4, 4, confirm=True)
    tied = _run_end([(0, tie), (1500, tie), (3000, tie)], side="home")
    assert tied[0]["final_confirmed"] is False

    noside = _run_end([(0, matched), (1500, matched), (3000, matched)], side=None)
    assert noside[0]["final_confirmed"] is False
    assert any("not on this read" in note for note in noside[0]["notes"])

    assert C.end_word_fits_side("defeat", 8, 3, "home") is False
    assert C.end_word_fits_side("defeat", 3, 8, "home") is True
    assert C.end_word_fits_side("victory", None, None, "home") is None
    assert C.side_from_bundle(None) is None


def test_side_file_names_the_side_chosen_at_the_start(tmp_path):
    assert C.side_from_bundle(tmp_path) is None
    path = tmp_path / "side_choice.json"
    path.write_text("{", encoding="utf-8")
    assert C.side_from_bundle(tmp_path) is None
    path.write_text(json.dumps({"role": "away"}), encoding="utf-8")
    assert C.side_from_bundle(tmp_path) == "away"
    path.write_text(json.dumps({"role": "left"}), encoding="utf-8")
    assert C.side_from_bundle(tmp_path) is None
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert "side=side_from_bundle(bundle)" in src


def test_calibrated_end_screen_needs_three_frames(tmp_path):
    class End:
        def __init__(self, read):
            self.read = read

    posted = []
    runner = C.CodSetRunner(posted.append, hud=type("H", (), {"read": lambda self, img: []})(),
                            end=End(lambda img: C.RegionRead("end_screen", "read", True, "ok", True, home=3, away=1)),
                            log_path=tmp_path / "cod_signals.jsonl")
    runner.on_frame(10_000, _Img())
    runner.on_frame(11_000, _Img())
    assert posted == []
    runner.on_frame(13_000, _Img())
    assert len(posted) == 1
    assert posted[0]["basis"] == "end_menu"
    assert posted[0]["method"] == "end_menu_read"
    assert posted[0]["final_confirmed"] is False
    assert posted[0]["quarter"] == 4
    confirmed = []
    runner2 = C.CodSetRunner(confirmed.append, hud=type("H", (), {"read": lambda self, img: []})(),
                             end=End(lambda img: C.RegionRead("end_screen", "read", True, "ok", True,
                                                             home=3, away=1, confirm=True)))
    for t in (0, 1000, 3000):
        runner2.on_frame(t, _Img())
    assert confirmed[0]["visual_confirmed"] is True and confirmed[0]["final_confirmed"] is False
    quiet = []
    runner3 = C.CodSetRunner(quiet.append, hud=type("H", (), {"read": lambda self, img: []})(),
                             end=End(lambda img: C.RegionRead("end_screen", "read", True, "ok", False, home=9, away=9)))
    for t in (0, 1000, 5000):
        runner3.on_frame(t, _Img())
    assert quiet == []


def test_scored_end_final_uses_the_eligibility_gate():
    """The scored path posts the visual read and confirms only through the shared gate."""
    same = _scored("victory", 3, 1, confirm=True)
    visual = _run_end([(0, same), (1000, same), (4000, same)])
    assert visual[0]["visual_confirmed"] is True
    assert visual[0]["final_confirmed"] is False
    anchor = "independent-v1:" + ("ab" * 32)

    def feed(last_source, *, anchor_token=anchor, epoch=1):
        posted = []
        pending = [same, same, same]

        class End:
            def read(self, img):
                return pending.pop(0)

        runner = C.CodSetRunner(posted.append, hud=type("H", (), {"read": lambda self, img: []})(),
                                end=End(), side="home")
        runner.set_signals(
            [{"type": "in", "t_us": 0, "b": 1 << 9}, {"type": "in", "t_us": 1_000, "b": 0}],
            [], [], 0)
        runner.menu_exit_us = 1_000
        runner.lag_us_override = 1_000
        runner.capture_epoch = epoch
        runner.capture_generation = 1
        runner.session_id = "s1"
        runner.seal_durable = True
        runner.anchor_validated = True
        runner.independent_anchor = anchor_token
        for i, t in enumerate((0, 1000, 4000)):
            source = {
                "capture_id": f"c{i}",
                "frame_id": f"c{i}",
                "seq": i,
                "epoch": epoch,
                "clock": "capture_pts",
                "mono_us": 300_000 + i,
            }
            if i == 2:
                source = last_source
            runner.on_frame(t, _Img(), source=source)
        return posted[0]

    armed = feed({
        "capture_id": "c2", "frame_id": "c2", "seq": 2, "epoch": 1,
        "clock": "capture_pts", "mono_us": 4_000_000, "captured_us": 4_000_000,
    })
    assert armed["visual_confirmed"] is True and armed["final_confirmed"] is False
    early = feed({
        "capture_id": "c2", "frame_id": "c2", "seq": 2, "epoch": 1,
        "clock": "capture_pts", "mono_us": 1, "captured_us": 1,
    })
    assert early["visual_confirmed"] is True and early["final_confirmed"] is False
    bare = feed(
        {"capture_id": "c2", "frame_id": "c2", "captured_us": 4_000_000, "mono_us": 4_000_000, "seq": 2, "epoch": 1, "clock": "capture_pts"},
        anchor_token="x")
    assert bare["final_confirmed"] is False
    moved = feed({
        "capture_id": "c2", "frame_id": "c2", "captured_us": 4_000_000, "mono_us": 4_000_000,
        "seq": 2, "epoch": 2, "clock": "capture_pts",
    })
    assert moved["final_confirmed"] is False
    mismatch = _scored("defeat", 8, 3, confirm=True)
    posted = []
    pending = [mismatch, mismatch, mismatch]

    class End:
        def read(self, img):
            return pending.pop(0)

    runner = C.CodSetRunner(posted.append, hud=type("H", (), {"read": lambda self, img: []})(),
                            end=End(), side="home")
    runner.set_signals(
        [{"type": "in", "t_us": 0, "b": 1 << 9}, {"type": "in", "t_us": 1_000, "b": 0}],
        [], [], 0)
    runner.menu_exit_us = 1_000
    runner.lag_us_override = 1_000
    runner.capture_epoch = 1
    runner.independent_anchor = anchor
    for t in (0, 1000, 4000):
        runner.on_frame(t, _Img(), source={"captured_us": 4_000_000, "mono_us": 4_000_000, "clock": "capture_pts", "epoch": 1})
    assert posted[0]["visual_confirmed"] is False and posted[0]["final_confirmed"] is False


def _hud():
    return type("H", (), {"read": lambda self, img: []})()


def _end(reads):
    pending = list(reads)

    class End:
        def read(self, img):
            return pending.pop(0)

    return End()


def test_reader_stats_file_counts_errors_and_end_word_range(tmp_path, monkeypatch):
    """Stop writes counts only. The signal log is not given a new row."""
    log = tmp_path / "cod_signals.jsonl"
    word = C.RegionRead("end_screen", "read", True, "victory", True, confirm=True)
    blank = C.RegionRead("end_screen", "read", True, "scoreboard", True)
    other = C.RegionRead(
        "end_screen", "read", True, "victory", True, confirm=True,
        player_tag="Qortrola30", kills=4, deaths=1,
    )
    # The owner hook is the one the runner imports. One readable row is enough.
    monkeypatch.setattr(
        "qorcap_cod_hud.read_owner_row",
        lambda img: other,
    )
    runner = C.CodSetRunner(lambda _e: None, hud=_hud(), end=_end([word, blank, word]), log_path=log)
    runner.stats["frames"] = 0
    runner.on_frame(1_000, _Img())
    runner.on_frame(2_000, _Img())
    runner.on_frame(4_500, _Img())
    assert runner.end_word_first_ms == 1_000
    assert runner.end_word_last_ms == 4_500
    assert runner.owner_row_ever_read is True
    before = log.read_bytes()
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"result":{}}\n', encoding="utf-8")
    receipt_before = receipt.read_bytes()
    state = {
        "core": runner,
        "reader_error": "TimeoutError: snapshot",
        "cod_signal_error": "OSError: inputs",
        "reader_tick": 1_700_000_000_123,
    }
    assert M.maybe_write_cod_reader_stats(
        tmp_path, state, "cod", practice=True, reader_joined=True) is True
    assert log.read_bytes() == before
    assert receipt.read_bytes() == receipt_before
    saved = json.loads((tmp_path / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "qorcap-cod-reader-stats/v0"
    assert saved["reader_join"] == "joined"
    assert saved["reader_tick_clock"] == C.READER_TICK_CLOCK
    assert saved["end_word_clock"] == C.END_WORD_CLOCK
    assert saved["stats"] == {
        "frames": 3,
        "agreed": runner.stats["agreed"],
        "flagged_mismatch": runner.stats["flagged_mismatch"],
        "not_counted": runner.stats["not_counted"],
        "hud_not_read": runner.stats["hud_not_read"],
        "errors": runner.stats["errors"],
    }
    assert set(saved["stats"]) == {"frames", "agreed", "flagged_mismatch", "not_counted", "hud_not_read", "errors"}
    assert "img" not in json.dumps(saved)
    assert saved["reader_error"] == "TimeoutError: snapshot"
    assert saved["cod_signal_error"] == "OSError: inputs"
    assert saved["reader_tick"] == 1_700_000_000_123
    assert saved["end_word_first_ms"] == 1_000
    assert saved["end_word_last_ms"] == 4_500
    assert saved["owner_row_ever_read"] is True
    assert "not a receipt field" in saved["note"].lower()
    assert receipt.is_file()


def test_reader_stats_omit_errors_until_one_is_stored_and_skip_an_unread_owner(tmp_path):
    scored = C.RegionRead("end_screen", "read", True, "defeat", True, home=2, away=9, confirm=True)
    runner = C.CodSetRunner(lambda _e: None, hud=_hud(), end=_end([scored]))
    runner.on_frame(8_000, _Img())
    assert runner.owner_row_ever_read is False
    assert runner.end_word_first_ms == 8_000 and runner.end_word_last_ms == 8_000
    doc = C.write_reader_stats(
        tmp_path,
        C.observation_from_runner(runner, reader_error=None, cod_signal_error="", reader_tick=False),
        reader_join="joined")
    assert doc["reader_error"] is None
    assert doc["cod_signal_error"] is None
    assert doc["reader_tick"] is None
    assert doc["owner_row_ever_read"] is False
    assert doc["end_word_first_ms"] == 8_000
    signals = tmp_path / "cod_signals.jsonl"
    assert not signals.exists()


def test_madden_stop_does_not_write_reader_stats(tmp_path):
    runner = C.CodSetRunner(lambda _e: None)
    runner.stats["frames"] = 9
    runner.end_word_first_ms = 1
    state = {"core": runner, "reader_error": "kept", "reader_tick": 5}
    assert M.maybe_write_cod_reader_stats(tmp_path, state, "madden", practice=True, reader_joined=True) is False
    assert M.maybe_write_cod_reader_stats(tmp_path, state, "closed", practice=True, reader_joined=True) is False
    assert M.maybe_write_cod_reader_stats(tmp_path, state, "cod", practice=False, reader_joined=True) is False
    assert M.maybe_write_cod_reader_stats(tmp_path, {}, "cod", practice=True, reader_joined=True) is False
    assert not (tmp_path / "cod_reader_stats.json").exists()
    assert not (tmp_path / "cod_signals.jsonl").exists()
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert "run_with_reader_stats(save_tail, persist_stats)" in src
    handler = src.split("except KeyboardInterrupt:", 1)[1]
    fault = handler.split("except Exception:", 1)[1].split("finally:", 1)[0]
    # A capture-loop error must not join, persist, or raise before the save.
    assert "seal_reader(" not in fault
    assert "persist_stats()" not in fault
    assert "raise" not in fault
    assert handler.index("finally:") < handler.index("def save_tail(")
    assert handler.index("def save_tail(") < handler.index(
        "run_with_reader_stats(save_tail, persist_stats)")
    persist = src.split("def persist_stats():", 1)[1].split("\n    tail_s = ", 1)[0]
    assert "seal_reader(" not in persist
    assert "stop.set()" not in persist
    main_src = inspect.getsource(M.main)
    wait_at = main_src.index("wait_and_countersign(")
    seal_at = main_src.index("seal_reader(")
    assert main_src.rindex("stop.set()", 0, wait_at) < wait_at < seal_at
    assert "seal_reader(" not in main_src[main_src.rindex("stop.set()", 0, wait_at):wait_at]
    madden = src.split("def start_reader(", 1)[1].split("def start_cod_reader(", 1)[0]
    assert "write_reader_stats" not in madden
    assert "cod_reader_stats" not in madden


def test_practice_gate_is_explicit_and_does_not_claim_another_build(tmp_path):
    runner = C.CodSetRunner(lambda _e: None, hud=_hud(), end=_end([
        C.RegionRead("end_screen", "read", True, "victory", True, home=1, away=0, confirm=True),
    ]))
    runner.on_frame(100, _Img())
    state = {"core": runner, "reader_tick": 10}
    assert M.maybe_write_cod_reader_stats(tmp_path, state, "cod", practice=True, reader_joined=True) is True
    saved = json.loads((tmp_path / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert "another installed build" in saved["note"]
    assert "not a receipt field" in saved["note"].lower()
    assert saved["reader_tick_clock"] == "wall_unix_ms"
    assert saved["end_word_clock"] == "capture_monotonic_ms"
    assert saved["end_word_first_ms"] == 100 and saved["end_word_last_ms"] == 100
    other = tmp_path / "other"
    other.mkdir()
    assert M.maybe_write_cod_reader_stats(other, state, "cod", practice=False, reader_joined=True) is False
    assert not (other / "cod_reader_stats.json").exists()


def test_exception_and_failed_diagnostics_do_not_block_save(tmp_path, monkeypatch):
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"kept":1}\n', encoding="utf-8")
    before = receipt.read_bytes()
    signals = tmp_path / "cod_signals.jsonl"
    signals.write_text('{"kind":"set"}\n', encoding="utf-8")
    sig_before = signals.read_bytes()
    runner = C.CodSetRunner(lambda _e: None)
    runner.stats["frames"] = 4
    state = {"core": runner, "reader_tick": 40}

    def body_fails():
        raise RuntimeError("sweep broke")

    def persist_ok():
        M.maybe_write_cod_reader_stats(tmp_path, state, "cod", practice=True, reader_joined=True)

    failed = False
    try:
        M.run_with_reader_stats(body_fails, persist_ok)
    except RuntimeError as e:
        failed = str(e) == "sweep broke"
    assert failed is True
    saved = json.loads((tmp_path / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert saved["reader_join"] == "joined"
    assert saved["stats"]["frames"] == 4
    assert receipt.read_bytes() == before
    assert signals.read_bytes() == sig_before

    def broken_write(bundle, observation, *, reader_join):
        raise OSError("synthetic disk")

    monkeypatch.setattr(C, "write_reader_stats", broken_write)

    def broken_say(*_a, **_k):
        raise BrokenPipeError("synthetic closed stdout")

    monkeypatch.setattr(M, "say", broken_say)
    side = tmp_path / "side"
    side.mkdir()

    def body_saves():
        receipt.write_text('{"kept":1,"saved":true}\n', encoding="utf-8")
        return 7

    def persist_bad():
        assert M.maybe_write_cod_reader_stats(
            side, state, "cod", practice=True, reader_joined=True) is False

    assert M.run_with_reader_stats(body_saves, persist_bad) == 7
    assert json.loads(receipt.read_text(encoding="utf-8"))["saved"] is True
    assert not (side / "cod_reader_stats.json").exists()
    assert signals.read_bytes() == sig_before


def test_join_timeout_keeps_a_frozen_copy_while_the_reader_continues(tmp_path):
    runner = C.CodSetRunner(lambda _e: None, log_path=tmp_path / "cod_signals.jsonl")
    runner.stats["frames"] = 5
    runner.end_word_first_ms = 1000
    runner.end_word_last_ms = 1000
    raw = "OSError: /tmp/synth-player/config.json missing C:\\Users\\synth\\qorcap.cfg " + ("pad " * 80)
    runner.publish_reader_observation(
        reader_error=raw,
        cod_signal_error="OSError: /tmp/synth-player/notes.txt missing",
        reader_tick=111)
    log_before = (tmp_path / "cod_signals.jsonl").read_bytes() if (tmp_path / "cod_signals.jsonl").exists() else b""
    release = threading.Event()

    def linger():
        release.wait(3)
        runner.stats["frames"] = 6
        runner.end_word_last_ms = 9000
        runner.publish_reader_observation(reader_error="later", cod_signal_error=None, reader_tick=999)

    thread = threading.Thread(target=linger, name="qorcap-cod-reader")
    thread.start()
    stop = threading.Event()
    state = {"core": runner, "reader_thread": thread, "reader_error": "SHOULD_NOT_USE", "reader_tick": 1}
    joined = M.seal_reader(stop, state, timeout=0.05, slice_s=0.02)
    assert joined is False
    assert M.maybe_write_cod_reader_stats(
        tmp_path, state, "cod", practice=True, reader_joined=False) is True
    release.set()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert runner.stats["frames"] == 6
    saved = json.loads((tmp_path / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert saved["reader_join"] == "timed_out"
    assert saved["stats"]["frames"] == 5
    assert saved["end_word_first_ms"] == 1000 and saved["end_word_last_ms"] == 1000
    assert saved["reader_tick"] == 111
    assert saved["reader_tick_clock"] == "wall_unix_ms"
    assert saved["end_word_clock"] == "capture_monotonic_ms"
    blob = json.dumps(saved)
    assert "synth-player" not in blob
    assert "qorcap.cfg" not in blob
    assert "Users" not in blob
    assert "[path]" in saved["reader_error"]
    assert "[path]" in saved["cod_signal_error"]
    assert saved["reader_error"].startswith("OSError: [path] missing [path]")
    assert saved["reader_error"].endswith(" [cut]")
    assert len(saved["reader_error"]) <= C.READER_STATS_ERROR_MAX
    assert "missing" in saved["cod_signal_error"]
    signal_path = tmp_path / "cod_signals.jsonl"
    assert (signal_path.read_bytes() if signal_path.exists() else b"") == log_before

    done = C.CodSetRunner(lambda _e: None)
    done.stats["frames"] = 2

    def finish():
        done.stats["frames"] = 6
        done.publish_reader_observation(reader_error=None, cod_signal_error=None, reader_tick=8)

    live = threading.Thread(target=finish, name="qorcap-cod-reader")
    live.start()
    live_state = {"core": done, "reader_thread": live, "reader_tick": 8}
    assert M.seal_reader(threading.Event(), live_state, timeout=2) is True
    assert M.maybe_write_cod_reader_stats(tmp_path, live_state, "cod", practice=True, reader_joined=True) is True
    joined_doc = json.loads((tmp_path / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert joined_doc["reader_join"] == "joined"
    assert joined_doc["stats"]["frames"] == 6
    assert joined_doc["reader_tick"] == 8


def test_missing_join_is_not_labeled_timed_out(tmp_path):
    """None means not_joined even while the reader thread is alive.

    False stays timed_out after that thread has exited. A claimed join that
    is still alive keeps the published copy and does not say joined.
    """
    runner = C.CodSetRunner(lambda _e: None)
    runner.stats["frames"] = 4
    runner.publish_reader_observation(reader_error=None, cod_signal_error=None, reader_tick=20)
    release = threading.Event()

    def linger():
        release.wait(3)
        runner.stats["frames"] = 9
        runner.publish_reader_observation(reader_error="later", cod_signal_error=None, reader_tick=99)

    thread = threading.Thread(target=linger, name="qorcap-cod-reader")
    thread.start()
    try:
        state = {"core": runner, "reader_thread": thread, "reader_tick": 1, "reader_error": "LIVE"}
        assert M.maybe_write_cod_reader_stats(
            tmp_path, state, "cod", practice=True, reader_joined=None) is True
        saved = json.loads((tmp_path / "cod_reader_stats.json").read_text(encoding="utf-8"))
        assert saved["reader_join"] == "not_joined"
        assert saved["stats"]["frames"] == 4
        assert saved["reader_tick"] == 20
        assert saved["reader_error"] is None

        claimed = tmp_path / "claimed"
        claimed.mkdir()
        assert M.maybe_write_cod_reader_stats(
            claimed, state, "cod", practice=True, reader_joined=True) is True
        claimed_doc = json.loads((claimed / "cod_reader_stats.json").read_text(encoding="utf-8"))
        assert claimed_doc["reader_join"] == "timed_out"
        assert claimed_doc["stats"]["frames"] == 4
        assert claimed_doc["reader_error"] is None
    finally:
        release.set()
        thread.join(2)

    assert thread.is_alive() is False
    dead = tmp_path / "dead"
    dead.mkdir()
    assert M.maybe_write_cod_reader_stats(
        dead, {"core": runner}, "cod", practice=True, reader_joined=False) is True
    timed = json.loads((dead / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert timed["reader_join"] == "timed_out"

    blank = tmp_path / "blank"
    blank.mkdir()
    bare = C.CodSetRunner(lambda _e: None)
    bare.stats["frames"] = 8
    assert M.maybe_write_cod_reader_stats(
        blank, {"core": bare}, "cod", practice=True, reader_joined=None) is True
    empty = json.loads((blank / "cod_reader_stats.json").read_text(encoding="utf-8"))
    assert empty["reader_join"] == "not_joined"
    assert empty["stats"] is None


def test_scrub_reader_error_drops_bearer_unc_tilde_and_spaced_paths():
    bearer = C.scrub_reader_error(
        "OSError: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig failed")
    assert "eyJhbGciOiJIUzI1NiJ9" not in bearer
    assert "payload.sig" not in bearer
    assert "[redacted]" in bearer
    assert bearer.startswith("OSError:")

    standalone = C.scrub_reader_error("read failed Bearer ya29.a0AfH6SM-example-token now")
    assert "ya29" not in standalone
    assert "a0AfH6SM" not in standalone
    assert "Bearer [redacted]" in standalone
    assert standalone.endswith("now")

    keyed = C.scrub_reader_error("OSError: token=example")
    assert "example" not in keyed
    assert "token=[redacted]" in keyed

    unc = C.scrub_reader_error(r"OSError: \\server\share\room\clip.mp4 missing")
    assert "server" not in unc
    assert "share" not in unc
    assert "clip.mp4" not in unc
    assert unc == "OSError: [path] missing"

    spaced_unc = C.scrub_reader_error(r"OSError: \\server\share\My Folder\clip.mp4 missing")
    assert "server" not in spaced_unc
    assert "Folder" not in spaced_unc
    assert "clip.mp4" not in spaced_unc
    assert spaced_unc == "OSError: [path] missing"

    tilde = C.scrub_reader_error(
        "OSError: ~/Library/Application Support/qorcap/secret.cfg missing")
    assert "Library" not in tilde
    assert "Application" not in tilde
    assert "secret.cfg" not in tilde
    assert "qorcap" not in tilde
    assert tilde == "OSError: [path] missing"

    spaced = C.scrub_reader_error(
        r"OSError: C:\Users\synth\My Documents\qorcap.cfg missing /home/synth/My Matches/end.png")
    assert "synth" not in spaced
    assert "Documents" not in spaced
    assert "qorcap.cfg" not in spaced
    assert "Matches" not in spaced
    assert "end.png" not in spaced
    assert spaced == "OSError: [path] missing [path]"

    quoted = C.scrub_reader_error(
        r'OSError: "C:\Program Files\QorCap\device.cfg" missing')
    assert "Program" not in quoted
    assert "device.cfg" not in quoted
    assert quoted == "OSError: [path] missing"


def test_controller_path_is_not_usb_when_the_pad_was_never_read():
    """Synthetic logs. A usb-hid header is where QorCap looked, not a path it read."""
    header = {"type": "header", "transport": "usb-hid", "wall_anchor_ms": 1}
    missing = {"type": "status", "t_us": 7043, "state": "missing", "detail": "no DualSense found on USB"}
    chunk = {"type": "chunk", "seq": 0, "n": 2}
    assert C.session_document(inputs=[header])["controller_path"] == "unknown"
    assert C.session_document(inputs=[header, missing, chunk])["controller_path"] == "unknown"
    connected = {"type": "status", "t_us": 9000, "state": "connected", "detail": "DualSense Edge (USB)"}
    assert C.session_document(inputs=[header, missing, connected])["controller_path"] == "usb"
    report = {"type": "in", "t_us": 9100, "b": 0, "r2": 0}
    assert C.session_document(inputs=[header, report])["controller_path"] == "usb"
    other = {"type": "header", "transport": "bluetooth"}
    assert C.session_document(inputs=[other, report])["controller_path"] == "unknown"
