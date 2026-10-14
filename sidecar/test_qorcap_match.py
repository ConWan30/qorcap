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
