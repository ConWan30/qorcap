"""PWEC engagement window index: window building, merging, classes, frame-edge uncertainty,
exclusions, seal isolation, error isolation, and parity with the plan's offline count.

Synthetic inputs.jsonl / frame_clock.jsonl only, plus one reduced practice log
(sidecar/fixtures/pwec). No pixels, no model, no network.
"""
import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import qorcap_engagement as E
import qorcap_match as M

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FX = HERE / "fixtures" / "pwec"
PRACTICE = {"QORCAP_LANE": "practice"}


# ------------------------------------------------------------------------------------------ helpers
def _in(t, r2=0, l2=0):
    return {"type": "in", "t_us": t, "b": 0, "l2": l2, "r2": r2}


def _hb(t, **bad):
    row = {"type": "hb", "t_us": t, "faults": 0, "stale_over_50ms": 0, "seq_breaks": 0, "short_reads": 0,
           "parse_faults": 0, "max_rx_gap_us": 1200}
    row.update(bad)
    return row


def _frames(n=1200, start_us=0, lag_us=4000, lag_at=None):
    """60 fps CFR frames (pts step 1500). ``lag_at`` = {frame: extra arrival lag us}."""
    out = []
    for i in range(n):
        lag = lag_us + (lag_at or {}).get(i, 0)
        out.append({"type": "frame", "file": "video.mp4", "frame": i, "pts_90k": i * 1500,
                    "t_mono_us": start_us + round(i * 1500 * 100 / 9) + lag})
    return out


