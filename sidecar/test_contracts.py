"""QorCap <-> QorTime contract tests (vendored registry: contracts/, pinned in contracts/CONTRACTS_PIN).

Unit part (always runs): vendored files are intact; sidecar constants match the registry.
E2E part (needs ffmpeg + a qortime checkout, ~1-2 min): runs the REAL Start Match (--test-pattern) -> QorCap
writer -> local countersign -> receipt, then asserts both verifiers (qortime verify + qorcap verify-bundle, plus
QorTime's official-review structural checks) agree on the good bundle and on tampered / foreign-key /
missing-checkpoint variants, and that the sealed stop checkpoint meets QorTime's gap/skew rule.
QorCap's GitHub CI has no workflow token yet: run locally with  python -m pytest -q sidecar/test_contracts.py
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
C = ROOT / "contracts"
COMPAT = json.loads((C / "compat.json").read_text(encoding="utf-8"))
QT = next((Path(p) for p in [os.getenv("QORTIME", ""), ROOT / "qortime", ROOT.parent / "qortime"]
           if p and (Path(p) / "qor_receipt" / "review").exists()), None)
sys.path.insert(0, str(HERE))


def test_vendored_contracts_intact():
    for line in (C / "SHA256SUMS").read_text().splitlines():
        h, name = line.split()
        assert hashlib.sha256((C / name).read_bytes()).hexdigest() == h, f"{name} edited locally: change it in qortime and re-vendor"


@pytest.mark.skipif(QT is None or not (QT / "schema" / "contracts").exists(), reason="no qortime checkout with schema/contracts")
def test_vendored_contracts_equal_qortime_checkout():
    for p in (QT / "schema" / "contracts").glob("*.json"):
        assert (C / p.name).read_bytes() == p.read_bytes(), f"contracts/{p.name} drifted from qortime"
    assert (C / "receipt.v0.json").read_bytes() == (QT / "schema" / "receipt.v0.json").read_bytes()


def test_sidecar_constants_match_registry():
    import qorcap_submit as qs
    assert qs.OFFICIAL_PUB == COMPAT["official_referee"]["pubkey"]
    assert list(qs.META_FILES) == COMPAT["bundle_files"]["meta"]
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    for prof in COMPAT["scorebug_profiles"]:
        assert prof in src + (HERE / "qorcap_mnp.py").read_text(encoding="utf-8")


def test_every_verdict_has_an_exit_code():
    if QT:
        sys.path.insert(0, str(QT))
    import qorcap_match as qm
    assert set(COMPAT["verdicts"]) <= set(qm.EXIT_CODES)
    assert qm.EXIT_CODES["SELF_ATTESTED"] == 0 and qm.EXIT_CODES["FAIL"] == 2


# ---------------------------------------------------------------------------------------------- E2E
def _qorcap_bin():
    for p in [os.getenv("QORCAP_BIN"), ROOT / "target" / "release" / "qorcap", ROOT / "target" / "debug" / "qorcap"]:
        if p and Path(p).exists():
            return str(p)
    return None


e2e = pytest.mark.skipif(QT is None or _qorcap_bin() is None or shutil.which("ffmpeg") is None
                         or os.getenv("QORCAP_E2E", "1") == "0", reason="needs qortime checkout, built qorcap, ffmpeg")


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory):
    out, keys = tmp_path_factory.mktemp("matches"), tmp_path_factory.mktemp("keys")
    env = dict(os.environ, PYTHONPATH=str(QT))
    p = subprocess.Popen([sys.executable, str(HERE / "qorcap_match.py"), "--test-pattern", "--test-secs", "8",
                          "--qortime", str(QT), "--qorcap", _qorcap_bin(), "--out", str(out), "--key-dir", str(keys),
                          "--no-open", "--no-submit-prompt", "--no-anchors", "--port", os.getenv("QORCAP_E2E_PORT", "8793")],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    time.sleep(float(os.getenv("QORCAP_E2E_SECS", "25")))
    p.send_signal(signal.SIGINT)  # same stop path as Ctrl+C in the Start Match window
    log = p.communicate(timeout=600)[0].decode("utf-8", "replace")
    b = next(out.glob("match-*"))
    return {"dir": b, "code": p.returncode, "log": log, "pub": (b / "referee.pub").read_text().strip()}


def _qt_verify(receipt: dict, pub: str) -> dict:
    sys.path.insert(0, str(QT))
    from qor_receipt.receipt import verify_receipt
    return verify_receipt(receipt, referee_pub=pub)


def _bundle_ok(d: Path) -> bool:
    return subprocess.run([_qorcap_bin(), "verify-bundle", str(d)], capture_output=True).returncode == 0


def _review_errors(d: Path) -> list:
    sys.path.insert(0, str(QT))
    from qor_receipt.review import bundle as B
    b = B.load({f: (d / f).read_bytes() for f in COMPAT["bundle_files"]["meta"] if (d / f).exists()})
    ib, e1 = B.check_inputs(b)
    return b.errors + B.check_manifest(b) + e1 + B.check_checkpoints(b, ib) + B.check_receipt_binding(b)


@e2e
def test_real_bundle_good_both_verifiers_agree(real_bundle):
    d = real_bundle["dir"]
    assert real_bundle["code"] == 0, real_bundle["log"][-2000:]
    rec = json.loads((d / "receipt.json").read_text())
    v = _qt_verify(rec, real_bundle["pub"])
    assert v["status"] == "SELF_ATTESTED" and v["errors"] == []
    assert _bundle_ok(d)
    assert _review_errors(d) == []


@e2e
def test_real_bundle_matches_shared_schemas(real_bundle):
    js = pytest.importorskip("jsonschema")
    d = real_bundle["dir"]
    js.validate(json.loads((d / "receipt.json").read_text()), json.loads((C / "receipt.v0.json").read_text()))
    js.validate(json.loads((d / "bundle.json").read_text()), json.loads((C / "qorcap-bundle.v0.json").read_text()))
    cs = json.loads((C / "qorcap-checkpoint.v0.json").read_text())
    for line in (d / "checkpoints.jsonl").read_text().splitlines():
        js.validate(json.loads(line), cs)


@e2e
def test_real_bundle_stop_checkpoint_meets_qortime_timing(real_bundle):
    d, t = real_bundle["dir"], COMPAT["checkpoint_timing"]
    raw = [json.loads(x) for x in (d / "checkpoints.jsonl").read_text().splitlines()]
    signed = [json.loads(x) for x in (d / "countersigned.jsonl").read_text().splitlines()]
    assert raw[-1]["qorcap"]["trigger"] == "stop" and len(signed) == len(raw)
    rec = json.loads((d / "receipt.json").read_text())
    assert signed[0]["referee_ts_ms"] - rec["match"]["started_ms"] <= t["max_gap_ms"]
    for a, b in zip(signed, signed[1:]):
        assert b["referee_ts_ms"] - a["referee_ts_ms"] <= t["max_gap_ms"]
    for c in signed:
        assert abs(c["referee_ts_ms"] - c["t_ms"]) <= t["max_skew_ms"]
    assert rec["match"]["ended_ms"] == signed[-1]["referee_ts_ms"]


def _copy(real_bundle, tmp_path):
    d = tmp_path / "b"
    shutil.copytree(real_bundle["dir"], d)
    return d


@e2e
def test_real_bundle_tampered_score_both_fail(real_bundle, tmp_path):
    d = _copy(real_bundle, tmp_path)
    rec = json.loads((d / "receipt.json").read_text())
    rec["result"]["home_score"] += 7
    (d / "receipt.json").write_text(json.dumps(rec))
    assert _qt_verify(rec, real_bundle["pub"])["status"] == "FAIL"


@e2e
def test_real_bundle_tampered_video_both_fail(real_bundle, tmp_path):
    d = _copy(real_bundle, tmp_path)
    v = d / "video.mp4"
    b = bytearray(v.read_bytes()); b[len(b) // 2] ^= 0xFF; v.write_bytes(bytes(b))
    assert not _bundle_ok(d)


@e2e
def test_real_bundle_foreign_key_fails(real_bundle):
    rec = json.loads((real_bundle["dir"] / "receipt.json").read_text())
    assert _qt_verify(rec, "ab" * 32)["status"] == "FAIL"
    assert _qt_verify(rec, COMPAT["official_referee"]["pubkey"])["status"] == "FAIL"  # cannot claim the official key


@e2e
def test_real_bundle_missing_checkpoint_both_fail(real_bundle, tmp_path):
    d = _copy(real_bundle, tmp_path)
    rec = json.loads((d / "receipt.json").read_text())
    del rec["checkpoints"][next(iter(rec["checkpoints"]))][2]
    assert _qt_verify(rec, real_bundle["pub"])["status"] == "FAIL"
    lines = (d / "checkpoints.jsonl").read_text().splitlines()
    (d / "checkpoints.jsonl").write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    assert _review_errors(d) != []


@e2e
def test_real_bundle_old_recorder_late_tail_fails(real_bundle, tmp_path):
    """v0.3.2 failure mode: tail countersigned after the bundle work. Re-stamp the tail +28 s with the same
    local referee key: QorTime must say gap_too_long/clock_skew on exactly the last checkpoints."""
    sys.path.insert(0, str(QT))
    import qorcap_match as qm
    from qor_receipt.checkpoints import body_of, countersign
    d = real_bundle["dir"]
    keys = next(p for p in d.parents[1].iterdir() if p.name.startswith("keys"))
    dev, ref = qm.load_keys(keys)
    rec = json.loads((d / "receipt.json").read_text())
    role = next(iter(rec["checkpoints"]))
    cps = rec["checkpoints"][role]
    for c in cps[-2:]:
        b = dict(body_of(c), hash=c["hash"])
        c.update(countersign(ref, rec["match"]["match_id"], role, b, c["t_ms"] + 28_000))
    draft = json.loads((d / "receipt.draft.json").read_text())
    rec2 = qm.build_receipt(draft, cps, dev, ref, match_id=rec["match"]["match_id"], role=role,
                            started_ms=rec["match"]["started_ms"])
    v = _qt_verify(rec2, real_bundle["pub"])
    n = len(cps)
    assert v["status"] == "FAIL"
    assert f"{role}:cp[{n - 2}]:gap_too_long" in v["errors"] and f"{role}:cp[{n - 1}]:clock_skew" in v["errors"]
