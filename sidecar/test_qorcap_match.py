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
    assert v["errors"] == [] and v["status"] == "PARTIAL"   # no referee challenges / motion: honestly partial
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
    env = dict(os.environ, QORCAP_HOME=str(tmp_path / "home"))
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
        assert img.shape == (360, 640, 3)
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
