"""Start Match cuts highlights after the receipt is signed. Failures stay off the receipt and the submission."""
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
import qorcap_highlights as hl
import qorcap_match as qm


def test_start_match_opts_in_and_leaves_the_recorder_flag_off():
    ps = (HERE.parent / "start-match.ps1").read_text(encoding="utf-8")
    assert "--no-auto-highlights" in ps and "--auto-highlights" in ps
    assert "$env:QORCAP_CONTENT_CREDENTIALS" not in ps
    assert ps.count("-not $isFinalize") >= 2
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert src.index("code = sign_and_report") < src.index("return after_receipt")
    assert 'ap.add_argument("--no-auto-highlights"' in src


def test_after_receipt_keeps_the_exit_code_and_still_submits(capsys):
    calls = []

    def highlights():
        calls.append("highlights")
        raise RuntimeError("ffmpeg died")

    def submit():
        calls.append("submit")

    assert qm.after_receipt(0, auto_highlights=True, highlights=highlights, submit=submit) == 0
    assert calls == ["highlights", "submit"]
    assert "receipt and submission are unaffected" in capsys.readouterr().out

    calls.clear()
    assert qm.after_receipt(2, auto_highlights=False, highlights=highlights, submit=submit) == 2
    assert calls == ["submit"]


def test_highlights_skip_while_recording_and_do_not_touch_the_receipt(tmp_path):
    b = tmp_path / "match"
    b.mkdir()
    receipt = b'{"schema":"qor-receipt/v0","match":{"match_id":"m"}}\n'
    (b / "receipt.json").write_bytes(receipt)
    (b / hl.MARKER).parent.mkdir(parents=True)
    (b / hl.MARKER).write_text("1\n")
    qm.post_match_highlights(b, "ffmpeg", qorcap="missing-qorcap")
    assert (b / "receipt.json").read_bytes() == receipt
    assert not (b / "highlights").exists()


def test_a_clip_step_cannot_rewrite_the_receipt(tmp_path):
    b = tmp_path / "match"
    b.mkdir()
    receipt = b'{"schema":"qor-receipt/v0","match":{"match_id":"m"},"checkpoints":{}}\n'
    (b / "receipt.json").write_bytes(receipt)
    (b / "manifest.jsonl").write_text("")
    (b / "checkpoints.jsonl").write_text("")
    qm.post_match_highlights(b, "ffmpeg", qorcap=None)
    assert (b / "receipt.json").read_bytes() == receipt
    man = json.loads((b / "highlights" / "manifest.json").read_text())
    assert man["clips"] == []
    assert man["content_credentials"]["verified_clips"] == 0


def test_honest_record_never_upgrades_a_status():
    assert hl.honest_credential_record("verified", True) == {
        "attached": True, "verified": True, "verification_status": "verified"}
    attested = hl.honest_credential_record("self_attested", False)
    assert attested["attached"] and attested["verified"] is False and attested["verification_status"] == "self_attested"
    for status, flag in (("FAIL", False), ("fail", False), ("verified", False), ("self_attested", True), (None, False)):
        rec = hl.honest_credential_record(status, flag)
        assert rec["verified"] is False and rec["attached"] is False, rec
        assert rec["reason"] == "receipt did not verify"


def test_mono_bounds_follow_the_match_clock():
    rows = [
        {"type": "segment", "kind": "media", "file": "video.mp4", "frames": 30, "mono_ms": 1000},
        {"type": "segment", "kind": "media", "file": "video.mp4", "frames": 60, "mono_ms": 2000},
    ]
    assert hl.mono_bounds(rows, "video.mp4", 0.0, 1.0) == (0, 1000)
    assert hl.mono_bounds(rows, "video.mp4", 1.0, 1.0) is None
    assert hl.mono_bounds([{"type": "segment", "file": "video.mp4"}], "video.mp4", 0.0, 1.0) is None


