"""qorcap highlights cut at the stream's measured frame rate (60 fps fix), fail closed when it is unknown or mixed.

Synthetic bundles only; ffmpeg/ffprobe are stubbed (no video is decoded), so this file runs on both CI jobs.
The 30 fps goldens were produced by the pre-fix cutter at main b0d5ad9 (docs/highlights-goldens.md).
"""
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import qorcap_highlights as hl  # noqa: E402

GOLDEN_FILE = HERE / "fixtures" / "highlights_golden_30fps_b0d5ad9.txt"
GOLDEN_SHA256 = "82683e1db0bbdb0c869cff102ed8d4c97e30ff24584df7ec8d459a97900cffdf"

# ---------------------------------------------------------------- 30 fps goldens (pre-fix main b0d5ad9)
# Synthetic bundles only. golden_text() runs make_highlights with ffmpeg stubbed out (cut args are recorded,
# not executed) and returns every timing-dependent output: the -ss/-t strings and exact float reprs passed to
# the cutter, the full highlights manifest (minus generated_ms), every .vtt and the mono bounds used for
# clip signing. The same function, run against the pre-fix code at b0d5ad9, produced GOLDEN_30
# (see docs/highlights-goldens.md).
T0 = 1_792_000_000_000


def _cp(seq, t_ms, away, home):
    ev = {"trigger": "score_change", "score": {"away": away, "home": home, "quarter": 1 + seq % 4, "clock_s": 900 - seq * 37},
          "prev_score": None, "segment_seq": seq, "segment_hash": f"{seq:064x}", "chain_head": f"{seq + 1:064x}",
          "match_id": "local-golden", "role": "home"}
    return {"seq": seq, "t_ms": T0 + t_ms, "frames_hash": hl.jcs_sha(ev), "input_hash": "0" * 64, "prev": "0" * 64,
            "hash": f"{seq + 100:064x}", "qorcap": ev}


def _seg(seq, file, wall, frames, mono):
    return {"type": "segment", "seq": seq, "kind": "media", "file": file, "wall_ms": T0 + wall, "frames": frames,
            "mono_ms": mono, "sha256": f"{seq:064x}", "chain_head": f"{seq + 7:064x}"}


def golden_cases():
    regular = [_seg(i, "video.mp4", i * 2000, i * 60, i * 2000 + 7) for i in range(1, 41)]
    jitter = [_seg(i, "video.mp4", i * 2000 + (i * 37) % 250, round(i * 59.7) + i % 3, i * 2000 + (i * 53) % 190)
              for i in range(1, 41)]
    two = ([_seg(i, "video.mp4", i * 2000, i * 60, i * 2000) for i in range(1, 16)]
           + [_seg(15 + i, "video.part1.mp4", 31_500 + i * 2000, i * 60 - 3, 31_500 + i * 2000 + 11) for i in range(1, 16)])
    scores = [(500, 0, 0), (1_234, 0, 6), (20_000, 0, 7), (33_333, 7, 7), (47_891, 7, 10), (61_111, 7, 13),
              (79_000, 14, 13), (95_000, 14, 16)]
    two_scores = [(100, 0, 0), (9_999, 3, 0), (29_500, 3, 7), (33_000, 3, 8), (45_678, 6, 8), (61_400, 12, 8)]
    return [
        ("regular", regular, scores, {}),
        ("regular-nob-pre3.3-post2.7-max4", regular, scores, {"pre": 3.3, "post": 2.7, "burn": False, "max_clips": 4}),
        ("jitter", jitter, scores, {}),
        ("two-parts", two, two_scores, {}),
        ("two-parts-nob", two, two_scores, {"burn": False, "pre": 12.0, "post": 0.5}),
    ]


