"""Tests for the one-click launcher. Needs a qortime checkout: $QORTIME or ../qortime or ../../qortime."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
QT = next((Path(p) for p in [os.getenv("QORTIME", ""), HERE.parent / "qortime", HERE.parent.parent / "qortime"]
           if p and (Path(p) / "qor_receipt").exists()), None)
pytestmark = pytest.mark.skipif(QT is None, reason="no qortime checkout")
if QT:
    sys.path.insert(0, str(QT))
sys.path.insert(0, str(HERE))
import qorcap_match as qm  # noqa: E402


def test_old_bundle_opts_into_challenge_rule_v2(tmp_path):
    bundle = tmp_path / "match"
    bundle.mkdir()
    (bundle / "bundle.json").write_text(json.dumps({"schema": "qorcap-bundle/v0", "qorcap_version": "0.3.15", "role": "home"}) + "\n")
    qm.opt_in_challenge_rule(bundle)
    meta = json.loads((bundle / "bundle.json").read_text())
    assert meta["challenge_rule"] == 2 and meta["role"] == "home" and meta["qorcap_version"] == "0.3.15"
    qm.opt_in_challenge_rule(bundle)
    assert json.loads((bundle / "bundle.json").read_text())["challenge_rule"] == 2
    shutil.rmtree(bundle)
    qm.opt_in_challenge_rule(bundle)  # no bundle.json yet: nothing to write


def _draft(mid="x"):
    return {"schema": "qor-receipt/v0",
            "match": {"match_id": mid, "game": "madden27", "platform": "ps5", "scorebug_profile": "madden27_standard",
                      "mode": "single", "started_ms": 0, "ended_ms": 0},
            "result": {"home_score": 14, "away_score": 10, "quarter": 4, "final_t_ms": 5000, "winner": "home",
                       "method": "agreed_scorebug_final", "final_basis": "clock_zero"},
            "players": [{"role": "home", "player_id": "psn:me", "pubkey": "0" * 64, "key_kind": "software"}],
            "referee": {}, "challenges": [], "checkpoints": {},
            "agreement": {"timelines_agree": None, "mismatches": []}, "signals": {"motion_permille": {"home": None}}}


def _signed(tmp_path, n=5):
    from qor_receipt.checkpoints import Chain
    dev, ref = qm.load_keys(tmp_path / "keys")
    t = [1000]
    cs = qm.Countersigner(ref, "local-1", "home", clock=lambda: t[0] + 50)
    ch = Chain()
    for i in range(n):
        cp = ch.next(t[0], "a" * 64, "b" * 64)
        cp["qorcap"] = {"trigger": "segment"}  # QorCap extras are dropped from the receipt
        assert cs.offer(cp)
        t[0] += 2000
    assert not cs.offer({"seq": 99})
    rec = qm.build_receipt(_draft(), cs.signed, dev, ref, match_id="local-1", role="home", started_ms=900)
    return rec, ref


def test_signed_receipt_drops_fields_the_schema_rejects(tmp_path):
    """receipt.v0.json closes result and match. New data stays off the signed receipt."""
    schema = json.loads((HERE.parent / "contracts" / "receipt.v0.json").read_text())
    assert schema["additionalProperties"] is False
    assert schema["properties"]["result"]["additionalProperties"] is False
    assert schema["properties"]["match"]["additionalProperties"] is False
    draft = _draft()
    draft["result"]["delta_corroboration"] = {"kind": "haptic", "t_ms": 2}
    draft["result"]["glimpse"] = {"verdict": "agree"}
    draft["match"]["glimpse_note"] = "advisory"
    draft["stop_reason"] = "enter"
    rec, ref = _signed_from(tmp_path, draft)
    assert "delta_corroboration" not in rec["result"] and "glimpse" not in rec["result"]
    assert "glimpse_note" not in rec["match"] and "stop_reason" not in rec
    assert set(rec["result"]) <= set(schema["properties"]["result"]["properties"])
    assert set(rec["match"]) <= set(schema["properties"]["match"]["properties"])
    from qor_receipt.receipt import verify_receipt
    assert verify_receipt(rec, referee_pub=ref.pubkey_hex)["status"] == "SELF_ATTESTED"


def _signed_from(tmp_path, draft, n=2):
    from qor_receipt.checkpoints import Chain
    dev, ref = qm.load_keys(tmp_path / "keys")
    t = [1000]
    cs = qm.Countersigner(ref, "local-1", "home", clock=lambda: t[0] + 50)
    ch = Chain()
    for _ in range(n):
        cp = ch.next(t[0], "a" * 64, "b" * 64)
        cp["qorcap"] = {"trigger": "segment"}
        assert cs.offer(cp)
        t[0] += 2000
    rec = qm.build_receipt(draft, cs.signed, dev, ref, match_id="local-1", role="home", started_ms=900)
    return rec, ref


def test_signed_receipt_verifies_and_is_honest(tmp_path):
    from qor_receipt.receipt import verify_receipt
    rec, ref = _signed(tmp_path)
    v = verify_receipt(rec, referee_pub=ref.pubkey_hex)
    assert v["errors"] == [] and v["status"] == "SELF_ATTESTED"  # QorCap local referee is never allowlisted VERIFIED
    assert v["trust_tier"] == "single-attested"
    assert rec["referee"]["key_kind"] == qm.REFEREE_KIND
    assert rec["players"][0]["key_kind"] == "software-file"
    assert "qorcap" not in rec["checkpoints"]["home"][0]


def test_schema_valid(tmp_path):
    js = pytest.importorskip("jsonschema")
    rec, _ = _signed(tmp_path)
    js.validate(rec, json.loads((QT / "schema" / "receipt.v0.json").read_text()))


def test_tamper_fails(tmp_path):
    from qor_receipt.receipt import verify_receipt
    rec, ref = _signed(tmp_path)
    rec["result"]["home_score"] = 99
    assert verify_receipt(rec, referee_pub=ref.pubkey_hex)["status"] == "FAIL"
    rec2, ref2 = _signed(tmp_path)
    assert verify_receipt(rec2, referee_pub="1" * 64)["status"] == "FAIL"


def test_keys_persist_private_and_not_printed(tmp_path, capsys):
    a, _ = qm.load_keys(tmp_path / "k")
    b, _ = qm.load_keys(tmp_path / "k")
    assert a.pubkey_hex == b.pubkey_hex
    seed = (tmp_path / "k" / "device.key").read_text().strip()
    assert seed not in capsys.readouterr().out
    if os.name != "nt":
        assert (tmp_path / "k" / "device.key").stat().st_mode & 0o077 == 0


def test_status_line_and_overall():
    s = qm.status_line({"state": "capturing", "recording_secs": 75}, {"score": {"home": 7, "away": 3, "quarter": 2, "clock_s": 65}, "segments": 4, "clips": 1}, 4)
    assert "REC" in s and "AWAY 3 - HOME 7" in s and "Q2 1:05" in s and "clips 1" in s
    assert "NO SIGNAL" in qm.status_line({"state": "waiting_for_device"}, {}, 0)
    assert qm.overall("VERIFIED", False) == "FAIL" and qm.overall("PARTIAL", True) == "PARTIAL"


def _qorcap_bin():
    for p in [HERE.parent / "target" / "release" / "qorcap", HERE.parent / "target" / "debug" / "qorcap"]:
        if p.exists():
            return p
    return None


@pytest.mark.skipif(shutil.which("ffmpeg") is None or _qorcap_bin() is None, reason="needs ffmpeg + built qorcap")
def test_end_to_end_test_pattern(tmp_path):
    # 8 s of simulated input-proof work after the stop: the tail must still be countersigned within ~1 s.
    env = dict(os.environ, QORCAP_HOME=str(tmp_path / "home"), QORCAP_TEST_BUNDLE_DELAY_MS="8000")
    p = subprocess.run([sys.executable, str(HERE / "qorcap_match.py"), "--test-pattern", "--test-secs", "10",
                        "--qortime", str(QT), "--qorcap", str(_qorcap_bin()), "--out", str(tmp_path / "m"),
                        "--port", "8795", "--no-open"], capture_output=True, text=True, env=env, timeout=180,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "RESULT: SELF_ATTESTED" in p.stdout and "SELF-ATTESTED" in p.stdout and "final via clock_zero" in p.stdout
    b = next((tmp_path / "m").iterdir())
    rec = json.loads((b / "receipt.json").read_text())
    from qor_receipt.receipt import verify_receipt
    v = verify_receipt(rec, referee_pub=(b / "referee.pub").read_text().strip())
    assert v["errors"] == [] and len(rec["checkpoints"]["home"]) >= 4
    first = json.loads((b / "checkpoints.jsonl").read_text().splitlines()[0])
    assert first["qorcap"]["trigger"] == "start"  # genesis checkpoint before the first segment (0.3.2)
    signed = [json.loads(x) for x in (b / "countersigned.jsonl").read_text().splitlines()]
    assert all(c["referee_ts_ms"] - c["t_ms"] < 1500 for c in signed), [c["referee_ts_ms"] - c["t_ms"] for c in signed][-3:]
    cps = [json.loads(x) for x in (b / "checkpoints.jsonl").read_text().splitlines()]
    assert cps[-1]["qorcap"]["trigger"] == "stop" and len(signed) == len(cps)  # sealed tail, nothing after
    log = (b / "qorcap.log").read_text()
    assert log.index("final checkpoints countersigned through") < log.rindex("match bundle written")
    assert subprocess.run([str(_qorcap_bin()), "verify-bundle", str(b)]).returncode == 0
    assert list((b / "clips").glob("*.mp4"))


# ------------------------------------------------------------------ offline finalize
def _record_bundle(tmp_path, port, secs=9):
    import time
    import urllib.request
    b = tmp_path / "match-20261014-120000"
    q = str(_qorcap_bin())
    p = subprocess.Popen([q, "--port", str(port), "record", "--device", "testpattern", "--res", "640x360", "--fps", "30",
                          "--bundle", str(b), "--encoder", "libx264", "--audio", "none", "--match-id", "local-t"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(secs)
    subprocess.run([q, "--port", str(port), "stop"], capture_output=True)
    p.wait(timeout=60)
    assert (b / "video.mp4").exists() and (b / "receipt.draft.json").exists()
    return b


class _ScriptCore:
    """Stands in for ReaderCore + the real ScorebugReader: emits a scripted game keyed on video time."""
    def __init__(self, post):
        from qor_receipt.final_detect import detect_final
        sys.path.insert(0, str(HERE))
        from qorcap_reader import ReaderCore
        from types import SimpleNamespace as NS

        class R:
            nxt = None
            def read(self, img):
                return self.nxt

        class A:
            def offer(self, fr, **_):
                return NS(agreed=fr.ok, read=fr if fr.ok else None)
        self.r = R()
        self.core = ReaderCore(post, self.r, A(), None, detect_final)
        self.stats = self.core.stats
        self.t0 = None
        self.NS = NS

    @property
    def post(self):
        return self.core.post

    @post.setter
    def post(self, v):
        self.core.post = v

    def on_frame(self, t, img):
        assert img.shape == (720, 1280, 3)  # v0.3.5: full-resolution frames (Monday Night profile needs them)
        self.t0 = t if self.t0 is None else self.t0
        s = (t - self.t0) / 1000
        h, a, q, c = (0, 0, 1, 900) if s < 2 else (7, 0, 2, 300) if s < 4 else (14, 10, 4, 0)
        self.r.nxt = self.NS(ok=True, reason="ok", left_score=a, right_score=h, quarter=q, clock_seconds=c, clock_red=c == 0)
        self.core.on_frame(t, img)


needs_bin = pytest.mark.skipif(shutil.which("ffmpeg") is None or _qorcap_bin() is None, reason="needs ffmpeg + built qorcap")


@needs_bin
def test_finalize_offline_scripted_game(tmp_path):
    pytest.importorskip("numpy")
    from qor_receipt.receipt import verify_receipt
    b = _record_bundle(tmp_path, 8796)
    code = qm.finalize(b, qt=QT, qorcap=str(_qorcap_bin()), ffmpeg=shutil.which("ffmpeg"), key_dir=tmp_path / "keys",
                       core_factory=_ScriptCore)
    assert code == 0
    rec = json.loads((b / "receipt.json").read_text())
    assert rec["result"]["final_basis"] == "clock_zero" and (rec["result"]["home_score"], rec["result"]["away_score"]) == (14, 10)
    assert rec["referee"]["key_kind"] == qm.REFEREE_KIND_OFFLINE
    v = verify_receipt(rec, referee_pub=(b / "referee.pub").read_text().strip())
    assert v["errors"] == [], v
    scps = [json.loads(x) for x in (b / "score_checkpoints.jsonl").read_text().splitlines()]
    assert [c["qorcap"]["trigger"] for c in scps] == ["score_change", "score_change", "score_change", "final"]
    from qor_receipt.checkpoints import body_of
    from qor_receipt.jcs import sha256_hex
    heads = {json.loads(x).get("chain_head") for x in (b / "manifest.jsonl").read_text().splitlines()}
    prev = "0" * 64
    for c in scps:
        assert c["prev"] == prev and sha256_hex(body_of(c)) == c["hash"] and sha256_hex(c["qorcap"]) == c["frames_hash"]
        assert c["qorcap"]["chain_head"] in heads
        prev = c["hash"]
    fin = json.loads((b / "final.offline.json").read_text())
    assert fin["basis"] == "clock_zero"
    assert subprocess.run([str(_qorcap_bin()), "verify-bundle", str(b)]).returncode == 0


@needs_bin
def test_finalize_cli_real_reader_on_test_pattern(tmp_path):
    """`qorcap finalize <bundle>` end to end with QorTime's real reader: a test pattern has no scorebug."""
    pytest.importorskip("cv2")
    b = _record_bundle(tmp_path, 8797, secs=6)
    env = dict(os.environ, QORCAP_HOME=str(tmp_path / "home"), QORCAP_PYTHON=sys.executable)
    p = subprocess.run([str(_qorcap_bin()), "finalize", str(b), "--", "--qortime", str(QT), "--no-open"],
                       capture_output=True, text=True, env=env, timeout=300, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Finalize Match (offline)" in p.stdout and "final: not detected" in p.stdout
    assert "NOT a confirmed final" in p.stdout and "RESULT: SELF_ATTESTED" in p.stdout
    rec = json.loads((b / "receipt.json").read_text())
    assert "final_basis" not in rec["result"] and len(rec["checkpoints"]["home"]) >= 2


@needs_bin
def test_finalize_reuses_live_countersignatures(tmp_path):
    env = dict(os.environ, QORCAP_HOME=str(tmp_path / "home"))
    p = subprocess.run([sys.executable, str(HERE / "qorcap_match.py"), "--test-pattern", "--test-secs", "8",
                        "--qortime", str(QT), "--qorcap", str(_qorcap_bin()), "--out", str(tmp_path / "m"),
                        "--port", "8798", "--no-open"], capture_output=True, text=True, env=env, timeout=180,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    b = next((tmp_path / "m").iterdir())
    live = (b / "countersigned.jsonl").read_text().splitlines()
    assert live
    code = qm.finalize(b, qt=QT, qorcap=str(_qorcap_bin()), ffmpeg=shutil.which("ffmpeg"), key_dir=tmp_path / "home" / "keys",
                       core_factory=_ScriptCore)
    assert code == 0
    rec = json.loads((b / "receipt.json").read_text())
    assert rec["referee"]["key_kind"] == qm.REFEREE_KIND
    assert rec["checkpoints"]["home"][0]["referee_sig"] == json.loads(live[0])["referee_sig"]


def test_obs_offer_noop_off_windows(monkeypatch):
    monkeypatch.setattr(qm, "capture_apps_running", lambda: [])
    assert qm.offer_close_capture_apps(ask=lambda _: pytest.fail("should not ask"))
    calls = []
    seq = iter([["obs64.exe"], []])
    monkeypatch.setattr(qm, "capture_apps_running", lambda: next(seq, []))
    monkeypatch.setattr(qm.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    assert qm.offer_close_capture_apps(ask=lambda _: "")
    assert calls == [["taskkill", "/IM", "obs64.exe"]]


def test_read_jsonl_salvages_corrupt_line(tmp_path):
    p = tmp_path / "manifest.jsonl"
    p.write_text('{"type":"reopen"}\n{not json\n{"type":"segment","seq":0}\n', encoding="utf-8")
    rows = qm.read_jsonl(p)
    assert [r["type"] for r in rows] == ["reopen", "segment"]


def test_status_line_shows_warning_and_disk():
    s = qm.status_line(
        {"state": "capturing", "recording_secs": 10, "warning": "AUDIO FAILED — recording video-only", "disk_free_mb": 1500},
        {"score": {"home": 7, "away": 6, "quarter": 4, "clock_s": 0}, "segments": 2, "clips": 0}, 1)
    assert "REC" in s and "AWAY 6 - HOME 7" in s and "disk 1500MB" in s and "AUDIO FAILED" in s
    assert "DISK FULL" in qm.status_line({"state": "disk_full"}, {}, 0)


def test_shutdown_checkpoints_countersigned_while_qorcap_finishes(tmp_path):
    """Regression (live match 2026-10-16): QorCap's last checkpoints were countersigned only after the
    process exited (~28 s of input-proof work), giving clock_skew + gap_too_long on the tail."""
    import threading
    import time as _t
    from qor_receipt.checkpoints import Chain, verify_chain
    dev, ref = qm.load_keys(tmp_path / "keys")
    cs = qm.Countersigner(ref, "local-1", "home")
    path = tmp_path / "checkpoints.jsonl"
    ch = Chain()
    start = int(_t.time() * 1000)

    class Proc:  # QorCap: writes its final checkpoints right away, then keeps "analysing" for a while
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    proc = Proc()

    def qorcap():
        with open(path, "a", encoding="utf-8") as f:
            for _ in range(3):
                f.write(json.dumps(ch.next(int(_t.time() * 1000), "a" * 64, "b" * 64)) + "\n")
                f.flush()
        _t.sleep(1.5)  # input proof etc.
        proc.returncode = 0

    th = threading.Thread(target=qorcap)
    th.start()
    assert qm.wait_and_countersign(proc, path, cs, timeout=10, poll_s=0.05)
    th.join()
    assert len(cs.signed) == 3
    assert all(c["referee_ts_ms"] - c["t_ms"] < 1000 for c in cs.signed)
    end = cs.signed[-1]["referee_ts_ms"]
    assert verify_chain(cs.signed, match_id="local-1", role="home", referee_pub=ref.pubkey_hex,
                        start_ms=start, end_ms=end, max_skew_ms=1000) == []


def test_partial_checkpoint_line_is_not_signed(tmp_path):
    from qor_receipt.checkpoints import Chain
    dev, ref = qm.load_keys(tmp_path / "keys")
    cs = qm.Countersigner(ref, "local-1", "home")
    ch = Chain()
    p = tmp_path / "checkpoints.jsonl"
    p.write_text(json.dumps(ch.next(1, "a" * 64, "b" * 64)) + "\n" + '{"seq": 1, "t_ms"', encoding="utf-8")
    assert qm.countersign_new_from_file(p, cs) == 1 and len(cs.signed) == 1


def test_wait_and_countersign_reports_progress_via_api_sweep(tmp_path):
    from qor_receipt.checkpoints import Chain
    dev, ref = qm.load_keys(tmp_path / "keys")
    cs = qm.Countersigner(ref, "local-1", "home")
    ch = Chain()
    cps = [ch.next(1000 + i, "a" * 64, "b" * 64) for i in range(3)]
    reports, ticks = [], []

    class Proc:
        returncode = None
        n = 0
        def poll(self):
            self.n += 1
            if self.n > 5:
                self.returncode = 0
            return self.returncode
        def kill(self): self.returncode = -9

    def sweep():  # QorCap's API serves the sealed tail (file may lag/miss on Windows)
        for c in cps:
            cs.offer(c)
    assert qm.wait_and_countersign(Proc(), tmp_path / "missing.jsonl", cs, timeout=10, poll_s=0.01, sweep=sweep,
                                   report=reports.append, on_tick=lambda: ticks.append(1))
    assert len(cs.signed) == 3 and reports == [2] and ticks


def test_mic_privacy_check():
    reg = {}
    assert qm.mic_privacy_blocked(lambda k: reg.get(k)) is None
    reg[qm.MIC_KEY + r"\NonPackaged"] = "Deny"
    assert qm.mic_privacy_blocked(lambda k: reg.get(k)) == "desktop"
    assert any("Let desktop apps access your microphone" in l for l in qm.mic_hint("desktop"))
    reg[qm.MIC_KEY] = "Deny"
    assert qm.mic_privacy_blocked(lambda k: reg.get(k)) == "all"
    reg = {qm.MIC_KEY: "Allow", qm.MIC_KEY + r"\NonPackaged": "Allow"}
    assert qm.mic_privacy_blocked(lambda k: reg.get(k)) is None
    assert qm.mic_hint(None) == []


def test_tail_final_check_confirms_from_recorded_frames(tmp_path):
    import cv2
    from qor_receipt.final_detect import detect_final
    from qorcap_reader import ReaderCore, madden_final_screen

    class Paused:
        paused = True
        def update(self, t, img): pass

    b = tmp_path
    reads = [{"t_ms": 1000 + i * 1000, "home": 17, "away": 0, "quarter": 4, "clock_s": c, "clock_red": True}
             for i, c in enumerate([3, 2, 0])]
    (b / "timeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reads))
    (b / "manifest.jsonl").write_text(json.dumps({"type": "segment", "seq": 1, "file": "video.mp4", "wall_ms": 60_000}) + "\n")
    (b / "video.mp4").write_bytes(b"x")
    end = cv2.imread(str(HERE / "fixtures" / "m27_end_menu_restart.jpg"))
    frames = [(k * 500, end) for k in range(20)]
    draft = _draft()
    draft["result"] = {"home_score": 17, "away_score": 0, "quarter": 4, "final_t_ms": 60_000, "winner": "home",
                       "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}
    # v0.3.3 (scorebug only) took 0-17 from the last scorebug read; v0.3.4 reads the end menu's 0-20.
    fac0 = lambda post: ReaderCore(post, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen)  # noqa: E731
    r = qm.tail_final_check(b, draft, ffmpeg="ffmpeg-missing", core_factory=fac0, frames=frames)["result"]
    assert r["final_basis"] == "final_screen" and (r["away_score"], r["home_score"]) == (0, 17)
    assert r["method"] == "agreed_scorebug_final" and r["final_confirmed"] is False
    from qorcap_endmenu import EndMenuReader
    em = EndMenuReader()
    fac = lambda post: ReaderCore(post, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,  # noqa: E731
                                  end_menu_reader=em)
    out = qm.tail_final_check(b, draft, ffmpeg="ffmpeg-missing", core_factory=fac, frames=frames)
    r = out["result"]
    assert (r["away_score"], r["home_score"]) == (0, 20) and r["winner"] == "home"
    assert r["method"] == "end_menu_read" and r["final_basis"] == "end_menu" and r["final_confirmed"] is False
    assert r["scorebug_last"] == {"home_score": 17, "away_score": 0, "quarter": 4, "t_ms": 3000}
    assert r["notes"] and r["notes"][0].startswith("final play score not shown on scorebug")
    assert any("NOT corroborated" in n for n in r["notes"])
    # With haptic corroboration near last scorebug, match-2 style 0-17→0-20 confirms:
    draft2 = dict(draft); draft2["result"] = dict(draft["result"])
    def fac_h(post):
        c = ReaderCore(post, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,
                       end_menu_reader=em)
        c.delta_corroboration = [{"kind": "haptic", "t_ms": 3000}]
        return c
    r2 = qm.tail_final_check(b, draft2, ffmpeg="ffmpeg-missing", core_factory=fac_h, frames=frames)["result"]
    assert r2["final_confirmed"] is True
    tail = json.loads((b / "final.tail.json").read_text())
    assert tail["final"]["basis"] == "end_menu" and tail["end_menu"]["frames"] >= 3
    # no end screen in the tail -> draft unchanged
    (b / "final.tail.json").unlink()
    black = end * 0
    out2 = qm.tail_final_check(b, draft, ffmpeg="ffmpeg-missing", core_factory=fac, frames=[(k * 500, black) for k in range(20)])
    assert out2["result"].get("final_confirmed") is False


# ---- v0.3.6 audio level checks
def test_parse_volumedetect_silent_and_ok():
    import qorcap_match as Q
    silent = "[Parsed_volumedetect_0 @ 0] mean_volume: -90.3 dB\n[Parsed_volumedetect_0 @ 0] max_volume: -80.8 dB\n"
    ok = "mean_volume: -24.1 dB\nmax_volume: -1.2 dB\n"
    assert Q.parse_volumedetect(silent) == {"mean_db": -90.3, "max_db": -80.8, "silent": True}
    assert Q.parse_volumedetect(ok)["silent"] is False
    assert Q.parse_volumedetect("mean_volume: -inf dB\nmax_volume: -inf dB")["silent"] is True
    assert Q.parse_volumedetect("nothing") is None


def test_pick_card_audio_and_hint():
    import qorcap_match as Q
    devs = ["Headset Microphone (DualSense Edge Wireless Controller)", "Digital Audio Interface (USB3.0 Audio)",
            "Microphone Array (Intel Smart Sound)"]
    assert Q.pick_card_audio("USB3.0 Video", "auto", devs) == "Digital Audio Interface (USB3.0 Audio)"
    assert Q.pick_card_audio("USB3.0 Video", "My Card", devs) == "My Card"
    assert Q.pick_card_audio("Other Cam", "auto", devs) is None
    assert Q.audio_silence_hint({"silent": False, "max_db": -3, "mean_db": -20}, when="x") == []
    txt = "\n".join(Q.audio_silence_hint({"silent": True, "max_db": -80.8, "mean_db": -90.3}, when="now"))
    assert "SILENT" in txt and "Chat Audio" in txt and "unplug" not in txt.lower()
    assert "pure digital silence" not in txt
    txt = "\n".join(Q.audio_silence_hint({"silent": True, "max_db": -91.0, "mean_db": -91.0}, when="now"))
    assert "pure digital silence" in txt and "Linear PCM" in txt and txt.index("Linear PCM") < txt.index("Chat Audio")
    assert "pure digital silence" not in "\n".join(Q.audio_silence_hint({"silent": True, "no_audio_stream": True}, when="now"))


def test_recover_rebuilds_draft_from_disk_and_excludes_the_tail(tmp_path):
    b = tmp_path / "match-20261017-151655"
    b.mkdir()
    body = b"hashed-bytes"
    tail = b"TAILDATA"
    (b / "video.mp4").write_bytes(body + tail)
    seg = {"type": "segment", "seq": 0, "file": "video.mp4", "offset": 0, "bytes": len(body),
           "sha256": "ab", "prev": "0" * 64, "chain_head": "cd", "wall_ms": 1000, "device": "USB3.0 Video"}
    (b / "manifest.jsonl").write_text(json.dumps(seg) + "\n", encoding="utf-8")
    cp = {"seq": 0, "t_ms": 1500, "hash": "ee", "qorcap": {
        "trigger": "segment", "match_id": "m-151655", "role": "away",
        "score": {"home": 16, "away": 10, "quarter": 4}}}
    (b / "checkpoints.jsonl").write_text(json.dumps(cp) + "\n", encoding="utf-8")
    (b / "timeline.jsonl").write_text(json.dumps({"t_ms": 1400, "home": 16, "away": 10, "quarter": 4}) + "\n", encoding="utf-8")
    ch = {"id": "abcd1234", "role": "away", "phase": "kickoff", "nonce": "00112233445566778899aabbccddeeff",
          "pattern": ["OPTIONS", "CROSS"], "issued_ms": 10, "deadline_ms": 20, "result": {"ok": True, "reason": "x"}}
    (b / "challenges.jsonl").write_text(json.dumps(ch) + "\n", encoding="utf-8")
    (b / "inputs.jsonl").write_text(json.dumps({"type": "chunk", "seq": 0, "head": "hh", "n": 1}) + "\n", encoding="utf-8")
    (b / "qorcap.log").write_text(
        "[ERROR] recorder pipe broken (The pipe is being closed. (os error 232)); stopping recording\n"
        "recording finalised -> video.mp4\n", encoding="utf-8")
    first = qm.recover_bundle(b)
    second = qm.recover_bundle(b)
    assert first == second
    assert first["recovered"] is True and first["stop_reason"] == "pipe_broken"
    assert first["signals"]["recovered"] is True and first["signals"]["stop_reason"] == "pipe_broken"
    assert first["result"]["home_score"] == 16 and first["result"]["away_score"] == 10
    assert first["result"]["quarter"] == 4
    assert first["result"]["method"] == "last_scorebug_read_unconfirmed"
    assert first["result"]["final_confirmed"] is False
    assert "final_basis" not in first["result"]
    assert first["challenges"][0]["result"] is None
    assert first["players"][0]["player_id"] == "local:player"
    assert any("player_id was not on disk" in n for n in first["result"]["notes"])
    assert any("last timeline score is unconfirmed" in n for n in first["result"]["notes"])
    meta = json.loads((b / "bundle.json").read_text(encoding="utf-8"))
    assert meta["recovered"] is True and meta["stop_reason"] == "pipe_broken"
    assert meta["match_id"] == "m-151655" and meta["role"] == "away" and meta["device"] == "USB3.0 Video"
    assert meta["segments"] == 1 and meta["checkpoints"] == 1
    assert meta["excluded_tails"][0]["covered_bytes"] == len(body)
    assert meta["excluded_tails"][0]["tail_bytes"] == len(tail)
    assert meta["excluded_tails"][0]["tail_sha256"]
    assert meta["inputs_on_disk"]["chunks"] == 1
    assert meta["final"] is None


def test_recover_without_a_log_does_not_invent_a_cause_or_a_score(tmp_path):
    b = tmp_path / "match-empty"
    b.mkdir()
    (b / "video.mp4").write_bytes(b"xyz")
    seg = {"type": "segment", "seq": 0, "file": "video.mp4", "offset": 0, "bytes": 3, "chain_head": "aa", "wall_ms": 5}
    (b / "manifest.jsonl").write_text(json.dumps(seg) + "\n", encoding="utf-8")
    (b / "checkpoints.jsonl").write_text(
        json.dumps({"seq": 0, "t_ms": 5, "hash": "h", "qorcap": {"match_id": "m", "role": "home"}}) + "\n",
        encoding="utf-8")
    draft = qm.recover_bundle(b)
    assert draft["stop_reason"] == "not_recorded"
    assert draft["result"]["home_score"] is None and draft["result"]["away_score"] is None
    assert draft["result"]["winner"] == "unknown"
    assert draft["challenges"] == []
    meta = json.loads((b / "bundle.json").read_text(encoding="utf-8"))
    assert "excluded_tails" not in meta
    assert qm.recover_bundle(tmp_path / "does-not-exist") is None


def test_finalize_recover_needs_evidence_and_plain_finalize_still_requires_a_draft(tmp_path, capsys):
    b = tmp_path / "match-bare"
    b.mkdir()
    (b / "video.mp4").write_bytes(b"x")
    assert qm.finalize(b, qt=tmp_path, qorcap="qorcap", ffmpeg="ffmpeg", key_dir=tmp_path, no_open=True) == 2
    assert "receipt.draft.json" in capsys.readouterr().out
    assert qm.finalize(b, qt=tmp_path, qorcap="qorcap", ffmpeg="ffmpeg", key_dir=tmp_path, no_open=True, recover=True) == 2
    assert not (b / "receipt.draft.json").exists()


def test_spawn_flags_detach_and_enter_still_posts_shutdown(monkeypatch):
    monkeypatch.setattr(qm, "WIN", False)
    assert qm.qorcap_spawn_flags() == 0
    monkeypatch.setattr(qm, "WIN", True)
    flags = qm.qorcap_spawn_flags()
    assert flags & qm.DETACHED_PROCESS
    assert flags & qm.CREATE_BREAKAWAY_FROM_JOB
    assert flags & 0x200
    calls = []

    class Proc:
        pid = 1

    def popen(cmd, **kw):
        calls.append(kw.get("creationflags"))
        if kw.get("creationflags", 0) & qm.CREATE_BREAKAWAY_FROM_JOB:
            raise OSError("job refused breakaway")
        return Proc()

    monkeypatch.setattr(qm.subprocess, "Popen", popen)
    proc = qm.spawn_qorcap(["qorcap"], stdout=None)
    assert proc.pid == 1
    assert calls[0] & qm.CREATE_BREAKAWAY_FROM_JOB
    assert calls[1] & qm.DETACHED_PROCESS
    assert not (calls[1] & qm.CREATE_BREAKAWAY_FROM_JOB)
    assert qm.shutdown_body("enter") == {"reason": "enter"}
    assert qm.shutdown_body("ctrl_c") == {"reason": "ctrl_c"}
    assert qm.shutdown_body("final") == {"reason": "final"}


def test_stop_reason_from_log_is_the_earliest_recorded_cause():
    text = "hello\n[ERROR] recorder pipe broken (os error 232)\nCtrl+C: stopping\n"
    assert qm.stop_reason_from_log(text) == "pipe_broken"
    assert qm.stop_reason_from_log("Ctrl+C: stopping\n") == "ctrl_c"
    assert qm.stop_reason_from_log("") == "not_recorded"
    assert qm.stop_reason_from_log(None) == "not_recorded"


def test_recovery_notes_survive_the_score_read_and_a_normal_draft_does_not_gain_them():
    recovered = {"recovered": True, "result": {"notes": ["recording stopped: pipe_broken", "recovered from on-disk evidence; unhashed video bytes are excluded"]}}
    replaced = {"method": "last_scorebug_read_unconfirmed", "notes": ["end-of-game menu score not read: scorebug final only, NOT confirmed"]}
    out = qm.keep_recovery_notes(replaced, recovered)
    assert out["notes"][0].startswith("end-of-game menu")
    assert any(n.startswith("recording stopped: pipe_broken") for n in out["notes"])
    plain = {"result": {"notes": ["recording stopped: enter"]}}
    assert qm.keep_recovery_notes(replaced, plain) is replaced


def test_hashed_decode_limit_leaves_a_matching_file_whole(tmp_path):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"abc")
    assert qm.hashed_decode_limit(p, 3) is None
    assert qm.hashed_decode_limit(p, 2) == 2
    assert qm.hashed_decode_limit(p, None) is None


def test_post_game_reset_after_the_scorebug_leaves_keeps_the_legal_score():
    """match-20261017-162055: 6-21, scorebug gone 93s, then a fresh 0-0 Q1 4:00 became the receipt."""
    events = [
        {"type": "score", "t_ms": 1, "away": 0, "home": 0, "quarter": 1, "clock_s": 247},
        {"type": "score", "t_ms": 2, "away": 0, "home": 14, "quarter": 1, "clock_s": 2},
        {"type": "score", "t_ms": 3, "away": 6, "home": 14, "quarter": None, "clock_s": None},
        {"type": "score", "t_ms": 4, "away": 6, "home": 21, "quarter": None, "clock_s": None},
        {"type": "obs", "t_ms": 5_000, "until_ms": 98_000, "obs": "scorebug_absent", "n": 180},
        {"type": "score", "t_ms": 99_000, "away": 0, "home": 0, "quarter": 1, "clock_s": 240},
    ]
    kept = qm.game_segment_score(events)
    assert (kept["away"], kept["home"], kept["quarter"]) == (6, 21, 1)
    assert "post-game score reset" in kept["notes"][0]
    res = qm.apply_final({"home_score": 0, "away_score": 0, "quarter": 1, "winner": "tie"}, None, fallback=kept)
    assert (res["away_score"], res["home_score"], res["winner"], res["final_confirmed"]) == (6, 21, "home", False)
    assert any("post-game score reset" in n for n in res["notes"])
    # A timeout, then a real touchdown, is still this game.
    play = events[:5] + [{"type": "score", "t_ms": 99_000, "away": 6, "home": 28, "quarter": 2, "clock_s": 400}]
    nxt = qm.game_segment_score(play)
    assert (nxt["away"], nxt["home"], nxt["quarter"]) == (6, 28, 2)
    assert "notes" not in nxt
    # A short flicker is not a session break.
    flicker = events[:4] + [
        {"type": "obs", "t_ms": 10, "until_ms": 12_000, "obs": "scorebug_absent", "n": 4},
        {"type": "score", "t_ms": 13_000, "away": 0, "home": 0, "quarter": 1, "clock_s": 240},
    ]
    assert qm.game_segment_score(flicker)["home"] == 0


def test_post_game_reset_keeps_a_third_quarter_when_one_was_read():
    """162055 never reaches the 4th quarter. A 3RD read before the reset is the receipt quarter."""
    events = [
        {"type": "score", "t_ms": 1, "away": 0, "home": 0, "quarter": 1, "clock_s": 247},
        {"type": "score", "t_ms": 2, "away": 6, "home": 21, "quarter": 2, "clock_s": 20},
        {"type": "score", "t_ms": 3, "away": 6, "home": 21, "quarter": 3, "clock_s": 237},
        {"type": "obs", "t_ms": 5_000, "until_ms": 98_000, "obs": "scorebug_absent", "n": 180},
        {"type": "score", "t_ms": 99_000, "away": 0, "home": 0, "quarter": 1, "clock_s": 240},
    ]
    kept = qm.game_segment_score(events)
    assert (kept["away"], kept["home"], kept["quarter"]) == (6, 21, 3)
    assert "post-game score reset" in kept["notes"][0]


def test_observed_handles_go_into_the_signed_receipt(tmp_path):
    """psn_names.json bound to the bundle -> receipt.match.observed_handles, covered by both signatures."""
    import qorcap_psn as P
    from qor_receipt.receipt import verify_receipt
    from qor_receipt.checkpoints import Chain
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "bundle.json").write_text(json.dumps({"chain_head": "cd" * 32}))
    (bundle / "manifest.jsonl").write_text("{}\n")
    side = {"handle": "Qortrola30", "pattern": "Qortrola30", "confidence_permille": 990, "sightings": 60, "agree": 60}
    doc = {"schema": P.SCHEMA, "reader": P.READER, "pack": "t", "bound_to": P.binding(bundle),
           "sides": {"left": dict(side, team="away"), "right": dict(side, handle=None, pattern="Every?hing_360", team="home")}}
    (bundle / "psn_names.json").write_text(json.dumps(doc))
    draft = qm.with_observed_handles(bundle, _draft())
    oh = draft["match"]["observed_handles"]
    assert oh["away"]["handle"] == "Qortrola30" and oh["home"]["handle"] is None and oh["source"] == "on_screen"
    dev, ref = qm.load_keys(tmp_path / "keys")
    cs = qm.Countersigner(ref, "local-1", "home", clock=lambda: 1050)
    cp = Chain().next(1000, "a" * 64, "b" * 64)
    assert cs.offer(cp)
    rec = qm.build_receipt(draft, cs.signed, dev, ref, match_id="local-1", role="home", started_ms=900)
    assert rec["match"]["observed_handles"] == oh
    v = verify_receipt(rec, referee_pub=ref.pubkey_hex)
    assert v["errors"] == [] and v["status"] == "SELF_ATTESTED"
    rec["match"]["observed_handles"]["away"]["handle"] = "Someone_else"  # edited after signing -> signature fails
    assert verify_receipt(rec, referee_pub=ref.pubkey_hex)["status"] == "FAIL"
    (bundle / "manifest.jsonl").write_text('{"other": 1}\n')  # file from another recording: ignored
    assert "observed_handles" not in qm.with_observed_handles(bundle, _draft())["match"]


@needs_bin
def test_finalize_role_correction_resigns_for_the_corrected_side(tmp_path, capsys):
    """QorCap was left on home but the player was away: finalize --role away records the correction in bundle.json,
    re-signs the checkpoints offline for away (the live countersignatures are bound to home) and never edits the
    recorded evidence. That bundle is not submittable and is not offered for upload."""
    pytest.importorskip("numpy")
    from qor_receipt.receipt import verify_receipt
    env = dict(os.environ, QORCAP_HOME=str(tmp_path / "home"))
    p = subprocess.run([sys.executable, str(HERE / "qorcap_match.py"), "--test-pattern", "--test-secs", "8",
                        "--qortime", str(QT), "--qorcap", str(_qorcap_bin()), "--out", str(tmp_path / "m"),
                        "--port", "8803", "--no-open"], capture_output=True, text=True, env=env, timeout=180,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    b = next((tmp_path / "m").iterdir())
    evidence = {f: (b / f).read_bytes() for f in ("checkpoints.jsonl", "countersigned.jsonl", "manifest.jsonl")}
    kw = dict(qt=QT, qorcap=str(_qorcap_bin()), ffmpeg=shutil.which("ffmpeg"), key_dir=tmp_path / "home" / "keys",
              core_factory=_ScriptCore)
    assert qm.finalize(b, role="away", role_reason="on-screen banners show the recorder owner on the away side", **kw) == 0
    out = capsys.readouterr().out
    meta = json.loads((b / "bundle.json").read_text())
    assert meta["submittable"] is False
    assert "NOT SUBMITTABLE" in out
    assert "Submit for Verification" not in out and "Submit now?" not in out
    assert qm.offer_submit(b, None, ask=lambda _p: pytest.fail("submit offered")) is None
    offer = capsys.readouterr().out
    assert "NOT SUBMITTABLE" in offer and "Submit for Verification" not in offer and "Submit now?" not in offer
    assert meta["role"] == "away" and meta["role_correction"]["from"] == "home" and meta["role_correction"]["to"] == "away"
    assert "away side" in meta["role_correction"]["reason"]
    rec = json.loads((b / "receipt.json").read_text())
    assert [x["role"] for x in rec["players"]] == ["away"] and list(rec["checkpoints"]) == ["away"]
    assert rec["referee"]["key_kind"] == qm.REFEREE_KIND_OFFLINE  # live sigs were for home
    assert any("side corrected after recording" in n for n in rec["result"]["notes"])
    assert (rec["result"]["away_score"], rec["result"]["home_score"]) == (10, 14)
    v = verify_receipt(rec, referee_pub=(b / "referee.pub").read_text().strip())
    assert v["errors"] == [], v
    assert {f: (b / f).read_bytes() for f in evidence} == evidence
    when = meta["role_correction"]["corrected_ms"]
    assert qm.finalize(b, **kw) == 0  # a later plain finalize keeps the corrected side
    meta2 = json.loads((b / "bundle.json").read_text())
    assert meta2["role"] == "away" and meta2["role_correction"]["corrected_ms"] == when
    assert meta2["submittable"] is False
    assert list(json.loads((b / "receipt.json").read_text())["checkpoints"]) == ["away"]
    assert qm.finalize(b, role="home", **kw) == 0  # back to the recorded side: record removed, live sigs reused
    meta3 = json.loads((b / "bundle.json").read_text())
    assert meta3["role"] == "home" and "role_correction" not in meta3
    assert meta3.get("submittable") is not False and "submittable" not in meta3
    assert json.loads((b / "receipt.json").read_text())["referee"]["key_kind"] == qm.REFEREE_KIND


def test_reader_timing_log_names_the_three_clocks(tmp_path):
    row = qm.reader_timing_row(1_000, [250, 300, 280], 12.4, [8, 9], [20, 22],
                               {"reader_lag": {"obs": "reader_lag", "n": 1, "max_gap_ms": 2100}})
    assert row["kind"] == "timing"
    assert row["frame_period_ms"]["n"] == 3 and row["frame_period_ms"]["max_ms"] == 300
    assert row["refresh_ms"] == 12
    assert row["on_frame_ms"]["mean_ms"] == 8 or row["on_frame_ms"]["mean_ms"] == 9
    text = qm.format_reader_timing(row)
    assert "frame_period_ms" in text and "refresh_ms" in text and "on_frame_ms" in text
    path = tmp_path / "reader_health.jsonl"
    qm.append_reader_health(path, row)
    got = json.loads(path.read_text(encoding="utf-8").strip())
    assert got["on_frame_ms"]["max_ms"] == 9
    assert "home" not in got and "away" not in got


def test_refresh_off_the_frame_thread_keeps_four_hertz(monkeypatch):
    """A 1.5 s refresh must not run on the frame thread.

    Sleep releases the GIL (at least 16 frames in 6 s). A CPU burn holds the GIL and
    still has to stay at or above 3 Hz.
    """
    import urllib.request

    import cv2
    import numpy as np

    import qorcap_reader

    counts = []

    def fast(self, t, img):
        counts.append(t)

    monkeypatch.setattr(qorcap_reader.ReaderCore, "on_frame", fast)
    frames = []
    for i in range(40):
        # Lossless and unique. JPEG quantization collapsed some of these 8x8 frames
        # onto the same bytes, and the loop then skipped them for up to a second.
        img = np.zeros((8, 8, 3), np.uint8)
        img[:, :, 0] = i
        ok, buf = cv2.imencode(".png", img)
        assert ok
        frames.append(bytes(buf))
    seq = {"i": 0}

    class _Resp:
        def __init__(self, data):
            self._d = data

        def read(self):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout=3):
        data = frames[seq["i"] % len(frames)]
        seq["i"] += 1
        return _Resp(data)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def drive(mode, floor):
        def slow_get(self, path, timeout=3):
            # Sleep releases the GIL. The CPU burn holds it for 1.5 s, which is what a
            # refresh stuck back on the frame thread would do. Off-thread, the frame
            # loop still has to clear 3 Hz.
            if mode == "sleep":
                time.sleep(1.5)
            else:
                deadline = time.perf_counter() + 1.5
                x = 0
                while time.perf_counter() < deadline:
                    x = (x * 1103515245 + 12345) & 0x7FFFFFFF
            return {}

        monkeypatch.setattr(qm.Api, "get", slow_get)
        stop = threading.Event()
        state = {}
        before = len(counts)
        thread = qm.start_reader(qm.Api(8791), stop, state, bundle=None)
        time.sleep(0.3)
        n0 = len(counts)
        time.sleep(6.0)
        n1 = len(counts)
        stop.set()
        thread.join(timeout=5)
        assert n1 - n0 >= floor, (mode, n1 - n0, n0 - before)

    drive("sleep", 16)
    drive("cpu", 18)  # 3 Hz over 6 s; the burn holds the GIL


def _full_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_bytes().decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _full_corr(bundle: Path) -> list:
    from qorcap_reader import collect_delta_corroboration

    clips = []
    meta = bundle / "bundle.json"
    if meta.is_file():
        clips.extend(json.loads(meta.read_text(encoding="utf-8")).get("clips") or [])
    for row in _full_jsonl(bundle / "manifest.jsonl"):
        if isinstance(row, dict) and row.get("type") == "clip":
            clips.append(row)
    return collect_delta_corroboration(
        clips=clips,
        haptic_records=[r for r in _full_jsonl(bundle / "haptics.jsonl") if isinstance(r, dict)],
        input_records=[r for r in _full_jsonl(bundle / "inputs.jsonl") if isinstance(r, dict)],
    )


def test_incremental_tail_matches_full_parse(tmp_path):
    """A generated inputs file, appended in chunks with a partial last line, matches a full parse."""
    from qorcap_reader import load_delta_corroboration_from_bundle, note_options_from_inputs, options_presses_ms

    bundle = tmp_path / "match"
    bundle.mkdir()
    (bundle / "bundle.json").write_text(json.dumps({"schema": "qorcap-bundle/v0", "clips": []}) + "\n", encoding="utf-8")
    (bundle / "manifest.jsonl").write_text(
        json.dumps({"type": "clip", "name": "score-a0-h3", "sha256": "ab" * 32, "t_ms": 5_000}) + "\n",
        encoding="utf-8")
    (bundle / "haptics.jsonl").write_text(json.dumps({"type": "haptic", "t_ms": 5_100}) + "\n", encoding="utf-8")
    inputs = bundle / "inputs.jsonl"
    parts = []
    size = 0
    i = 0
    while size < 15_000_000:
        button = 512 if i % 10 == 0 else 0
        line = f'{{"type":"in","b":{button},"t_us":{i * 1000}}}\n'.encode()
        parts.append(line)
        size += len(line)
        i += 1
    blob = b"".join(parts)

    class _Opt:
        def __init__(self):
            self._inputs_tail = None
            self.presses = []

        def set_options(self, presses):
            self.presses = list(presses)

    opt = _Opt()

    def check():
        note_options_from_inputs(opt, inputs)
        assert opt.presses == options_presses_ms(_full_jsonl(inputs))
        assert load_delta_corroboration_from_bundle(bundle) == _full_corr(bundle)

    for off in range(0, len(blob), 3_000_000):
        with open(inputs, "ab") as fh:
            fh.write(blob[off:off + 3_000_000])
        check()
    with open(inputs, "ab") as fh:
        fh.write(b'{"type":"in","b":512,"t_us":')  # partial trailing line, not valid JSON yet
    check()
    with open(inputs, "ab") as fh:
        fh.write(b"999}\n")
    check()
    with open(bundle / "haptics.jsonl", "ab") as fh:
        fh.write(json.dumps({"type": "haptic", "t_ms": 9_000}).encode() + b"\n")
    with open(bundle / "manifest.jsonl", "ab") as fh:
        fh.write(json.dumps({"type": "clip", "name": "score-a3-h3", "sha256": "cd" * 32, "t_ms": 9_100}).encode() + b"\n")
    check()
    rotated = bundle / "inputs-next.jsonl"
    rotated.write_bytes(b'{"type":"in","b":0,"t_ms":1}\n{"type":"in","b":512,"t_ms":2}\n')
    os.replace(rotated, inputs)
    check()
    inputs.write_bytes(b'{"type":"in","b":512,"t_us":4000}\n{"type":"in","b":')  # truncated, partial again
    check()


def test_tail_reparse_when_the_window_before_the_offset_changes(tmp_path):
    """A rewrite that keeps the 64-byte prefix and grows past the old offset is parsed again.

    The shrink is not observed: write_bytes replaces the bytes in place, and the next read
    sees a larger file. The window just before the old offset is what shows the body changed.
    """
    from qorcap_reader import load_delta_corroboration_from_bundle, note_options_from_inputs, options_presses_ms

    bundle = tmp_path / "m"
    bundle.mkdir()
    (bundle / "bundle.json").write_text("{}\n", encoding="utf-8")
    (bundle / "manifest.jsonl").write_bytes(b"")
    (bundle / "haptics.jsonl").write_bytes(b"")
    inputs = bundle / "inputs.jsonl"
    blob = b'{"type":"in","b":0,"t_us":1}\n' * 20
    inputs.write_bytes(blob)

    class _Opt:
        presses: list = []

        def set_options(self, presses):
            self.presses = list(presses)

    opt = _Opt()

    def check():
        note_options_from_inputs(opt, inputs)
        assert opt.presses == options_presses_ms(_full_jsonl(inputs))
        assert load_delta_corroboration_from_bundle(bundle) == _full_corr(bundle)

    check()
    new = blob[:64] + (b'{"type":"in","b":512,"t_ms":10}\n' * 40)
    assert len(new) > len(blob) and new[:64] == blob[:64]
    assert new[len(blob) - 64:len(blob)] != blob[-64:]
    inputs.write_bytes(new)
    check()
    with open(inputs, "ab") as fh:
        fh.write(b'{"type":"in","b":512,"t_ms":77}')  # valid JSON, no newline yet
    check()


class _Clock:
    """Stand-in for the refresh wait. ``advance`` moves its time; wall time does not."""

    def __init__(self):
        self._cv = threading.Condition()
        self.t = 0.0
        self.waiting = threading.Event()

    def __call__(self, ev, seconds):
        with self._cv:
            deadline = self.t + float(seconds)
            self.waiting.set()
            while self.t < deadline and not ev.is_set():
                self._cv.wait(timeout=1.0)
            return bool(ev.is_set())

    def advance(self, dt):
        with self._cv:
            self.t += float(dt)
            self._cv.notify_all()


class _Body:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _still_jpeg() -> bytes:
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    assert ok
    return bytes(buf)


def _arm_enter(monkeypatch):
    """Make the next ENTER/Q visible to key_watcher, on Windows or POSIX."""
    ready = threading.Event()
    if qm.WIN:
        import msvcrt
        monkeypatch.setattr(msvcrt, "kbhit", lambda: ready.is_set())
        monkeypatch.setattr(msvcrt, "getwch", lambda: "\r")
    else:
        import select
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "readline", lambda: "\n")

        def fake_select(rlist, wlist, xlist, timeout=None):
            if ready.wait(0.2 if timeout is None else timeout):
                return list(rlist)
            return []

        monkeypatch.setattr(select, "select", fake_select)
    return ready


def test_options_press_is_known_before_end_menu_can_lock(tmp_path, monkeypatch):
    """An OPTIONS press appended just after a refresh is visible before an end menu can lock.

    EndMenuAgreement commits on 3 frames. At 4 Hz a menu that appears just after a frame
    is locked by the third following frame, 3 periods later (0.75 s). A 2 s refresh
    period misses that. 0.5 s does not. The refresh wait uses an injected clock.
    """
    import urllib.request

    import qorcap_reader
    from qorcap_endmenu import EndMenuAgreement

    agree = EndMenuAgreement()
    lock_s = agree.n * qm.FRAME_PERIOD_S
    assert lock_s * 1000 >= agree.min_span_ms
    assert qm.REFRESH_PERIOD_S < lock_s, qm.REFRESH_PERIOD_S

    monkeypatch.setattr(qorcap_reader.ReaderCore, "on_frame", lambda *a, **k: None)
    jpeg = _still_jpeg()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=3: _Body(jpeg))
    monkeypatch.setattr(qm.Api, "get", lambda self, path, timeout=3: {})

    bundle = tmp_path / "m"
    bundle.mkdir()
    inputs = bundle / "inputs.jsonl"
    inputs.write_text('{"type":"header","wall_anchor_ms":1000}\n', encoding="utf-8")
    stop = threading.Event()
    state = {}
    clock = _Clock()
    thread = qm.start_reader(qm.Api(8792), stop, state, bundle=bundle, clock=clock)
    assert clock.waiting.wait(2)
    # The synchronous refresh has already consumed the header. The worker is in its wait.
    with open(inputs, "ab") as fh:
        fh.write(b'{"type":"in","b":512,"t_ms":2500}\n')
    clock.advance(lock_s)
    deadline = time.monotonic() + 2
    seen = False
    while time.monotonic() < deadline:
        if 2500 in state["core"]._options_snapshot():
            seen = True
            break
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert seen, qm.REFRESH_PERIOD_S
    alive = [th.name for th in threading.enumerate() if th.name == "qorcap-refresh" and th.is_alive()]
    assert alive == []


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_reader_crash_leaves_enter_and_the_heartbeat_running(monkeypatch):
    """A raise in the frame loop must not set the match stop.

    ENTER still records user_stop, and the official live heartbeat keeps stepping.
    """
    import urllib.request

    import cv2

    import qorcap_reader
    from qorcap_submit import start_official_live

    monkeypatch.setattr(qorcap_reader.ReaderCore, "on_frame", lambda *a, **k: None)
    jpeg = _still_jpeg()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=3: _Body(jpeg))
    monkeypatch.setattr(qm.Api, "get", lambda self, path, timeout=3: {})

    def _boom(*_a, **_k):
        raise RuntimeError("forced")

    monkeypatch.setattr(cv2, "imdecode", _boom)
    ready = _arm_enter(monkeypatch)

    class _Live:
        def __init__(self):
            self.failures = 0
            self.steps = 0

        def step(self):
            self.steps += 1

    stop = threading.Event()
    state = {}
    live = _Live()
    thread = qm.start_reader(qm.Api(8793), stop, state, bundle=None)
    qm.key_watcher(stop, state)
    live_thread = start_official_live(live, stop, interval=0.05)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert not stop.is_set()
    base = live.steps
    grew = time.monotonic() + 1
    while time.monotonic() < grew and live.steps <= base:
        time.sleep(0.02)
    assert live.steps > base
    ready.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not state.get("user_stop"):
        time.sleep(0.02)
    assert state.get("user_stop") is True
    assert not stop.is_set()
    base = live.steps
    grew = time.monotonic() + 1
    while time.monotonic() < grew and live.steps <= base:
        time.sleep(0.02)
    assert live.steps > base
    stop.set()
    live_thread.join(timeout=2)
    assert not live_thread.is_alive()


