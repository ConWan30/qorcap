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
    assert 'return {"result": receipt_result(res), "local": local}' in chal
    assert 'return {"result": res if res["ok"] else None, "local": local}' not in chal


def _challenge(nonce: str, phase: str, issued: int) -> dict:
    from qor_receipt.challenge import pattern_for

    return {"id": nonce[:8], "role": "home", "phase": phase, "nonce": nonce,
            "pattern": pattern_for(nonce), "issued_ms": issued, "deadline_ms": issued + 90_000, "result": None}


def _mismatch_bundle(bundle: Path) -> dict:
    """Kickoff presses OPTIONS then a button that is not the prompt. Halftime and end have no presses."""
    from qor_receipt.review.bundle import BUTTON_BITS

    import qorcap_match as qm

    bits = {name: bit for name, bit in BUTTON_BITS}
    kick = _challenge("00112233445566778899aabbccddeeff", "kickoff", 1_000_000)
    wrong = next(b for b in ("CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT")
                 if b != kick["pattern"][1])
    t0 = kick["issued_ms"] + 1_000
    rows = [
        {"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0, "seg": 0, "wall_anchor_ms": 0},
        {"type": "in", "t_us": t0 * 1000, "b": 1 << bits["OPTIONS"], "seg": 0},
        {"type": "in", "t_us": (t0 + 400) * 1000, "b": 1 << bits[wrong], "seg": 0},
    ]
    bundle.mkdir(parents=True)
    (bundle / "inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    for name in ("bundle.json", "receipt.json"):
        (bundle / name).write_text("{}\n", encoding="utf-8")
    for name in ("manifest.jsonl", "checkpoints.jsonl"):
        (bundle / name).write_text("", encoding="utf-8")
    chs = [
        kick,
        _challenge("ffeeddccbbaa99887766554433221100", "halftime", 2_000_000),
        _challenge("0123456789abcdef0123456789abcdef", "end", 3_000_000),
    ]
    # decode=False: a mismatch still names pattern_mismatch. The pause search is not what failed.
    return qm.fill_challenge_results(bundle, {"challenges": chs}, decode=False)


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

    schema = json.loads((HERE.parent / "contracts" / "receipt.v0.json").read_text())
    result_schema = schema["properties"]["challenges"]["items"]["properties"]["result"]["oneOf"]
    assert result_schema[0] == {"type": "null"}
    props = result_schema[1]["properties"]
    assert props["ok"]["type"] == "boolean" and props["reason"]["type"] == "string"
    assert "enum" not in props["reason"] and "enum" not in props["ok"]
    assert set(kick) <= set(props) | {"reverified_by"}

    _rec, v = _signed_from_challenges(tmp_path / "sign", filled["challenges"])
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