def _golden_bundle(root: Path, segs, scores) -> Path:
    b = root / "match"
    b.mkdir()
    init = {"type": "segment", "seq": 0, "kind": "init", "file": "video.mp4", "wall_ms": T0, "frames": 0, "mono_ms": 0,
            "sha256": "aa" * 32, "chain_head": "bb" * 32}
    (b / "manifest.jsonl").write_text("".join(json.dumps(m) + "\n" for m in [init] + segs))
    cps = [_cp(i, t, a, h) for i, (t, a, h) in enumerate(scores)]
    (b / "checkpoints.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cps))
    (b / "receipt.json").write_text(json.dumps({"schema": "qor-receipt/v0", "match": {"match_id": "local-golden"},
                                                "checkpoints": {"home": [{"hash": c["hash"]} for c in cps[::2]]},
                                                "trust_tier": "single-attested", "signatures": {"referee": {"sig": "x"}}}))
    for f in {s["file"] for s in segs}:
        (b / f).write_bytes(b"not a real video")
    return b


def golden_text(rate_stdout: str = "r_frame_rate=30/1\navg_frame_rate=30/1\n") -> str:
    import tempfile
    calls: list[str] = []

    def fake_cut(ffmpeg, src, start, dur, outp, vf):
        calls.append(f"cut {src.name} -ss {start:.3f} -t {dur:.3f} start={start!r} dur={dur!r} vf={'yes' if vf else 'no'}")
        outp.write_bytes(f"{src.name} {start!r} {dur!r}".encode())
        return True, ""

    saved = {k: getattr(hl, k) for k in ("cut", "probe_height", "_font", "_probe_run") if hasattr(hl, k)}
    hl.cut, hl.probe_height, hl._font = fake_cut, (lambda ff, s: 720), (lambda: None)
    if "_probe_run" in saved:  # the fixed code probes the stream; answer like ffprobe on a 30/1 file
        hl._probe_run = lambda cmd: (0, rate_stdout, "")
    out = []
    try:
        for name, segs, scores, kw in golden_cases():
            with tempfile.TemporaryDirectory() as d:
                b = _golden_bundle(Path(d), segs, scores)
                calls.clear()
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    man = hl.make_highlights(b, ffmpeg="ffmpeg", **kw)
                man.pop("generated_ms")
                out.append(f"## {name}")
                out += calls
                out += ["say " + line for line in printed.getvalue().splitlines()]
                out.append(json.dumps(man, sort_keys=True))
                rows = hl.read_jsonl(b / "manifest.jsonl")
                for c in man["clips"]:
                    out.append((b / "highlights" / c["vtt"]).read_text(encoding="utf-8").replace("\n", "|"))
                    s = c["source"]
                    out.append(f"mono {c['id']} {hl.mono_bounds(rows, s['file'], float(s['start_s']), float(s['end_s']))}")
    finally:
        for k, v in saved.items():
            setattr(hl, k, v)
    return "\n".join(out) + "\n"


def test_thirty_fps_output_is_byte_identical_to_the_pre_fix_cutter():
    golden = GOLDEN_FILE.read_bytes()
    assert hashlib.sha256(golden).hexdigest() == GOLDEN_SHA256
    assert golden.count(b"\ncut ") + golden.startswith(b"cut ") == 26
    assert golden_text().encode("utf-8") == golden


class _Stub:
    """Stub ffmpeg/ffprobe for make_highlights: records cuts, answers the rate probe."""

    def __init__(self, monkeypatch, probe_stdout=None, banner=None):
        self.cuts = []
        self.probes = []

        def fake_cut(ffmpeg, src, start, dur, outp, vf):
            self.cuts.append((src.name, start, dur))
            outp.write_bytes(b"clip")
            return True, ""

        def fake_run(cmd):
            self.probes.append(cmd)
            if "-show_entries" in cmd:
                return (0, probe_stdout, "") if probe_stdout is not None else (1, "", "ffprobe: not found")
            return (1, "", banner or "")

        monkeypatch.setattr(hl, "cut", fake_cut)
        monkeypatch.setattr(hl, "probe_height", lambda ff, s: 720)
        monkeypatch.setattr(hl, "_font", lambda: None)
        monkeypatch.setattr(hl, "_probe_run", fake_run)


def _regular(per_seg_frames):
    return [_seg(i, "video.mp4", i * 2000, i * per_seg_frames, i * 2000 + 7) for i in range(1, 41)]


SCORES = golden_cases()[0][2]


def _bundle_in(tmp_path: Path, name: str, segs) -> Path:
    (tmp_path / name).mkdir()
    return _golden_bundle(tmp_path / name, segs, SCORES)


def _frame_clock(b: Path, file: str, step_90k: int, n: int = 40) -> None:
    rows = [{"type": "frame", "file": file, "frame": k, "t_mono_us": 1_000 + k * 16_667, "pts_90k": 900_000 + k * step_90k}
            for k in range(n)]
    (b / "frame_clock.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_sixty_fps_recording_cuts_the_same_seconds_as_thirty(tmp_path, monkeypatch):
    s30 = _Stub(monkeypatch, "r_frame_rate=30/1\navg_frame_rate=30/1\n")
    m30 = hl.make_highlights(_bundle_in(tmp_path, "a", _regular(60)))
    s60 = _Stub(monkeypatch, "r_frame_rate=60/1\navg_frame_rate=60/1\n")
    b60 = _bundle_in(tmp_path, "b", _regular(120))
    m60 = hl.make_highlights(b60)
    assert s30.cuts and s60.cuts == s30.cuts, "a 60 fps file (twice the frames per second) cuts the same wall-clock window"
    assert [c["source"]["start_s"] for c in m60["clips"]] == [c["source"]["start_s"] for c in m30["clips"]]
    assert all(c["source"]["frame_rate"] == "60/1" for c in m60["clips"])
    assert all("frame_rate" not in c["source"] for c in m30["clips"]), "30/1 manifests keep the pre-fix shape"
    # the pre-fix cutter (30 fps assumed) would have started every 60 fps clip at twice the time
    legacy = hl.VideoClock(hl.read_jsonl(b60 / "manifest.jsonl"))
    f, at = legacy.locate(T0 + 20_000)
    assert f == "video.mp4" and abs(at - 40.0) < 1e-9
    f, at = hl.VideoClock(hl.read_jsonl(b60 / "manifest.jsonl"), lambda _f: 60.0).locate(T0 + 20_000)
    assert abs(at - 20.0) < 1e-9
    # credentials map the clip back to the mono clock at the same rate
    rows = hl.read_jsonl(b60 / "manifest.jsonl")
    c = m60["clips"][1]
    assert hl.mono_bounds(rows, "video.mp4", c["source"]["start_s"], c["source"]["end_s"], 60.0) == (12_007, 24_007)
    assert len(s60.probes) == 1, "one probe per video file"


def test_ntsc_rate_is_used_as_measured(tmp_path, monkeypatch):
    s = _Stub(monkeypatch, "r_frame_rate=60000/1001\navg_frame_rate=60000/1001\n")
    b = _golden_bundle(tmp_path, _regular(120), SCORES)
    man = hl.make_highlights(b)
    assert all(c["source"]["frame_rate"] == "60000/1001" for c in man["clips"])
    assert s.cuts[1][1] == max(0.0, 20_000 / 2000 * 120 / (60000 / 1001) - hl.PRE_S)


def test_unknown_rate_skips_the_highlights_fail_closed(tmp_path, monkeypatch, capsys):
    s = _Stub(monkeypatch, probe_stdout=None, banner="no stream line here")
    b = _golden_bundle(tmp_path, _regular(120), SCORES)
    man = hl.make_highlights(b)
    assert s.cuts == [] and man["clips"] == []
    unmeasured = [x for x in man["skipped"] if "frame rate UNMEASURED" in x["reason"]]
    assert len(unmeasured) == 6 and all(x["reason"].endswith("highlight not cut (fail closed)") for x in unmeasured)
    assert (b / "highlights" / "manifest.json").exists()
    assert "highlights for video.mp4 skipped: frame rate UNMEASURED" in capsys.readouterr().out


def test_mixed_rate_skips_the_highlights_fail_closed(tmp_path, monkeypatch):
    s = _Stub(monkeypatch, "r_frame_rate=30/1\n")
    b = _golden_bundle(tmp_path, _regular(120), SCORES)
    _frame_clock(b, "video.mp4", 1500)  # 90 kHz / 1500 = 60 fps, the stream header says 30
    man = hl.make_highlights(b)
    assert s.cuts == [] and man["clips"] == []
    assert any("stream says 30/1 but frame_clock PTS step says 60/1" in x["reason"] for x in man["skipped"])


def test_frame_clock_alone_or_agreeing_gives_the_rate(tmp_path, monkeypatch):
    s = _Stub(monkeypatch, probe_stdout=None)
    b = _bundle_in(tmp_path, "x", _regular(120))
    _frame_clock(b, "video.mp4", 1500)
    man = hl.make_highlights(b)
    assert [c["source"]["frame_rate"] for c in man["clips"]] == ["60/1"] * 6
    s2 = _Stub(monkeypatch, "r_frame_rate=60/1\n")
    b2 = _bundle_in(tmp_path, "y", _regular(120))
    _frame_clock(b2, "video.mp4", 1501)  # within 1 %
    assert len(hl.make_highlights(b2)["clips"]) == 6 and s2.cuts == s.cuts


def test_banner_fallback_when_ffprobe_is_missing(tmp_path, monkeypatch):
    banner = "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 1280x720, 3000 kb/s, 59.94 fps, 59.94 tbr, 90k tbn\n"
    _Stub(monkeypatch, probe_stdout=None, banner=banner)
    man = hl.make_highlights(_golden_bundle(tmp_path, _regular(120), SCORES))
    assert {c["source"]["frame_rate"] for c in man["clips"]} == {"60000/1001"}


def test_parsers_match_the_rust_align_rules():
    assert hl.parse_rational("60/1") == (60, 1) and hl.parse_rational("30000/1001") == (30000, 1001)
    for bad in ("0/0", "60", "x/1", "5000/1", "1/2"):
        assert hl.parse_rational(bad) is None, bad
    assert hl.parse_probe_rate("avg_frame_rate=0/0\nr_frame_rate=60/1\n") == (60, 1)
    assert hl.parse_probe_rate("r_frame_rate=0/0\n") is None
    assert hl.parse_banner_rate("Stream #0:0: Video: h264, 60 fps, 60 tbr, 90k tbn") == (60, 1)
    assert hl.parse_banner_rate("Stream #0:0: Video: h264, 29.97 fps, 29.97 tbr, 90k tbn") == (30000, 1001)
    assert hl.parse_banner_rate("Stream #0:0: Video: h264, 25 fps") == (25, 1)
    assert hl.parse_banner_rate("Stream #0:1: Audio: aac, 48000 Hz") is None
    stamps = [{"file": "v", "frame": k, "t": k, "pts": k * 3000} for k in range(10)]
    assert hl.frame_clock_rate(stamps, "v") is None, "fewer than 10 steps is UNMEASURED"
    stamps.append({"file": "v", "frame": 10, "t": 10, "pts": 30_000})
    assert hl.frame_clock_rate(stamps, "v") == (30, 1)


def test_credentials_use_the_rate_the_clip_was_cut_at(tmp_path):
    b = tmp_path / "match"
    (b / "highlights").mkdir(parents=True)
    rows = [{"type": "segment", "seq": i, "file": "video.mp4", "frames": i * 120, "mono_ms": i * 2000} for i in range(1, 11)]
    (b / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    clips = []
    for n, fr in enumerate([None, "60/1", "garbage"]):
        (b / "highlights" / f"hl{n}.mp4").write_bytes(b"clip")
        src = {"file": "video.mp4", "start_s": 2.0, "end_s": 4.0}
        if fr:
            src["frame_rate"] = fr
        clips.append({"id": f"hl{n}", "file": f"hl{n}.mp4", "overlay": "sidecar", "source": src})
    mp = b / "highlights" / "manifest.json"
    mp.write_text(json.dumps({"schema": hl.SCHEMA, "clips": clips}))
    seen = {}

    def run(cmd):
        seen[Path(cmd[cmd.index("--clip") + 1]).name] = (int(cmd[cmd.index("--start-mono-ms") + 1]),
                                                         int(cmd[cmd.index("--end-mono-ms") + 1]))
        return 4, "", "receipt did not verify"

    man = hl.attach_credentials(b, mp, qorcap="qorcap", run=run)
    assert seen["hl0.mp4"] == (1000, 2000), "an older manifest (no frame_rate) was cut at 30 fps"
    assert seen["hl1.mp4"] == (2000, 4000)
    assert "hl2.mp4" not in seen, "an unreadable rate is never signed"
    assert man["clips"][2]["content_credentials"]["attached"] is False


def test_unknown_rate_never_blocks_finalize_or_touches_the_receipt(tmp_path, monkeypatch):
    import qorcap_match as qm
    _Stub(monkeypatch, probe_stdout=None)
    b = _golden_bundle(tmp_path, _regular(120), SCORES)
    before = (b / "receipt.json").read_bytes()
    qm.post_match_highlights(b, "ffmpeg", qorcap=None)  # never raises
    assert (b / "receipt.json").read_bytes() == before
    man = json.loads((b / "highlights" / "manifest.json").read_text())
    assert man["clips"] == [] and any("UNMEASURED" in x["reason"] for x in man["skipped"])


# ---------------------------------------------------------------- QorProof r164 LOWs (F2, F3, F4)
@pytest.mark.parametrize("tbr", ["nan", "inf"])
def test_nan_or_inf_banner_rate_is_unmeasured_not_a_crash(tmp_path, monkeypatch, tbr):
    banner = f"  Stream #0:0: Video: h264 (High), yuv420p, 1280x720, {tbr} fps, {tbr} tbr, 90k tbn\n"
    assert hl.parse_banner_rate(banner) is None, "Rust maps a non-finite tbr to None"
    s = _Stub(monkeypatch, probe_stdout=None, banner=banner)
    b = _golden_bundle(tmp_path, _regular(120), SCORES)
    man = hl.make_highlights(b)  # must not raise
    assert s.cuts == [] and man["clips"] == []
    unmeasured = [x for x in man["skipped"] if "frame rate UNMEASURED" in x["reason"]]
    assert len(unmeasured) == 6 and len(man["skipped"]) == 7  # + the lock outside the recorded video
    assert (b / "highlights" / "manifest.json").exists()


def test_unreadable_frame_clock_skips_the_file_and_still_writes_the_manifest(tmp_path, monkeypatch):
    s = _Stub(monkeypatch, "r_frame_rate=60/1\navg_frame_rate=60/1\n")
    b = _golden_bundle(tmp_path, _regular(120), SCORES)
    (b / "frame_clock.jsonl").mkdir()  # exists but cannot be read as a file
    man = hl.make_highlights(b)  # must not raise
    assert s.cuts == [] and man["clips"] == [], "a rejected file is never cut"
    rejected = [x for x in man["skipped"] if "frame_clock.jsonl is unreadable" in x["reason"]]
    assert len(rejected) == 6 and len(man["skipped"]) == 7  # + the lock outside the recorded video
    assert all(x["reason"].endswith("(fail closed)") for x in rejected)
    assert json.loads((b / "highlights" / "manifest.json").read_text())["clips"] == []
    assert not list((b / "highlights").glob("*.mp4"))


def test_avg_frame_rate_note_matches_rust(tmp_path, monkeypatch):
    s = _Stub(monkeypatch, "r_frame_rate=60/1\navg_frame_rate=60000/1001\n")
    man = hl.make_highlights(_bundle_in(tmp_path, "ntsc", _regular(120)))
    assert man["notes"] == ["video.mp4: avg_frame_rate 60000/1001 differs from r_frame_rate 60/1 "
                            "(note only; analysed at r_frame_rate)"]
    assert len(man["clips"]) == 6 and {c["source"]["frame_rate"] for c in man["clips"]} == {"60/1"}, "cut at r_frame_rate"
    cuts = list(s.cuts)
    s2 = _Stub(monkeypatch, "r_frame_rate=60/1\navg_frame_rate=5999/100\n")  # duration-rounding drift: no note
    man2 = hl.make_highlights(_bundle_in(tmp_path, "drift", _regular(120)))
    assert "notes" not in man2 and s2.cuts == cuts


def test_avg_rate_note_tolerance_is_the_rust_one():
    r60, r30 = (60, 1), (30, 1)
    rat = hl.parse_rational
    for r, avg in [(r60, "6003/100"), (r60, "5997/100"), (r60, "5999/100"), (r30, "29997/1000"), (r60, "120/2")]:
        assert hl.avg_rate_note("v", r, rat(avg)) is None, avg
    for r, avg in [(r60, "60031/1000"), (r60, "59969/1000"), (r60, "60000/1001"), (r30, "30000/1001"), (r60, "30/1")]:
        assert hl.avg_rate_note("v", r, rat(avg)) is not None, avg
    assert hl.avg_rate_note("v", r60, None) is None
    assert hl.parse_probe_rates("avg_frame_rate=0/0\nr_frame_rate=60/1\n") == ((60, 1), None)
