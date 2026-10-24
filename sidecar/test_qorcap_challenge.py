"""Recorder challenge recheck for the USB capture card + Bluetooth pad (v0.3.15).

Measured on match-20261017-110028: the Madden pause menu is stable about 1.75 s after OPTIONS.
Rule v1, the default verifier, stays 0..600 ms. Rule v2 (opt-in) uses this same clock with a
2500 ms ceiling and a ±300 ms consistency window. This bound is the recorder's recheck only. The
video clock is the fragment PTS through the manifest frame fit, not the segment close stamp.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import cv2
import pytest

HERE = Path(__file__).parent
QT = next((Path(p) for p in [os.getenv("QORTIME", ""), str(HERE.parent / "qortime"), "/tmp/qortime", "/workspace/qortime-src"]
           if p and (Path(p) / "qor_receipt").exists()), None)
sys.path.insert(0, str(HERE))
if QT:
    sys.path.insert(0, str(QT))
    sys.path.insert(0, str(QT / "tests"))
from qorcap_challenge import (  # noqa: E402
    CAPTURE_PAUSE_MAX_LATENCY_MS, capture_clock, capture_pause_detector, frame_mono_ms,
)
from qor_receipt.challenge import MAX_LATENCY_MS, RULE2_CONSISTENCY_MS, RULE2_MAX_LATENCY_MS, verify_response  # noqa: E402

FX = HERE / "fixtures"


def _twice(det, im):
    assert det.update(0, im) is False  # confirm_frames = 2; onset is the first frame
    return det.update(100, im)


def test_capture_detector_sees_both_pause_menus_and_not_gameplay():
    assert CAPTURE_PAUSE_MAX_LATENCY_MS == 2500
    assert MAX_LATENCY_MS == 600  # rule v1, still the default verifier
    assert RULE2_MAX_LATENCY_MS == 2500 and RULE2_CONSISTENCY_MS == 300
    kick = cv2.imread(str(FX / "m27_std_110028_pause_kick.jpg"))
    end = cv2.imread(str(FX / "m27_std_110028_pause_end.jpg"))
    pre = cv2.imread(str(FX / "m27_std_110028_pre_pause.jpg"))
    assert _twice(capture_pause_detector(), kick) is True
    assert _twice(capture_pause_detector(), end) is True
    assert _twice(capture_pause_detector(), pre) is False


def test_recorder_bound_accepts_1750_ms_and_rejects_a_relay():
    press = 1_000
    ev = [{"t_ms": press, "button": "OPTIONS", "down": True}]
    ok = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000,
                         pause_seen_ms=press + 1750, max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
    assert ok.ok and ok.reason == "ok" and ok.latency_ms == 1750
    late = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000,
                           pause_seen_ms=press + 2600, max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
    assert not late.ok and late.reason == "video_latency_too_high"
    early = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000,
                            pause_seen_ms=press - 100, max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
    assert not early.ok and early.reason == "video_before_input"
    # The same 1.75 s onset still fails rule v1. The rule v2 ceiling accepts it; consistency is separate.
    official = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000, pause_seen_ms=press + 1750)
    assert not official.ok and official.reason == "video_latency_too_high"
    v2 = verify_response(["OPTIONS"], ev, issued_ms=0, deadline_ms=10_000, pause_seen_ms=press + 1750,
                         max_latency_ms=RULE2_MAX_LATENCY_MS)
    assert v2.ok and v2.latency_ms == 1750


def test_capture_clock_extrapolates_frame_zero_behind_the_first_close():
    """match-110028: the first close is frame 129 at mono 6768, and frame 0 is ~2646, not close-2000."""
    segs = [{"kind": "init", "frames": 129, "mono_ms": 6768}]
    for fr in range(129, 129 + 61 * 20, 61):
        segs.append({"kind": "media", "frames": fr, "mono_ms": int(round(2646.3 + 33.3346 * fr))})
    intercept, slope = capture_clock(segs)
    assert abs(intercept - 2646.3) < 5
    assert abs(slope - 33.3346) < 0.01
    assert abs(intercept - (6768 - 2000)) > 1000  # the segment-0 fallback (11:00:42.5) is not frame 0


def test_capture_clock_keeps_pts_as_mono_when_frames_do_not_advance():
    segs = [{"kind": "media", "frames": 60, "mono_ms": i * 2000} for i in range(1, 15)]
    assert capture_clock(segs) == (0.0, 1000.0 / 30.0)


def test_frame_fit_stamps_the_110028_pause_at_1750_ms():
    """The three OPTIONS presses, and the frame 1750 ms later on the manifest frame fit.

    The v0.3.14 recheck reported 3593 / 3641 / 3522 because it stamped that same frame from the
    close. Handoff JPEGs (0.25 s grid, same CDT clock) let the capture detector accept the menu at
    +1728 / +1654 / +1711; the 30 fps onset is inside the preceding quarter-second. All of those
    are under the 2500 ms bound.
    """
    clock = (2646.3, 33.3346)
    wmm = 1_792_252_837_704
    presses = {"kickoff": 1_792_253_007_922, "halftime": 1_792_253_533_896, "end": 1_792_254_134_839}
    jpeg_onset = {"kickoff": 1728, "halftime": 1654, "end": 1711}
    for phase, press in presses.items():
        true_mono = (press + 1750) - wmm
        pts0 = (true_mono - clock[0]) / clock[1] * (1000.0 / 30.0)
        wall = frame_mono_ms(clock, pts0, 0, 30.0) + wmm
        latency = wall - press
        assert abs(latency - 1750) <= 2
        # Handoff JPEGs are 250 ms apart. The first frame the detector accepts is the ~1.75 s menu.
        assert 0 <= jpeg_onset[phase] <= CAPTURE_PAUSE_MAX_LATENCY_MS
        assert abs(jpeg_onset[phase] - 1750) <= 250


@pytest.mark.skipif(QT is None or shutil.which("ffmpeg") is None, reason="needs qortime and ffmpeg")
def test_close_stamp_lag_does_not_move_the_pause(tmp_path):
    """A fragment hashed 4 s after its frames must still report the fixture's ~200 ms pause, not ~4200."""
    import review_fixture as F

    import qorcap_challenge as C

    F.build(tmp_path / "m")
    man_path = tmp_path / "m" / "manifest.jsonl"
    rows = [json.loads(line) for line in man_path.read_text().splitlines() if line.strip()]
    lag = 4_000
    for m in rows:
        if m.get("type") != "segment":
            continue
        m["mono_ms"] = int(m["mono_ms"]) + lag
        m["wall_ms"] = int(m["wall_ms"]) + lag
        if m.get("kind") == "media":
            m["frames"] = int(round(m["mono_ms"] * 30 / 1000))
    man_path.write_text("".join(json.dumps(m) + "\n" for m in rows))
    chs = [dict(c, result=None) for c in json.loads((tmp_path / "m" / "receipt.json").read_text())["challenges"]]
    outs = C.challenge_results(tmp_path / "m", chs)
    lats = [o["local"].get("latency_ms") for o in outs]
    assert all(o["result"] and o["result"]["ok"] for o in outs), [o["local"] for o in outs]
    assert all(isinstance(x, int) and 0 <= x <= 600 for x in lats), lats


