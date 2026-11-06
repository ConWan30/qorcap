"""Game set picker and the Call of Duty correlation hooks.

Madden stays the default when --game is omitted. Call of Duty hooks fail closed
until a calibrated reader is wired. Words on the log are agreed, flagged a
mismatch, and not Counted.
"""
import io
import json
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
    assert '$fwd = @("--game", "ask") + $fwd' in ps
    assert ps.index('"--game", "ask"') < ps.index('& $vpy (Join-Path $here "sidecar\\qorcap_match.py")')
    assert '"--game"' in ps
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
        json.dumps({"type": "header", "transport": "usb-hid", "wall_anchor_ms": 5}) + "\n", encoding="utf-8")
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
    assert held[0]["final_confirmed"] is True

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
    assert len(switched) == 1 and switched[0]["t_ms"] == 4000 and switched[0]["final_confirmed"] is True
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
    assert ok[0]["final_confirmed"] is True

    away_ahead = _scored("defeat", 3, 8, confirm=True)
    away = _run_end([(0, away_ahead), (1500, away_ahead), (3000, away_ahead)], side="away")
    assert away[0]["final_confirmed"] is False

    away_behind = _scored("defeat", 8, 3, confirm=True)
    away_ok = _run_end([(0, away_behind), (1500, away_behind), (3000, away_behind)], side="away")
    assert away_ok[0]["final_confirmed"] is True

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
    assert confirmed[0]["final_confirmed"] is True
    quiet = []
    runner3 = C.CodSetRunner(quiet.append, hud=type("H", (), {"read": lambda self, img: []})(),
                             end=End(lambda img: C.RegionRead("end_screen", "read", True, "ok", False, home=9, away=9)))
    for t in (0, 1000, 5000):
        runner3.on_frame(t, _Img())
    assert quiet == []