def _bundle(tmp_path, rows, frames=None, name="match-20990101-000000", hb_every=1_000_000, hb_until=None,
           extra_hb=(), heartbeats=None):
    d = tmp_path / name
    d.mkdir(parents=True)
    last = max(r["t_us"] for r in rows)
    hb_until = last + 2_000_000 if hb_until is None else hb_until
    if heartbeats is not None:
        hb = list(heartbeats)
    else:
        hb = [_hb(t) for t in range(hb_every, hb_until + 1, hb_every)] + list(extra_hb)
    allrows = [{"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0}] + sorted(rows + hb, key=lambda r: r["t_us"])
    (d / "inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in allrows), encoding="utf-8")
    fr = [{"type": "header", "schema": "qorcap-frame-clock/v0"}] + (frames if frames is not None else _frames())
    (d / "frame_clock.jsonl").write_text("".join(json.dumps(r) + "\n" for r in fr), encoding="utf-8")
    return d


def _windows(d, **kw):
    ws, summary = E.analyze_match_dir(d, **kw)
    return ws, summary


# ------------------------------------------------------------------------------------------ building
def test_threshold_is_strictly_above_60_and_rows_are_on_change():
    rows = [(0, 0, 0), (100, 60, 0), (200, 61, 0), (300, 61, 0), (400, 60, 0), (500, 255, 0)]
    assert E.hold_intervals(rows, 1, 900) == [(200, 400, False), (500, 900, True)]
    assert E.hold_intervals(rows, 2, 900) == []


def test_window_spans_pre_and_post_roll_and_carries_input_facts(tmp_path):
    rows = [_in(0), _in(5_000_000, l2=200), _in(5_300_000, r2=200, l2=200), _in(5_600_000, r2=0, l2=200),
            _in(6_000_000, l2=0)]
    ws, s = _windows(_bundle(tmp_path, rows))
    assert len(ws) == 1
    w = ws[0]
    assert w["class"] == "L2+R2"
    assert w["input"]["core_t_us"] == [5_000_000, 6_000_000]
    assert w["t_us"] == [4_500_000, 7_000_000]
    assert w["input"]["r2_holds"] == 1 and w["input"]["l2_holds"] == 1
    assert w["input"]["r2_held_ms"] == 300.0 and w["input"]["l2_held_ms"] == 1000.0
    assert w["input"]["r2l2_overlap_ms"] == 300.0
    assert w["flags"] == [] and w["excluded"] is False
    assert s["windows"]["L2+R2"] == 1 and s["clean_windows"]["L2+R2"] == 1


def test_merge_gap_500_ms_is_inclusive():
    r2 = [(1_000_000, 1_200_000, False), (1_700_000, 1_800_000, False), (2_300_001, 2_400_000, False)]
    eps = E.build_episodes(r2, [], 500_000)
    assert [(e["start"], e["end"], len(e["R"])) for e in eps] == [(1_000_000, 1_800_000, 2), (2_300_001, 2_400_000, 1)]


def test_classes_l2r2_r2only_l2only_split():
    assert E.build_episodes([(0, 100, False)], [(50, 200, False)])[0]["cls"] == "L2+R2"
    assert E.build_episodes([(0, 100, False)], [])[0]["cls"] == "R2-only"
    assert E.build_episodes([], [(0, 100, False)])[0]["cls"] == "L2-only"
    # Hip-fire, then L2 within the merge gap, never held together: its own bucket.
    split = E.build_episodes([(0, 100_000, False)], [(300_000, 900_000, False)])
    assert len(split) == 1 and split[0]["cls"] == "split" and split[0]["overlap_us"] == 0
    assert set(E.CLASSES) == {"L2+R2", "R2-only", "L2-only", "split"}


def test_core_certain_frames_carry_the_whole_edge_uncertainty(tmp_path):
    a, b = 5_000_000, 6_000_000
    rows = [_in(0), _in(a, r2=200, l2=200), _in(b)]
    d = _bundle(tmp_path, rows)
    ws, s = _windows(d)
    w = ws[0]
    env = s["clock"]["env_us"]
    T = [i * 1500 * 100 / 9 + env for i in range(1200)]
    certain = [i for i, t in enumerate(T) if t - 233_333 >= a and t + 16_667 <= b]
    possible = [i for i, t in enumerate(T) if t + 16_667 >= a and t - 233_333 <= b]
    window = [i for i, t in enumerate(T) if t + 16_667 >= a - 500_000 and t - 233_333 <= b + 1_000_000]
    fr = w["frames"]
    assert (fr["core_certain"]["first"], fr["core_certain"]["last"], fr["core_certain"]["count"]) == (
        certain[0], certain[-1], len(certain))
    assert (fr["core_possible"]["first"], fr["core_possible"]["last"]) == (possible[0], possible[-1])
    assert (fr["window"]["first"], fr["window"]["last"]) == (window[0], window[-1])
    assert fr["core_certain"]["count"] < fr["core_possible"]["count"] < fr["window"]["count"]
    assert s["clock"]["capture_offset"] == "UNMEASURED" and s["clock"]["frame_W_ms"] == [-233.3, 16.7]


def test_core_under_250_ms_has_no_core_certain_frame(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_240_000)]
    w = _windows(_bundle(tmp_path, rows))[0][0]
    assert w["frames"]["core_certain"] == {"first": None, "last": None, "count": 0}
    assert w["frames"]["core_possible"]["count"] > 0


# ------------------------------------------------------------------------------------------ exclusions
def test_long_core_over_8_s_is_flagged(tmp_path):
    rows = [_in(0), _in(2_000_000, l2=200), _in(10_000_001, l2=0)]
    w = _windows(_bundle(tmp_path, rows))[0][0]
    assert "long_core" in w["flags"] and w["excluded"] is True


def test_controller_health_flags_hid_doubt(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    w = _windows(_bundle(tmp_path, rows))[0][0]
    assert "hid_doubt" not in w["flags"]
    for bad in ({"faults": 1}, {"stale_over_50ms": 2}, {"seq_breaks": 1}, {"short_reads": 1}, {"parse_faults": 1},
                {"max_rx_gap_us": 50_001}):
        d = _bundle(tmp_path / "_".join(sorted(bad)), rows, extra_hb=[_hb(5_200_000, **bad)])
        assert "hid_doubt" in _windows(d)[0][0]["flags"], bad


def test_heartbeat_gap_or_none_after_window_is_hid_doubt(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    gap = _bundle(tmp_path / "gap", rows, hb_every=2_000_000)
    assert "hid_doubt" in _windows(gap)[0][0]["flags"]
    none_after = _bundle(tmp_path / "end", rows, hb_until=6_000_000)
    assert "hid_doubt" in _windows(none_after)[0][0]["flags"]


def test_leading_heartbeat_gap_when_j_is_zero(tmp_path):
    """No heartbeat precedes the padded start (j == 0). A first heartbeat more than 1.5 s
    after that start is hid_doubt; one within 1.5 s is not, if later gaps are also healthy."""
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    # padded start is 4_500_000. First hb 1.6 s later, then 1 s steps through the end.
    late = _bundle(tmp_path / "late", rows, heartbeats=[_hb(6_100_000), _hb(7_100_000), _hb(8_100_000)])
    assert "hid_doubt" in _windows(late)[0][0]["flags"]
    ok = _bundle(tmp_path / "ok", rows, heartbeats=[_hb(5_500_000), _hb(6_500_000), _hb(7_500_000), _hb(8_500_000)])
    assert "hid_doubt" not in _windows(ok)[0][0]["flags"]


def test_backlog_and_out_of_span_and_open_at_end(tmp_path):
    rows = [_in(0), _in(10_000_000, r2=200, l2=200), _in(10_500_000)]
    lagged = _bundle(tmp_path / "lag", rows, frames=_frames(lag_at={600: 700_000}))
    assert "backlog" in _windows(lagged)[0][0]["flags"]
    early = _bundle(tmp_path / "early", [_in(0), _in(100_000, r2=200), _in(400_000)])
    assert "out_of_span" in _windows(early)[0][0]["flags"]
    still = _bundle(tmp_path / "open", [_in(0), _in(10_000_000, l2=200), _in(10_100_000, l2=200)])
    w = _windows(still)[0][0]
    assert "open_at_log_end" in w["flags"] and w["input"]["open_at_log_end"] is True


def test_match_b4_is_a_caller_flag_and_otherwise_unmeasured(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    d = _bundle(tmp_path, rows)
    ws, s = _windows(d)
    assert "match_b4" not in ws[0]["flags"] and s["match_b4"].startswith("UNMEASURED")
    ws, s = _windows(d, match_flags=("match_b4",))
    assert "match_b4" in ws[0]["flags"] and s["clean_windows"]["L2+R2"] == 0


# ------------------------------------------------------------------------------------------ output shape
def test_every_line_is_info_only_never_counted(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200), _in(5_400_000), _in(7_000_000, l2=200), _in(7_500_000)]
    d = _bundle(tmp_path / "m", rows)
    out_root = tmp_path / "out"
    p = E.write_engagement_windows(d, out_root)
    assert p == out_root / d.name / E.FILE_NAME
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert [w["class"] for w in lines] == ["R2-only", "L2-only"]
    for w in lines:
        assert w["schema"] == "qorcap-engagement-windows/v0-info" and w["version"] == E.VERSION
        assert w["info_only"] is True and w["counted"] is False and w["advisory_only"] is True
        assert w["match"] == d.name
    s = json.loads((out_root / d.name / E.SUMMARY_NAME).read_text(encoding="utf-8"))
    assert s["info_only"] is True and s["counted"] is False
    assert s["params"] == {"threshold_gt": 60, "W_lo_ms": 233.3, "W_hi_ms": 16.7, "merge_gap_ms": 500.0,
                           "pre_roll_ms": 500.0, "post_roll_ms": 1000.0, "max_core_ms": 8000.0}
    assert s["inputs"]["inputs_sha256"] == hashlib.sha256((d / "inputs.jsonl").read_bytes()).hexdigest()
    assert "Not ADS seen on screen" in s["wording"]
    # Same inputs, same bytes.
    first = p.read_bytes()
    E.write_engagement_windows(d, out_root)
    assert p.read_bytes() == first
    assert not list((out_root / d.name).glob("*.tmp"))


def test_wording_makes_no_banned_claim():
    banned = ("verified", "proven", "skill", "reaction time", "anti-cheat")
    for path in (HERE / "qorcap_engagement.py", REPO / "docs" / "PWEC.md", FX / "README.md"):
        text = path.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, (path.name, word)


# ------------------------------------------------------------------------------------------ gate
def test_gate_practice_lane_cod_only_with_opt_out():
    assert E.enabled("cod", PRACTICE) is True
    assert E.enabled("cod", {"QORCAP_LANE": " Practice "}) is True
    assert E.enabled("cod", {}) is False                       # REAL MATCH / no lane
    assert E.enabled("cod", {"QORCAP_LANE": "real"}) is False
    assert E.enabled("cod", {"QORCAP_PWEC": "1"}) is False     # the flag alone never turns it on
    assert E.enabled("madden", PRACTICE) is False
    for off in ("0", "false", "no", "off", " OFF "):
        assert E.enabled("cod", {**PRACTICE, "QORCAP_PWEC": off}) is False


def test_off_gate_writes_nothing_and_says_nothing(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    d = _bundle(tmp_path / "m", rows)
    said = []
    for game, env in (("cod", {}), ("madden", PRACTICE), ("cod", {**PRACTICE, "QORCAP_PWEC": "0"})):
        assert E.maybe_write_engagement_windows(d, game, environ=env, out_root=tmp_path / "out", say=said.append) is False
    assert not (tmp_path / "out").exists() and said == []


def test_default_out_root_is_localappdata_engagement_folder(tmp_path):
    assert E.default_out_root({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "QorCapEngagement"
    assert E.default_out_root({"LOCALAPPDATA": str(tmp_path), "QORCAP_PWEC_OUT": str(tmp_path / "x")}) == tmp_path / "x"


def test_out_dir_for_rejects_unsafe_leaf_names(tmp_path):
    out_root = tmp_path / "QorCapEngagement"
    out_root.mkdir()
    safe = tmp_path / "matches" / "match-20990101-000000"
    safe.mkdir(parents=True)
    assert E.out_dir_for(safe, out_root) == out_root / "match-20990101-000000"
    # Path(".../..").name is ".."; without the check this would be out_root.parent.
    escaped = (tmp_path / "any" / "match") / ".."
    assert escaped.name == ".."
    assert (out_root / escaped.name).resolve() == out_root.parent.resolve()
    with pytest.raises(E.PwecError, match="empty or a path parent"):
        E.out_dir_for(escaped, out_root)
    with pytest.raises(E.PwecError, match="empty or a path parent"):
        E.out_dir_for(Path(".."), out_root)
    with pytest.raises(E.PwecError, match="empty or a path parent"):
        E.out_dir_for(Path("."), out_root)
    for name in ("", "foo/bar", "foo\\bar", "/abs"):
        with pytest.raises(E.PwecError, match="empty or a path parent|not a single leaf"):
            E.require_safe_leaf(name)


# ------------------------------------------------------------------------------------------ seal isolation
def _tree(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def _sealed_bundle(tmp_path):
    import qorcap_submit as S
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    d = _bundle(tmp_path / "Documents" / "QorCap Matches", rows)
    for n in S.upload_names():
        if not (d / n).exists():
            (d / n).write_text(json.dumps({"stub": n}) + "\n", encoding="utf-8")
    return d, S


def test_sidecar_file_is_outside_everything_the_seal_or_upload_reads(tmp_path):
    d, S = _sealed_bundle(tmp_path)
    compat = json.loads((REPO / "contracts" / "compat.json").read_text(encoding="utf-8"))["bundle_files"]
    for name in (E.FILE_NAME, E.SUMMARY_NAME):
        assert name not in compat["required"] and name not in compat["meta"] and name not in S.upload_names()
    before_tree, before_zip = _tree(d), S.meta_zip(d)
    out_root = tmp_path / "LocalAppData" / "QorCapEngagement"
    said = []
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=out_root, say=said.append) is True
    assert _tree(d) == before_tree                # every bundle file: same names, same bytes
    assert S.meta_zip(d) == before_zip           # the upload zip is byte-identical
    out = out_root / d.name / E.FILE_NAME
    assert out.is_file() and not E._under(out, d)
    assert "info only, not Counted" in said[0]


def test_output_root_inside_the_match_folder_is_refused(tmp_path):
    d, _S = _sealed_bundle(tmp_path)
    before = _tree(d)
    said = []
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=d / "sub", say=said.append) is False
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=d.parent, say=said.append) is False
    assert _tree(d) == before and not (d / "sub").exists()
    assert all("not written" in m for m in said)
    # Successful path (valid out root): the match folder must still be unchanged. A write into
    # the match folder after a valid out root must fail this test, not only the tree-hash and
    # audit-hook tests.
    ok_root = tmp_path / "LocalAppData" / "QorCapEngagement"
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=ok_root, say=said.append) is True
    assert (ok_root / d.name / E.FILE_NAME).is_file()
    assert _tree(d) == before


def test_match_folder_gets_no_write_open_rename_or_mkdir(tmp_path):
    """A child process with an audit hook: any write-mode open, rename, replace, remove or mkdir
    under the match folder raises. The CLI must finish with exit 0 and write only outside it."""
    d, _S = _sealed_bundle(tmp_path)
    out_root = tmp_path / "LocalAppData" / "QorCapEngagement"
    code = r'''
import os, sys
match = os.path.realpath(sys.argv[1])
hits = []
def under(p):
    try:
        p = os.path.realpath(os.fsdecode(p))
    except Exception:
        return False
    try:
        return os.path.commonpath([p, match]) == match
    except ValueError:
        return False
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = any(c in mode for c in "wax+") or (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if writing and under(args[0]):
            hits.append((event, args[0]))
            raise PermissionError("write under the match folder")
    elif event in ("os.rename", "os.replace", "os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.truncate"):
        for a in args[:2]:
            if isinstance(a, (str, bytes, os.PathLike)) and under(a):
                hits.append((event, a))
                raise PermissionError(event + " under the match folder")
sys.addaudithook(hook)
sys.path.insert(0, sys.argv[3])
import qorcap_engagement as E
rc = E.main(["--match-dir", sys.argv[1], "--out", sys.argv[2]])
print("HITS", len(hits))
sys.exit(rc)
'''
    r = subprocess.run([sys.executable, "-c", code, str(d), str(out_root), str(HERE)], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    assert "HITS 0" in r.stdout
    assert (out_root / d.name / E.FILE_NAME).is_file()


# ------------------------------------------------------------------------------------------ error isolation
def test_errors_are_swallowed_and_logged(tmp_path, monkeypatch):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    d = _bundle(tmp_path / "m", rows)
    said = []

    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(E, "analyze_match_dir", boom)
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=tmp_path / "o", say=said.append) is False
    assert said and "RuntimeError" in said[0] and "match is unaffected" in said[0]
    monkeypatch.undo()
    (d / "frame_clock.jsonl").unlink()
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=tmp_path / "o", say=None) is False
    # A say() that raises is swallowed too.
    def bad_say(_m):
        raise OSError("console gone")
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=tmp_path / "o", say=bad_say) is False


def test_corrupt_lines_are_counted_not_fatal(tmp_path):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    d = _bundle(tmp_path / "m", rows)
    with open(d / "inputs.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n[1,2]\n")
    ws, s = _windows(d)
    assert len(ws) == 1 and s["inputs"]["inputs_bad_lines"] == 2


def test_unreadable_inputs_write_no_index(tmp_path):
    """An inputs log that exists but does not parse must not write a 0-window index."""
    d = tmp_path / "match-20990101-000000"
    d.mkdir()
    (d / "inputs.jsonl").write_text("{not json\n[1,2]\n", encoding="utf-8")
    (d / "frame_clock.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ([{"type": "header"}] + _frames())),
                                        encoding="utf-8")
    said = []
    out = tmp_path / "out"
    assert E.maybe_write_engagement_windows(d, "cod", environ=PRACTICE, out_root=out, say=said.append) is False
    assert list(out.rglob(E.FILE_NAME)) == []
    summaries = list(out.rglob(E.SUMMARY_NAME))
    for sp in summaries:
        doc = json.loads(sp.read_text(encoding="utf-8"))
        assert doc.get("status") == "UNKNOWN"
        assert "windows" not in doc
    assert said and "does not parse" in said[0] and "UNKNOWN" in said[0]
    with pytest.raises(E.PwecError, match="does not parse"):
        E.analyze_match_dir(d)
    # Header-only (parses as JSON, but no in or hb row) is the same: no index.
    d2 = tmp_path / "match-20990101-000001"
    d2.mkdir()
    (d2 / "inputs.jsonl").write_text(json.dumps({"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0}) + "\n",
                                     encoding="utf-8")
    (d2 / "frame_clock.jsonl").write_bytes((d / "frame_clock.jsonl").read_bytes())
    assert E.maybe_write_engagement_windows(d2, "cod", environ=PRACTICE, out_root=out, say=said.append) is False
    assert list(out.rglob(E.FILE_NAME)) == []


def test_match_hook_never_raises_and_runs_after_reader_stats(tmp_path, monkeypatch):
    import qorcap_engagement

    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(qorcap_engagement, "maybe_write_engagement_windows", boom)
    said = []
    monkeypatch.setattr(M, "say", lambda m="", **k: said.append(m))
    assert M.maybe_write_engagement_windows(tmp_path, "cod", environ=PRACTICE) is False
    assert said and "match is unaffected" in said[0]
    src = Path(M.__file__).read_text(encoding="utf-8")
    persist = src.split("def persist_stats():", 1)[1].split("\n    tail_s = ", 1)[0]
    assert persist.index("maybe_write_cod_reader_stats(") < persist.index("maybe_write_engagement_windows(bundle, game)")
    assert "try:" in persist.split("maybe_write_cod_reader_stats(", 1)[1]
    wrapper = src.split("def maybe_write_engagement_windows(", 1)[1].split("def mark_health_sealed(", 1)[0]
    assert wrapper.index("QORCAP_LANE") < wrapper.index("from qorcap_engagement import")
    # persist_stats runs in run_with_reader_stats' finally, after save_tail (seal, countersign,
    # receipt); its exceptions cannot replace the save result.
    assert "run_with_reader_stats(save_tail, persist_stats)" in src
    assert M.run_with_reader_stats(lambda: 7, boom) == 7


def test_real_match_lane_hook_is_a_no_op(tmp_path, monkeypatch):
    rows = [_in(0), _in(5_000_000, r2=200, l2=200), _in(5_500_000)]
    d = _bundle(tmp_path / "m", rows)
    monkeypatch.delenv("QORCAP_LANE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    before = _tree(d)
    path_before = list(sys.path)
    assert M.maybe_write_engagement_windows(d, "cod") is False
    assert _tree(d) == before and not (tmp_path / "lad").exists()
    assert sys.path == path_before


# ------------------------------------------------------------------------------------------ parity
def _load_fixture(tmp_path):
    name = "match-20261119-191816"
    d = tmp_path / name
    d.mkdir()
    with gzip.open(FX / f"{name}.inputs.jsonl.gz", "rb") as f:
        (d / "inputs.jsonl").write_bytes(f.read())
    prev = (0, 0, 0)
    lines = []
    with gzip.open(FX / f"{name}.frame_clock.delta.txt.gz", "rt", encoding="utf-8") as f:
        for line in f:
            df, dt, dp = (int(x) for x in line.split())
            prev = (prev[0] + df, prev[1] + dt, prev[2] + dp)
            lines.append(json.dumps({"type": "frame", "file": "video.mp4", "frame": prev[0], "t_mono_us": prev[1],
                                     "pts_90k": prev[2]}))
    (d / "frame_clock.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d, json.loads((FX / f"{name}.p0-golden.json").read_text(encoding="utf-8"))


def test_parity_with_the_plan_offline_count_on_a_reduced_practice_log(tmp_path):
    d, golden = _load_fixture(tmp_path)
    ws, s = _windows(d)
    got = [[w["class"], w["input"]["core_ms"], w["input"]["r2_holds"], w["input"]["l2_holds"],
            w["frames"]["window"]["count"], w["frames"]["core_possible"]["count"],
            w["frames"]["core_certain"]["count"], sorted(w["flags"])] for w in ws]
    assert got == golden["windows"]
    assert len(got) == 82
    assert {k: v for k, v in s["windows"].items() if v} == golden["match_summary"]["windows"]
    assert {k: v for k, v in s["clean_windows"].items() if v} == golden["match_summary"]["clean_windows"]
    assert s["clock"]["video_frames"] == golden["match_summary"]["video_frames"]
    assert (s["r2_holds"], s["l2_holds"]) == (golden["match_summary"]["r2_holds"], golden["match_summary"]["l2_holds"])
    assert s["clock"]["fps"] == 60.0