def test_fill_challenge_results_uses_the_recorder_recheck():
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert "from qorcap_challenge import challenge_results" in src
    chal = (HERE / "qorcap_challenge.py").read_text(encoding="utf-8")
    assert "max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS" in chal
    assert 'res["reverified_by"] = "recorder"' in chal
    body = chal.split("def challenge_results", 1)[1]
    assert 'res["rule"]' not in body and "res['rule']" not in body
    assert "capture_pause_max_ms=CAPTURE_PAUSE_MAX_LATENCY_MS" in chal
    assert "def receipt_result(" in chal
    assert "mismatch_log_trustworthy(" in body
    assert "b.inputs + b.input_chunks" in body
    assert "receipt_result(res)" in body
    assert 'return {"result": res if res["ok"] else None, "local": local}' not in chal


_NON_OK_REASONS = (
    "no_pause_press",
    "pattern_mismatch",
    "pause_not_seen_on_video",
    "video_before_input",
    "video_latency_too_high",
)


def _challenge(nonce: str, phase: str, issued: int) -> dict:
    from qor_receipt.challenge import pattern_for

    return {"id": nonce[:8], "role": "home", "phase": phase, "nonce": nonce,
            "pattern": pattern_for(nonce), "issued_ms": issued, "deadline_ms": issued + 90_000, "result": None}


def _bits():
    from qor_receipt.review.bundle import BUTTON_BITS

    return {name: bit for name, bit in BUTTON_BITS}


