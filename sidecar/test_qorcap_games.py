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