def test_refresh_thread_is_joined_before_the_reader_returns(monkeypatch):
    """Shutdown waits on the refresh thread. Dropping the join lets the reader return early.

    A bare 'not alive after stop' check misses that: the refresh wait wakes on its own
    stop and the daemon exits. This holds the second refresh and requires the reader
    to still be in join.
    """
    import urllib.request

    import qorcap_reader

    monkeypatch.setattr(qorcap_reader.ReaderCore, "on_frame", lambda *a, **k: None)
    jpeg = _still_jpeg()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=3: _Body(jpeg))
    entered = threading.Event()
    hold = threading.Event()
    calls = {"n": 0}

    def get(self, path, timeout=3):
        calls["n"] += 1
        if calls["n"] >= 2:
            entered.set()
            hold.wait()
        return {}

    monkeypatch.setattr(qm.Api, "get", get)
    stop = threading.Event()
    state = {}
    thread = qm.start_reader(qm.Api(8794), stop, state, bundle=None)
    try:
        assert entered.wait(2.0), calls["n"]
        stop.set()
        time.sleep(0.6)
        assert thread.is_alive()
        assert any(th.name == "qorcap-refresh" and th.is_alive() for th in threading.enumerate())
    finally:
        hold.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(
        th.name == "qorcap-refresh" and th.is_alive() for th in threading.enumerate()
    ):
        time.sleep(0.02)
    assert not any(th.name == "qorcap-refresh" and th.is_alive() for th in threading.enumerate())
