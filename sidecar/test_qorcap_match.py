"""Tests for the one-click launcher. Needs a qortime checkout: $QORTIME or ../qortime or ../../qortime."""
import json
import os
import shutil
import subprocess
import sys
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
    assert "RESULT: PARTIAL" in p.stdout and "SELF-ATTESTED" in p.stdout and "final via clock_zero" in p.stdout
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
    assert "NOT a confirmed final" in p.stdout and "RESULT: PARTIAL" in p.stdout
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
