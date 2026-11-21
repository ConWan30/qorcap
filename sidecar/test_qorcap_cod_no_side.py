"""Practice picker: Call of Duty skips the home/away prompt. Madden keeps it."""
import io
import json
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


def test_side_file_parser_alone_on_an_empty_folder(tmp_path):
    # Parser only. This is not the session path; the main() tests below cover that.
    from qorcap_cod import side_from_bundle
    assert side_from_bundle(tmp_path) is None


# ---------------------------------------------------------------- main() -> Call of Duty reader
STAMP = "20261107-100000"
_REAL_COD_READER = M.start_cod_reader
_REAL_STRFTIME = M.time.strftime


class _Abort(Exception):
    pass


class _Proc:
    returncode = None

    def poll(self):
        return None


def _run_main(tmp_path, monkeypatch, argv):
    """Run main() up to the reader start. Returns what the recorder and the reader got."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    seen = {}
    real_reader, real_strftime = _REAL_COD_READER, _REAL_STRFTIME

    class FakeApi:
        def __init__(self, port):
            self.base = f"http://127.0.0.1:{port}"
            self.calls = 0

        def get(self, path, timeout=3):
            self.calls += 1
            if self.calls == 1:  # the "already running?" probe: nothing is running
                raise OSError("not running")
            return {}

        def post(self, path, body=None, timeout=5):
            return {}

    def spawn(cmd, log):
        seen["cmd"] = list(cmd)
        return _Proc()

    def cod_reader(api, stop, state, bundle=None, **kw):
        seen["reader_kwargs"] = dict(kw)
        stop.set()  # the frame loop exits at once; no snapshot is fetched
        t = real_reader(api, stop, state, bundle=bundle, **kw)
        t.join(timeout=5)
        seen["runner_side"] = state["core"].side
        raise _Abort

    def madden_reader(api, stop, state, bundle=None):
        seen["madden_reader"] = True
        raise _Abort

    monkeypatch.setattr(M, "data_dir", lambda: data)
    monkeypatch.setattr(M, "find_qortime", lambda arg, download=True: tmp_path)
    monkeypatch.setattr(M, "check_python_deps", lambda need_vision: [])
    monkeypatch.setattr(M, "find_qorcap", lambda arg: "qorcap")
    monkeypatch.setattr(M, "load_keys", lambda d: (None, None))
    monkeypatch.setattr(M, "Api", FakeApi)
    monkeypatch.setattr(M, "spawn_qorcap", spawn)
    monkeypatch.setattr(M, "Countersigner", lambda *a, **k: None)
    monkeypatch.setattr(M, "start_cod_reader", cod_reader)
    monkeypatch.setattr(M, "start_reader", madden_reader)
    monkeypatch.setattr(M, "start_fake_scores", madden_reader)
    monkeypatch.setattr(M.time, "strftime",
                        lambda fmt, *t: STAMP if fmt == "%Y%m%d-%H%M%S" else real_strftime(fmt, *t))
    try:
        M.main(["--test-pattern", "--audio", "none", "--no-open", "--no-official-challenges",
                "--out", str(tmp_path / "out")] + argv)
    except _Abort:
        pass
    return seen, tmp_path / "out" / f"match-{STAMP}", data


def _stale(bundle, data, role="away"):
    bundle.mkdir(parents=True, exist_ok=True)
    side = bundle / "side_choice.json"
    side.write_text(json.dumps({"role": role, "source": "prompt", "chosen_at": "2026-11-01T09:00:00-0500"}),
                    encoding="utf-8")
    nxt = data / M.SIDE_FILE_NAME
    data.mkdir(parents=True, exist_ok=True)
    nxt.write_text(role, encoding="utf-8")
    return side, side.read_bytes(), nxt


def _assert_no_side(seen):
    cmd = seen["cmd"]
    assert cmd[cmd.index("--role") + 1] == "home"  # receipt.v0 role slot, not a typed N/A
    assert "--side-source" not in cmd
    assert seen["reader_kwargs"] == {"side": None}
    assert seen["runner_side"] is None


def test_main_cod_fresh_folder_passes_no_side(tmp_path, monkeypatch):
    seen, bundle, _data = _run_main(tmp_path, monkeypatch, ["--game", "cod", "--side", "ask"])
    _assert_no_side(seen)
    assert not (bundle / "side_choice.json").exists()


def test_main_cod_ignores_stale_side_files(tmp_path, monkeypatch):
    for role in ("home", "away"):
        root = tmp_path / role
        root.mkdir()
        side, before, nxt = _stale(root / "out" / f"match-{STAMP}", root / "data", role=role)
        seen, bundle, _data = _run_main(root, monkeypatch, ["--game", "cod", "--side", "ask"])
        _assert_no_side(seen)
        assert bundle == side.parent
        assert side.read_bytes() == before  # prior evidence left as it was, not rewritten or deleted
        assert nxt.read_text(encoding="utf-8") == role  # next-side.txt not consumed by Call of Duty


def test_main_cod_explicit_away_flag_ends_up_home(tmp_path, monkeypatch):
    for flag in (["--side", "away"], ["--role", "away"]):
        root = tmp_path / flag[0].strip("-")
        root.mkdir()
        seen, _bundle, _data = _run_main(root, monkeypatch, ["--game", "cod"] + flag)
        _assert_no_side(seen)


def test_main_madden_unchanged_with_a_side(tmp_path, monkeypatch):
    seen, bundle, _data = _run_main(tmp_path, monkeypatch, ["--game", "madden", "--side", "away"])
    cmd = seen["cmd"]
    assert cmd[cmd.index("--role") + 1] == "away"
    assert cmd[cmd.index("--side-source") + 1] == "flag"
    assert json.loads((bundle / "side_choice.json").read_text(encoding="utf-8"))["role"] == "away"
    assert "reader_kwargs" not in seen and seen.get("madden_reader") is True


def test_reader_side_controls_the_scored_end_side_check(tmp_path):
    """Positive and negative control on the scored-end side check only (a read with home and away).

    Owner-row final agreement is a separate path and is not asserted here.
    """
    import qorcap_cod as C
    import test_qorcap_games as TG
    side_file = tmp_path / "side_choice.json"
    side_file.write_text(json.dumps({"role": "home"}), encoding="utf-8")
    matched = TG._scored("defeat", 3, 8, confirm=True)  # home behind: DEFEAT fits a chosen home side

    def run(side):
        runner = C.CodSetRunner(lambda b: None, hud=type("H", (), {"read": lambda self, img: []})(),
                                end=TG._scripted(iter([matched, matched, matched])), side=side)
        for t in (0, 1500, 3000):
            runner.on_frame(t, TG._Img())
        return runner.stats["end_direction_mismatch"]

    # A side supplied on purpose: the side check passes.
    assert run(C.side_from_bundle(tmp_path)) == 0
    # No-pick context with the same stale file present (what main() now passes for Call of Duty): it does not.
    assert run(None) > 0