def _header(anchor_ms: int = 0) -> dict:
    return {"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0, "seg": 0, "wall_anchor_ms": anchor_ms}


def _press(t_ms: int, button: str) -> dict:
    return {"type": "in", "t_us": t_ms * 1000, "b": 1 << _bits()[button], "seg": 0}


def _status(t_ms: int, state: str) -> dict:
    return {"type": "status", "t_us": t_ms * 1000, "state": state, "detail": state, "seg": 0}


def _chunk(t_ms: int, seq: int = 1, n: int = 1) -> dict:
    """A per-segment chunk row. An idle pad from 0.3.34 still writes one with ``n`` 0."""
    return {"type": "chunk", "seq": seq, "seg": seq, "n": n, "sha256": "ab" * 32,
            "prev": "cd" * 32, "head": "ef" * 32, "t_us": t_ms * 1000}


def _chunk_span(issued_ms: int, deadline_ms: int, step: int = 2_000, *, n: int = 1,
                start: int | None = None, until: int | None = None) -> list[dict]:
    """Chunk rows from one step before the window through the deadline.

    ``start`` is the first stamp (default one step before ``issued_ms``). ``until`` is the
    last stamp (default ``deadline_ms``). ``n`` is written on every row.
    """
    rows = []
    t = issued_ms - step if start is None else start
    end = deadline_ms if until is None else until
    seq = 0
    if t > end:
        return rows
    while t < end:
        rows.append(_chunk(t, seq, n))
        seq += 1
        t += step
    rows.append(_chunk(end, seq, n))
    return rows


def _kickoff():
    return _challenge("00112233445566778899aabbccddeeff", "kickoff", 1_000_000)


def _phases(kick: dict) -> list[dict]:
    return [
        kick,
        _challenge("ffeeddccbbaa99887766554433221100", "halftime", 2_000_000),
        _challenge("0123456789abcdef0123456789abcdef", "end", 3_000_000),
    ]


def _wrong_button(pattern: list[str]) -> str:
    return next(b for b in ("CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT") if b != pattern[1])


def _fill(bundle: Path, rows: list[dict], challenges: list[dict], *, chop: int = 0,
          tear_after: int | None = None, inject: tuple[int, str] | None = None) -> dict:
    import qorcap_match as qm

    bundle.mkdir(parents=True)
    lines = [json.dumps(r) for r in rows]
    if tear_after is not None:
        lines.insert(tear_after, '{"type": "in", "t_us":')
    if inject is not None:
        lines.insert(inject[0], inject[1])
    raw = ("\n".join(lines) + "\n").encode()
    if chop:
        raw = raw[:-chop]
    (bundle / "inputs.jsonl").write_bytes(raw)
    for name in ("bundle.json", "receipt.json"):
        (bundle / name).write_text("{}\n", encoding="utf-8")
    for name in ("manifest.jsonl", "checkpoints.jsonl"):
        (bundle / name).write_text("", encoding="utf-8")
    # decode=False: the pattern decision does not wait on a pause frame.
    return qm.fill_challenge_results(bundle, {"challenges": challenges}, decode=False)


def _mismatch_bundle(bundle: Path) -> dict:
    """Kickoff presses OPTIONS then a button that is not the prompt. The pad stays connected."""
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    rows = [
        _header(),
        _status(0, "connected"),
        _press(t0, "OPTIONS"),
        _press(t0 + 400, _wrong_button(kick["pattern"])),
        *_chunk_span(kick["issued_ms"], kick["deadline_ms"]),
    ]
    return _fill(bundle, rows, _phases(kick))


def _signed_from_challenges(tmp: Path, challenges: list[dict]):
    from qor_receipt.checkpoints import Chain
    from qor_receipt.receipt import verify_receipt

    import qorcap_match as qm

    dev, ref = qm.load_keys(tmp / "keys")
    t = [1000]
    cs = qm.Countersigner(ref, "local-mismatch", "home", clock=lambda: t[0] + 50)
    chain = Chain()
    for _ in range(3):
        cp = chain.next(t[0], "a" * 64, "b" * 64)
        assert cs.offer(cp)
        t[0] += 2000
    draft = {
        "match": {"match_id": "local-mismatch", "game": "madden27", "platform": "ps5",
                  "scorebug_profile": "madden27_standard", "mode": "single", "started_ms": 900, "ended_ms": 0},
        "result": {"home_score": 14, "away_score": 10, "quarter": 4, "final_t_ms": 5000, "winner": "home",
                   "method": "agreed_scorebug_final", "final_basis": "clock_zero"},
        "players": [{"role": "home", "player_id": "psn:me", "pubkey": "0" * 64, "key_kind": "software"}],
        "challenges": challenges,
        "agreement": {"timelines_agree": None, "mismatches": []},
        "signals": {"motion_permille": {"home": None}},
    }
    rec = qm.build_receipt(draft, cs.signed, dev, ref, match_id="local-mismatch", role="home", started_ms=900)
    return rec, verify_receipt(rec, referee_pub=ref.pubkey_hex)


def test_pattern_mismatch_verifies_as_challenge_failed(tmp_path, capsys):
    """match-20261024-135645: a wrong pattern is challenge_failed, never challenge_unanswered."""
    import qorcap_challenge as C

    filled = _mismatch_bundle(tmp_path / "bundle")
    by_phase = {c["phase"]: c for c in filled["challenges"]}
    kick = by_phase["kickoff"]["result"]
    assert kick["ok"] is False and kick["reason"] == "pattern_mismatch"
    assert kick["reverified_by"] == "recorder" and "rule" not in kick
    assert isinstance(kick["hid_sha256"], str) and len(kick["hid_sha256"]) == 64
    assert kick["done_ms"] is None and kick["latency_ms"] is None
    assert isinstance(kick["pause_press_ms"], int)
    assert by_phase["halftime"]["result"] is None and by_phase["end"]["result"] is None
    lines = [json.loads(x) for x in (tmp_path / "bundle" / "challenge_results.jsonl").read_text().splitlines()]
    assert lines[0]["reason"] == "pattern_mismatch" and lines[0]["ok"] is False
    out = capsys.readouterr().out
    assert "FAILED (pattern_mismatch)" in out and "PASSED" not in out

    import jsonschema

    schema = json.loads((HERE.parent / "contracts" / "receipt.v0.json").read_text())
    rec, v = _signed_from_challenges(tmp_path / "sign", filled["challenges"])
    jsonschema.validate(rec, schema)
    assert v["status"] == "FAIL"
    assert "home:kickoff:challenge_failed:pattern_mismatch" in v["errors"]
    assert "home:challenge_unanswered:kickoff" not in v["partial"]
    assert "home:challenge_unanswered:halftime" in v["partial"]
    assert "home:challenge_unanswered:end" in v["partial"]
    assert C.receipt_result({"ok": False, "reason": "no_pause_press"}) is None
    assert C.receipt_result({"ok": False, "reason": "pause_not_seen_on_video"}) is None
    assert C.receipt_result({"ok": True, "reason": "ok"}) == {"ok": True, "reason": "ok"}


def test_null_result_mutation_hides_the_mismatch(tmp_path, monkeypatch):
    """The pre-fix drop (`ok` else null) must fail the mismatch check, not pass it."""
    import qorcap_challenge as C

    real, v_real = _signed_from_challenges(tmp_path / "real-sign", _mismatch_bundle(tmp_path / "real")["challenges"])
    assert real["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    assert "home:kickoff:challenge_failed:pattern_mismatch" in v_real["errors"]
    assert "home:challenge_unanswered:kickoff" not in v_real["partial"]

    monkeypatch.setattr(C, "receipt_result", lambda res: res if isinstance(res, dict) and res.get("ok") else None)
    mutated = _mismatch_bundle(tmp_path / "mutated")
    assert mutated["challenges"][0]["result"] is None
    _rec, v_bad = _signed_from_challenges(tmp_path / "mut-sign", mutated["challenges"])
    assert "home:challenge_unanswered:kickoff" in v_bad["partial"]
    assert "home:kickoff:challenge_failed:pattern_mismatch" not in v_bad["errors"]
    assert v_bad["status"] == "SELF_ATTESTED"


def test_only_pattern_mismatch_among_non_ok_reasons_is_stored():
    """video_before_input and video_latency_too_high stay null. Widening the allowlist would store them."""
    import qorcap_challenge as C

    def widened(res):
        if isinstance(res, dict) and res.get("ok") is True:
            return res
        if isinstance(res, dict) and res.get("reason") in {"pattern_mismatch", "video_before_input", "video_latency_too_high"}:
            return res
        return None

    for reason in _NON_OK_REASONS:
        body = {"ok": False, "reason": reason, "pause_press_ms": 1, "done_ms": None, "latency_ms": None}
        got = C.receipt_result(body)
        if reason == "pattern_mismatch":
            assert got is not None and got["reason"] == "pattern_mismatch"
        else:
            assert got is None
        if reason in {"video_before_input", "video_latency_too_high"}:
            assert widened(body) is not None and got is None


def test_options_alone_and_late_rest_stay_failed(tmp_path):
    """QorArch: these two match the referee, so a trustworthy log stores pattern_mismatch."""
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    header = _header()
    span = _chunk_span(kick["issued_ms"], kick["deadline_ms"])
    alone = _fill(tmp_path / "alone", [header, _status(0, "connected"), _press(t0, "OPTIONS"), *span], _phases(kick))
    assert alone["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    _rec, v_alone = _signed_from_challenges(tmp_path / "alone-sign", alone["challenges"])
    assert "home:kickoff:challenge_failed:pattern_mismatch" in v_alone["errors"]
    assert "home:challenge_unanswered:kickoff" not in v_alone["partial"]

    late_rows = [header, _status(0, "connected"), _press(t0, "OPTIONS")]
    for i, button in enumerate(kick["pattern"][1:]):
        late_rows.append(_press(kick["deadline_ms"] + 1_000 + i * 400, button))
    late_rows.extend(_chunk_span(kick["issued_ms"], kick["deadline_ms"]))
    late = _fill(tmp_path / "late", late_rows, _phases(kick))
    assert late["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    _rec, v_late = _signed_from_challenges(tmp_path / "late-sign", late["challenges"])
    assert "home:kickoff:challenge_failed:pattern_mismatch" in v_late["errors"]
    assert "home:challenge_unanswered:kickoff" not in v_late["partial"]


def _glitch_rows(kind: str) -> tuple[list[dict], int, int | None]:
    """Presses that verify_response calls pattern_mismatch, plus one controller glitch.

    The third value is the line index of a torn record inserted into an otherwise complete file.
    chop is how many bytes to cut off the tail. A tail chunk keeps coverage from hiding a status glitch.
    """
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    header = _header()
    connected = _status(0, "connected")
    options = _press(t0, "OPTIONS")
    wrong = _press(t0 + 400, _wrong_button(kick["pattern"]))
    span = _chunk_span(kick["issued_ms"], kick["deadline_ms"])
    if kind == "stall":
        return [header, connected, options, _press(t0 + 400, kick["pattern"][1]),
                _status(t0 + 800, "stalled"), *span], 0, None
    if kind == "unplug":
        return [header, connected, options, _status(t0 + 200, "unplugged"), *span], 0, None
    if kind == "reconnect":
        return [
            header, connected, options,
            _status(t0 + 200, "unplugged"),
            _status(t0 + 800, "connected"),
            _press(t0 + 1_200, _wrong_button(kick["pattern"])),
            *span,
        ], 0, None
    if kind == "truncated":
        # The span is intact. Chopping the last press leaves a partial line after a covered window.
        return [header, connected, options, *span, wrong], 12, None
    if kind == "stall_after":
        # stalled is written only after 2 s with no reports, so it can land just past the deadline.
        return [header, connected, options, wrong, _status(kick["deadline_ms"] + 500, "stalled"), *span], 0, None
    if kind == "clean_cut":
        # A crash drops the pending batch. The file ends on a complete line inside the window.
        return [header, connected, options, wrong], 0, None
    if kind == "torn":
        # A bad line in the middle; the last line and the chunk span are intact.
        return [header, connected, options, wrong, *span], 0, 3
    raise AssertionError(kind)


def _glitch_fill(bundle: Path, kind: str) -> dict:
    rows, chop, tear_after = _glitch_rows(kind)
    return _fill(bundle, rows, _phases(_kickoff()), chop=chop, tear_after=tear_after)


@pytest.mark.parametrize("kind", ["stall", "unplug", "reconnect", "truncated", "stall_after", "clean_cut", "torn"])
def test_controller_glitch_stays_unanswered(tmp_path, kind):
    """A recorder or controller gap is not a signed player failure."""
    import qorcap_challenge as C

    filled = _glitch_fill(tmp_path / "bundle", kind)
    assert filled["challenges"][0]["result"] is None
    line = json.loads((tmp_path / "bundle" / "challenge_results.jsonl").read_text().splitlines()[0])
    assert line["reason"] == "pattern_mismatch" and line["ok"] is False
    assert line["withheld"] == "controller_log"
    path = tmp_path / "bundle" / "inputs.jsonl"
    if kind in {"truncated", "torn"}:
        assert C.inputs_log_intact(path) is False
    if kind == "torn":
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        json.loads(lines[-1])
        with pytest.raises(json.JSONDecodeError):
            json.loads(lines[3])
    if kind == "clean_cut":
        assert C.inputs_log_intact(path) is True
    _rec, v = _signed_from_challenges(tmp_path / "sign", filled["challenges"])
    assert "home:challenge_unanswered:kickoff" in v["partial"]
    assert "home:kickoff:challenge_failed:pattern_mismatch" not in v["errors"]
    assert v["status"] == "SELF_ATTESTED"


@pytest.mark.parametrize("kind", ["stall", "unplug", "reconnect", "truncated", "stall_after", "clean_cut", "torn"])
def test_ignoring_the_controller_log_signs_the_glitch_as_failed(tmp_path, monkeypatch, kind):
    """Mutation: trusting every pattern_mismatch signs the glitch as challenge_failed."""
    import qorcap_challenge as C

    monkeypatch.setattr(C, "mismatch_log_trustworthy", lambda *a, **k: True)
    filled = _glitch_fill(tmp_path / "bundle", kind)
    assert filled["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    assert "withheld" not in json.loads((tmp_path / "bundle" / "challenge_results.jsonl").read_text().splitlines()[0])
    _rec, v = _signed_from_challenges(tmp_path / "sign", filled["challenges"])
    assert "home:kickoff:challenge_failed:pattern_mismatch" in v["errors"]
    assert "home:challenge_unanswered:kickoff" not in v["partial"]
    assert v["status"] == "FAIL"


def test_fail_verdict_exits_2_and_skips_the_submit_prompt():
    import qorcap_match as qm

    assert qm.EXIT_CODES["FAIL"] == 2
    assert qm.overall("FAIL", True) == "FAIL"
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert "if code != 2 and (a.submit or not a.no_submit_prompt) and not a.test_pattern:" in src
    readme = (HERE.parent / "README.md").read_text(encoding="utf-8")
    assert "exits 2 and does not ask to submit" in readme
    assert "qorcap submit" in readme


def test_issue_135645_shape_stays_failed(tmp_path):
    """OPTIONS, then a button that is not next, then the rest of the prompt. That stays FAIL."""
    kick = _kickoff()
    pat = kick["pattern"]
    assert pat[0] == "OPTIONS"
    t0 = kick["issued_ms"] + 1_000
    rows = [_header(), _status(0, "connected"), _press(t0, "OPTIONS"), _press(t0 + 200, _wrong_button(pat))]
    for i, button in enumerate(pat[1:]):
        rows.append(_press(t0 + 600 + i * 200, button))
    rows.extend(_chunk_span(kick["issued_ms"], kick["deadline_ms"]))
    filled = _fill(tmp_path / "bundle", rows, _phases(kick))
    assert filled["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    _rec, v = _signed_from_challenges(tmp_path / "sign", filled["challenges"])
    assert v["status"] == "FAIL"
    assert "home:kickoff:challenge_failed:pattern_mismatch" in v["errors"]
    assert "home:challenge_unanswered:kickoff" not in v["partial"]


def _boundary(tmp_path: Path, name: str, rows: list[dict]):
    filled = _fill(tmp_path / name, rows, _phases(_kickoff()))
    local = json.loads((tmp_path / name / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    return filled["challenges"][0]["result"], local


def test_window_boundaries(tmp_path):
    """Connected exactly at the open is stored. A stall on either edge, or at the end of the lag, is not."""
    import qorcap_challenge as C

    assert C.STALL_LAG_MS == 2_500
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    t0 = issued + 1_000
    presses = [_press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"]))]

    span = _chunk_span(issued, deadline)
    stored, local = _boundary(
        tmp_path, "at-issued",
        [_header(), _status(issued, "connected"), *presses, *span])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local

    for name, stall_at in (
        ("stall-issued", issued),
        ("stall-deadline", deadline),
        ("stall-lag", deadline + C.STALL_LAG_MS),
    ):
        stored, local = _boundary(
            tmp_path, name,
            [_header(), _status(0, "connected"), *presses, _status(stall_at, "stalled"), *span])
        assert stored is None and local["withheld"] == "controller_log", name

    stored, local = _boundary(
        tmp_path, "past-lag",
        [_header(), _status(0, "connected"), *presses,
         _status(deadline + C.STALL_LAG_MS + 1, "stalled"), *span])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local


def test_no_status_rows_stay_unanswered(tmp_path):
    """A bundle with no status rows has no proof the pad was connected. It stays null."""
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    rows = [_header(), _press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"])),
            *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    stored, local = _boundary(tmp_path, "bundle", rows)
    assert stored is None
    assert local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_status_rows_are_sorted_before_the_window_check(tmp_path):
    """A later stall written above the connected row must not hide a connected pad, or a real stall."""
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    t0 = issued + 1_000
    presses = [_press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"]))]
    stored, local = _boundary(tmp_path, "late-first", [
        _header(),
        _status(deadline + 10_000, "stalled"),
        _status(issued, "connected"),
        *presses,
        *_chunk_span(issued, deadline),
    ])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local

    stored, local = _boundary(tmp_path, "stall-first", [
        _header(),
        _status(t0 + 800, "stalled"),
        _status(0, "connected"),
        *presses,
        *_chunk_span(issued, deadline),
    ])
    assert stored is None and local["withheld"] == "controller_log"


def test_nonzero_wall_anchor_is_applied(tmp_path):
    """Status and chunk times use header wall_anchor_ms. Dropping it would hide this mismatch."""
    anchor = 5_000_000
    kick = _challenge("00112233445566778899aabbccddeeff", "kickoff", anchor)
    wrong = _wrong_button(kick["pattern"])
    # Helpers store t_us from a mono millisecond. Wall time is anchor + that millisecond.
    rows = [
        _header(anchor),
        _status(0, "connected"),
        _press(1_000, "OPTIONS"),
        _press(1_400, wrong),
        *_chunk_span(0, 90_000),
    ]
    filled = _fill(tmp_path / "store", rows, [kick])
    assert filled["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    stalled = [
        _header(anchor),
        _status(0, "connected"),
        _press(1_000, "OPTIONS"),
        _press(1_400, wrong),
        _status(1_000, "stalled"),
        *_chunk_span(0, 90_000),
    ]
    filled = _fill(tmp_path / "stall", stalled, [kick])
    assert filled["challenges"][0]["result"] is None
    line = json.loads((tmp_path / "stall" / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["withheld"] == "controller_log"


def test_malformed_status_time_stays_unanswered(tmp_path):
    """A status row whose t_us is not an integer fails closed, instead of being skipped."""
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    bad = {"type": "status", "t_us": float((t0 + 800) * 1000), "state": "stalled", "detail": "stalled", "seg": 0}
    rows = [_header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"])), bad,
            *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    stored, local = _boundary(tmp_path, "bundle", rows)
    assert stored is None and local["withheld"] == "controller_log"


def test_connected_after_a_stall_needs_the_wall_anchor(tmp_path):
    """Dropping wall_anchor_ms hides an in-window stall behind the later connected row.

    Anchor 400000. The stall is inside the window and a connected row follows it, so the
    mismatch stays null. Without the offset both rows fall before issued_ms, the later
    connected row becomes the state at the open, and the same bundle is stored as FAIL.
    """
    anchor = 400_000
    kick = _challenge("00112233445566778899aabbccddeeff", "kickoff", anchor)
    wrong = _wrong_button(kick["pattern"])
    # Mono milliseconds. Wall time is anchor + mono. The span also reaches the raw deadline,
    # so a gate that drops the offset still has chunk coverage and can only flip on status.
    rows = [
        _header(anchor),
        _status(0, "connected"),
        _press(500, "OPTIONS"),
        _press(900, wrong),
        _status(1_000, "stalled"),
        _status(2_000, "connected"),
        *_chunk_span(0, kick["deadline_ms"]),
    ]
    filled = _fill(tmp_path / "bundle", rows, [kick])
    assert filled["challenges"][0]["result"] is None
    line = json.loads((tmp_path / "bundle" / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["reason"] == "pattern_mismatch" and line["withheld"] == "controller_log"


@pytest.mark.parametrize("literal", ["42", "[]"])
def test_non_object_jsonl_line_stays_unanswered(tmp_path, literal):
    """A line that parses but is not an object is malformed. It is not a player FAIL."""
    import qorcap_challenge as C

    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    rows = [_header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"])),
            *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    filled = _fill(tmp_path / "bundle", rows, _phases(kick), inject=(2, literal))
    assert C.inputs_log_intact(tmp_path / "bundle" / "inputs.jsonl") is False
    assert filled["challenges"][0]["result"] is None
    line = json.loads((tmp_path / "bundle" / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["reason"] == "pattern_mismatch" and line["withheld"] == "controller_log"


def test_second_header_stays_unanswered(tmp_path):
    """Two header rows make the log malformed. The mismatch is not signed."""
    import qorcap_challenge as C

    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    rows = [_header(), _header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"])),
            *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    filled = _fill(tmp_path / "bundle", rows, _phases(kick))
    assert C.inputs_log_intact(tmp_path / "bundle" / "inputs.jsonl") is False
    assert filled["challenges"][0]["result"] is None
    line = json.loads((tmp_path / "bundle" / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["reason"] == "pattern_mismatch" and line["withheld"] == "controller_log"


def test_chunk_coverage_must_be_continuous(tmp_path):
    """One far-future chunk, or a crash gap before post-deadline rows, does not cover the window."""
    import qorcap_challenge as C

    assert C.CHUNK_INTERVAL_MS == 2_000 and C.CHUNK_GAP_SLACK_MS == 1_000
    assert C.CHUNK_GAP_MAX_MS == 3_000
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    t0 = issued + 1_000
    head = [_header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"]))]

    far = _fill(tmp_path / "far", [*head, _chunk(deadline + 60_000)], _phases(kick))
    assert far["challenges"][0]["result"] is None
    gap = _fill(
        tmp_path / "gap",
        [*head, *_chunk_span(issued, issued + 10_000), _chunk(deadline), _chunk(deadline + 2_000, seq=99)],
        _phases(kick))
    assert gap["challenges"][0]["result"] is None
    for name in ("far", "gap"):
        line = json.loads((tmp_path / name / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert line["withheld"] == "controller_log" and line["reason"] == "pattern_mismatch"

    # A gap of exactly the bound still stores. One millisecond over it withholds.
    stored, local = _boundary(tmp_path, "at-bound", [*head, *_chunk_span(issued, deadline, step=C.CHUNK_GAP_MAX_MS)])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local
    stored, local = _boundary(
        tmp_path, "over-bound", [*head, *_chunk_span(issued, deadline, step=C.CHUNK_GAP_MAX_MS + 1)])
    assert stored is None and local["withheld"] == "controller_log"


def _mismatch_head(kick: dict) -> list[dict]:
    t0 = kick["issued_ms"] + 1_000
    return [_header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"]))]


def test_idle_pad_empty_chunks_store_pattern_mismatch(tmp_path):
    """An idle pad seals a chunk every 2 s with n 0. That window stores the mismatch.

    seal() used to return before writing when n was 0, so a real idle stretch left gaps
    over 3 s and the gate withheld the window.
    """
    kick = _kickoff()
    rows = [*_mismatch_head(kick), *_chunk_span(kick["issued_ms"], kick["deadline_ms"], n=0)]
    assert [r["n"] for r in rows if r["type"] == "chunk"]
    assert all(r["n"] == 0 for r in rows if r["type"] == "chunk")
    stored, local = _boundary(tmp_path, "idle", rows)
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local


def test_chunk_after_issue_does_not_open_coverage(tmp_path):
    """The first chunk is 1 ms after issue and nothing is at or before it. That withholds.

    A gate that let a chunk after issued_ms open coverage would store this.
    """
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    rows = [*_mismatch_head(kick), *_chunk_span(issued, deadline, start=issued + 1)]
    assert all(r["t_us"] // 1000 > issued for r in rows if r["type"] == "chunk")
    stored, local = _boundary(tmp_path, "after-issue", rows)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_last_chunk_before_deadline_withholds(tmp_path):
    """The last chunk is 1 ms before the deadline and nothing is at or after it. That withholds.

    Steps stay inside 3000 ms, and the deadline is 1 ms after the last chunk. A gate that
    stops once the steps fit, or that treats the end as covered when deadline - last <= 3000,
    would store this.
    """
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    rows = [*_mismatch_head(kick), *_chunk_span(issued, deadline, start=issued, until=deadline - 1)]
    times = sorted(r["t_us"] // 1000 for r in rows if r["type"] == "chunk")
    assert times[0] == issued and times[-1] == deadline - 1
    assert all(times[i] - times[i - 1] <= 3_000 for i in range(1, len(times)))
    assert all(t < deadline for t in times)
    stored, local = _boundary(tmp_path, "before-deadline", rows)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_chunk_exactly_at_issue_opens_coverage(tmp_path):
    """A chunk stamped exactly at issued_ms opens coverage when nothing is earlier.

    Changing the opening comparison from <= issued_ms to < issued_ms would withhold this.
    """
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    rows = [*_mismatch_head(kick), *_chunk_span(issued, deadline, start=issued)]
    times = sorted(r["t_us"] // 1000 for r in rows if r["type"] == "chunk")
    assert times[0] == issued and all(t >= issued for t in times) and times[-1] >= deadline
    stored, local = _boundary(tmp_path, "at-issue", rows)
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local


def test_status_rows_are_not_chunk_coverage(tmp_path):
    """Status rows across the window are not chunk rows. Dropping the type filter would store this."""
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    rows = _mismatch_head(kick)
    t = 0
    while t <= deadline:
        rows.append(_status(t, "connected"))
        t += 2_000
    assert not any(r["type"] == "chunk" for r in rows)
    stored, local = _boundary(tmp_path, "status-only", rows)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_issued_equal_deadline_with_a_chunk_at_that_instant_stores(tmp_path):
    """issued_ms == deadline_ms and one chunk at that instant covers.

    The early ``prev >= deadline_ms`` return is what accepts this. Flipping it to false
    withholds only this shape. The press on that instant is inside the window.
    """
    import qorcap_challenge as C

    kick = _kickoff()
    issued = kick["issued_ms"]
    kick = dict(kick, deadline_ms=issued)
    wrong = _wrong_button(kick["pattern"])
    rows = [
        _header(),
        _status(0, "connected"),
        _press(issued, "OPTIONS"),
        _press(issued, wrong),
        _chunk(issued, 0),
    ]
    assert C.chunks_cover(rows, issued_ms=issued, deadline_ms=issued, anchor_ms=0) is True
    filled = _fill(tmp_path / "equal", rows, [kick])
    assert filled["challenges"][0]["result"]["reason"] == "pattern_mismatch"
    line = json.loads((tmp_path / "equal" / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["reason"] == "pattern_mismatch" and "withheld" not in line


def test_float_chunk_time_is_ignored(tmp_path):
    """A float t_us is not an integer, so it does not close the window."""
    import qorcap_challenge as C

    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    rows = [*_mismatch_head(kick), *_chunk_span(issued, deadline)]
    assert rows[-1]["t_us"] == deadline * 1000
    rows[-1]["t_us"] = float(deadline * 1000)
    assert C.chunks_cover(rows, issued_ms=issued, deadline_ms=deadline, anchor_ms=0) is False
    stored, local = _boundary(tmp_path, "float-chunk", rows)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_bool_chunk_time_is_ignored(tmp_path):
    """A bool t_us is not a timestamp. Treating True as 1 would open coverage at 0."""
    import qorcap_challenge as C

    assert C.chunks_cover([{"type": "chunk", "t_us": True}], issued_ms=0, deadline_ms=0, anchor_ms=0) is False
    assert C.chunks_cover([{"type": "chunk", "t_us": False}], issued_ms=0, deadline_ms=0, anchor_ms=0) is False
    assert C.chunks_cover([{"type": "chunk", "t_us": 0}], issued_ms=0, deadline_ms=0, anchor_ms=0) is True
    kick = _challenge("00112233445566778899aabbccddeeff", "kickoff", 0)
    rows = [
        _header(),
        _status(0, "connected"),
        _press(1_000, "OPTIONS"),
        _press(1_400, _wrong_button(kick["pattern"])),
        {"type": "chunk", "seq": 0, "seg": 0, "n": 0, "sha256": "ab" * 32,
         "prev": "cd" * 32, "head": "ef" * 32, "t_us": True},
        *_chunk_span(0, kick["deadline_ms"], start=2_000),
    ]
    filled = _fill(tmp_path / "bool-chunk", rows, [kick])
    assert filled["challenges"][0]["result"] is None
    line = json.loads((tmp_path / "bool-chunk" / "challenge_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["reason"] == "pattern_mismatch" and line["withheld"] == "controller_log"


def test_non_string_status_state_withholds(tmp_path):
    """A status state that is not a string fails closed. An unknown string does too."""
    import qorcap_challenge as C

    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    span = _chunk_span(issued, deadline)
    head = _mismatch_head(kick)
    number = [*head, {"type": "status", "t_us": (issued + 500) * 1000, "state": 1, "detail": "1", "seg": 0}, *span]
    assert C.pad_connected_through(number, issued_ms=issued, deadline_ms=deadline, anchor_ms=0) is False
    stored, local = _boundary(tmp_path, "state-number", number)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"
    asleep = [*head, _status(issued + 500, "asleep"), *span]
    assert C.pad_connected_through(asleep, issued_ms=issued, deadline_ms=deadline, anchor_ms=0) is False
    stored, local = _boundary(tmp_path, "state-asleep", asleep)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_bool_status_time_withholds(tmp_path):
    """A bool status t_us is not an integer. True would otherwise be time 0 and stay connected."""
    import qorcap_challenge as C

    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    rows = [
        *_mismatch_head(kick),
        {"type": "status", "t_us": True, "state": "connected", "detail": "connected", "seg": 0},
        *_chunk_span(issued, deadline),
    ]
    assert C.pad_connected_through(rows, issued_ms=issued, deadline_ms=deadline, anchor_ms=0) is False
    stored, local = _boundary(tmp_path, "bool-status", rows)
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"
