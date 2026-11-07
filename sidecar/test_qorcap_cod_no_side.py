"""Practice picker: Call of Duty skips the home/away prompt. Madden keeps it."""
import io
import types

import qorcap_match as M


def keys(seq):
    it = iter(seq)
    return lambda timeout: next(it, None)


def ns(side=None, role=None):
    return types.SimpleNamespace(side=side, role=role)


def test_madden_still_prompts_and_records_a_side(tmp_path):
    out = io.StringIO()
    got = M.resolve_side(ns(side="ask"), side_file=tmp_path / "next-side.txt",
                         read_key=keys(["a", "\n"]), out=out, game="madden")
    assert got == ("away", "prompt")
    assert "WHICH SIDE ARE YOU?" in out.getvalue()


def test_cod_does_not_prompt_and_records_the_no_side_value(tmp_path):
    out = io.StringIO()
    def no_keys(timeout):
        raise AssertionError("Call of Duty must not read a side key")
    got = M.resolve_side(ns(side="ask"), side_file=tmp_path / "next-side.txt", read_key=no_keys, out=out, game="cod")
    # Same value the no-side path already returns: receipt role slot "home", no side source.
    assert got == ("home", None) == M.resolve_side(ns(), side_file=tmp_path / "x.txt")
    assert out.getvalue() == ""


def test_cod_leaves_next_side_file_and_flags_unused(tmp_path):
    f = tmp_path / "next-side.txt"
    f.write_text("away", encoding="utf-8")
    assert M.resolve_side(ns(side="ask"), side_file=f, game="cod") == ("home", None)
    assert f.exists()  # not consumed; it stays for the next Madden match
    assert M.resolve_side(ns(side="away"), side_file=f, game="cod") == ("home", None)


def test_cod_has_no_chosen_side_for_the_end_word_check(tmp_path):
    from qorcap_cod import side_from_bundle
    assert side_from_bundle(tmp_path) is None  # no side_choice.json is written without a side source
