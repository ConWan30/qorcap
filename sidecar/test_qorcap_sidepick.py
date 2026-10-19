"""v0.3.26 side pick at Start Match: flag > one-shot side file > H/A prompt (waits, never a silent home)."""
import io
import os
import time
import types
from pathlib import Path

import qorcap_match as M


def keys(seq):
    it = iter(seq)
    return lambda timeout: next(it, None)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def ns(side=None, role=None):
    return types.SimpleNamespace(side=side, role=role)


def test_prompt_text_is_big_and_explains_left_is_away():
    lines = M.side_prompt_lines()
    txt = "\n".join(lines)
    assert "press  H  or  A" in txt
    assert "AWAY = the LEFT team, listed FIRST on the Madden scorebug." in txt
    assert "HOME = the RIGHT team" in txt and "Nothing is recorded until you choose" in txt
    assert len(M.big_text("H = HOME")) == 5 and "#####" in M.big_text("A = AWAY")[2]


def test_prompt_needs_a_letter_then_enter_and_allows_switching():
    out = io.StringIO()
    assert M.prompt_side(keys(["\n", "x", "a", "h", "\n"]), out) == "home"   # Enter before a choice is ignored
    assert "You picked AWAY (left / first-listed team)" in out.getvalue()
    assert "You picked HOME (right team)" in out.getvalue()
    assert M.prompt_side(keys(["A", "\n"]), io.StringIO()) == "away"


def test_prompt_waits_without_timeout_and_reminds_every_60_s():
    clk, out = Clock(), io.StringIO()
    def rk(timeout):
        clk.t += timeout
        return "a" if clk.t > 600 and not getattr(rk, "done", False) and setattr(rk, "done", True) is None else (
            "\n" if getattr(rk, "done", False) else "")
    assert M.prompt_side(rk, out, clock=clk) == "away"            # 10 minutes unanswered: still waiting, no default
    assert out.getvalue().count("WHICH SIDE ARE YOU?") >= 10
    assert out.getvalue().count("\a") >= 10


def test_prompt_without_input_returns_none():
    assert M.prompt_side(keys([]), io.StringIO()) is None


def test_side_file_is_one_shot_and_renamed_not_deleted(tmp_path):
    f = tmp_path / M.SIDE_FILE_NAME
    f.write_text("away\n", encoding="utf-8")
    side, note = M.take_side_file(f)
    assert side == "away" and "AWAY" in note and not f.exists()
    assert [p.name.startswith("next-side.used-") for p in tmp_path.iterdir()] == [True]
    assert M.take_side_file(f) == (None, "")                      # consumed: the next match asks again


def test_stale_or_bad_side_file_is_ignored(tmp_path):
    f = tmp_path / M.SIDE_FILE_NAME
    f.write_text("away", encoding="utf-8")
    old = time.time() - 5 * 3600
    os.utime(f, (old, old))
    side, note = M.take_side_file(f)
    assert side is None and "min old" in note and not f.exists()
    f.write_text("visitor", encoding="utf-8")
    side, note = M.take_side_file(f)
    assert side is None and "not home/away" in note
    assert sorted(p.name.split("-")[1].split(".")[1] for p in tmp_path.iterdir()) == ["ignored", "ignored"]


def test_resolve_order_flag_then_file_then_prompt(tmp_path):
    f = tmp_path / M.SIDE_FILE_NAME
    f.write_text("home", encoding="utf-8")
    assert M.resolve_side(ns(side="away"), side_file=f) == ("away", "flag")
    assert M.resolve_side(ns(role="away"), side_file=f) == ("away", "flag")
    assert f.exists()                                              # a flag leaves the side file for later
    assert M.resolve_side(ns(side="ask"), side_file=f, read_key=keys([])) == ("home", "file")
    assert M.resolve_side(ns(side="ask"), side_file=f, read_key=keys(["a", "\n"]), out=io.StringIO()) == ("away", "prompt")
    assert M.resolve_side(ns(side="ask"), side_file=f, read_key=keys([]), out=io.StringIO()) == (None, None)
    assert M.resolve_side(ns(), side_file=f) == ("home", None)    # direct CLI without --side: legacy default, unrecorded


def test_cli_accepts_side_and_launcher_always_asks():
    ps = (Path(__file__).resolve().parents[1] / "start-match.ps1").read_text(encoding="utf-8")
    assert '$fwd = @("--side", "ask") + $fwd' in ps and '"--side"' in ps and '"--role"' in ps
    assert ps.index('"--side", "ask"') < ps.index('& $vpy (Join-Path $here "sidecar\\qorcap_match.py")')
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert 'choices=["home", "away", "ask"]' in src and '"--side-source", side_source' in src
    assert src.index("resolve_side(a, side_file=") < src.index("proc = spawn_qorcap(cmd, log)")