def _clip_manifest(tmp: Path, body: bytes) -> tuple[Path, Path]:
    b = tmp / "match"
    hl_dir = b / "highlights"
    hl_dir.mkdir(parents=True)
    (b / "manifest.jsonl").write_text(json.dumps({
        "type": "segment", "kind": "media", "file": "video.mp4", "frames": 30, "mono_ms": 1000,
    }) + "\n")
    (hl_dir / "hl00.mp4").write_bytes(body)
    man = {
        "schema": hl.SCHEMA,
        "clips": [{
            "id": "hl00", "file": "hl00.mp4", "sha256": hl.sha256_file(hl_dir / "hl00.mp4"), "bytes": len(body),
            "overlay": "burned+sidecar",
            "source": {"file": "video.mp4", "start_s": 0.0, "end_s": 1.0, "segments": []},
        }],
    }
    mp = hl_dir / "manifest.json"
    mp.write_text(json.dumps(man), encoding="utf-8")
    (b / "receipt.json").write_text("{}\n")
    return b, mp


def test_failed_receipt_leaves_the_clip_unsigned(tmp_path):
    body = b"plain-clip-bytes"
    b, mp = _clip_manifest(tmp_path, body)

    def run(cmd):
        assert cmd[1:3] == ["clip", "sign"]
        return 4, "", "error: refusing to attach content credentials: receipt did not verify"

    man = hl.attach_credentials(b, mp, qorcap="qorcap", run=run)
    clip = man["clips"][0]
    assert (b / "highlights" / "hl00.mp4").read_bytes() == body
    assert clip["content_credentials"]["attached"] is False
    assert clip["content_credentials"]["verified"] is False
    assert clip["content_credentials"]["reason"] == "receipt did not verify"
    assert man["content_credentials"]["verified_clips"] == 0


def test_a_lying_verified_bit_is_removed(tmp_path):
    body = b"plain-clip-bytes"
    b, mp = _clip_manifest(tmp_path, body)

    def run(cmd):
        Path(cmd[cmd.index("--clip") + 1]).write_bytes(b'{"verified":true,"verification_status":"self_attested"}')
        return 0, "verification_status: self_attested\nverified: false\n", ""

    man = hl.attach_credentials(b, mp, qorcap="qorcap", run=run)
    assert (b / "highlights" / "hl00.mp4").read_bytes() == body
    cred = man["clips"][0]["content_credentials"]
    assert cred["verified"] is False and cred["attached"] is False
    errs = hl.check_manifest(mp)
    assert not any("says verified" in e for e in errs)


def test_self_attested_credentials_stay_unverified(tmp_path):
    body = b"plain-clip"
    b, mp = _clip_manifest(tmp_path, body)
    signed = b'org.qorcap.match-receipt-v0 {"verified":false,"verification_status":"self_attested"}'

    def run(cmd):
        Path(cmd[cmd.index("--clip") + 1]).write_bytes(signed)
        return 0, "verification_status: self_attested\nverified: false\n", ""

    man = hl.attach_credentials(b, mp, qorcap="qorcap", run=run)
    cred = man["clips"][0]["content_credentials"]
    assert cred == {"attached": True, "verified": False, "verification_status": "self_attested"}
    assert (b / "highlights" / "hl00.mp4").read_bytes() == signed
    assert man["clips"][0]["sha256"] == hl.sha256_file(b / "highlights" / "hl00.mp4")
    assert hl.credential_problems("hl00", cred, signed) == []


def test_manifest_rejects_a_verified_claim_on_an_unsigned_clip(tmp_path):
    body = b"plain-clip"
    _b, mp = _clip_manifest(tmp_path, body)
    man = json.loads(mp.read_text())
    man["clips"][0]["content_credentials"] = {"attached": True, "verified": True, "verification_status": "verified"}
    mp.write_text(json.dumps(man))
    errs = hl.check_manifest(mp)
    assert any("verified" in e for e in errs)
