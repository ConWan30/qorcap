"""QorCap <-> QorTime contract tests (vendored registry: contracts/, pinned in contracts/CONTRACTS_PIN).

Unit part (CI job ``contracts``, no capture card): vendored files match SHA256SUMS; sidecar constants
match the registry; the recorder's pause caps match rule v2; the copies match the qortime checkout in
``$QORTIME`` (CI checks out qortime main, not the runtime verifier pin).
E2E part (needs ffmpeg + a built qorcap + a qortime checkout, ~1-2 min): runs the REAL Start Match
(--test-pattern) -> QorCap writer -> local countersign -> receipt, then asserts both verifiers
(qortime verify + qorcap verify-bundle, plus QorTime's official-review structural checks) agree on the
good bundle and on tampered / foreign-key / missing-checkpoint variants, and that the sealed stop
checkpoint meets QorTime's gap/skew rule. CI sets QORCAP_E2E=0. Run the E2E locally.
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


# Written by QorCap. QorTime checks the same objects in code and does not ship these schemas.
QORCAP_ONLY_SCHEMAS = ("device_attest.v0.json", "score_timeline.v0.json")


def test_vendored_contracts_intact():
    for line in (C / "SHA256SUMS").read_text().splitlines():
        if not line.strip():
            continue
        h, name = line.split()
        assert hashlib.sha256((C / name).read_bytes()).hexdigest() == h, f"{name} edited locally: change it in qortime and re-vendor"


def test_qorcap_only_schemas_are_checksummed():
    """device_attest and score_timeline stay in this repo until ownership is decided.

    They are in SHA256SUMS so a local edit cannot pass silently. They are not part of the
    qortime byte-compare. If qortime gains a copy, this fails so the two trees are not
    treated as the same file without a decision.
    """
    names = [line.split()[1] for line in (C / "SHA256SUMS").read_text().splitlines() if line.strip()]
    for name in QORCAP_ONLY_SCHEMAS:
        assert name in names
        assert (C / name).is_file()
    if QT is not None and (QT / "schema" / "contracts").exists():
        for name in QORCAP_ONLY_SCHEMAS:
            assert not (QT / "schema" / "contracts" / name).exists(), (
                f"qortime now has schema/contracts/{name}; decide which tree owns it before re-vendoring"
            )


PIN = "585ccc4f2af3d67880d40e75553864df01b6e7b7"
READERS_SHA = "d3a93ececcf8448c5d2234ea9d59d08c85cf3a13e5a48687c975fc47de670dd2"
RECEIPT_SHA = "b09cef2b8f91d2a4cb066d1790965ece2c0ce17411891d6182ee5c51b93e19e4"


def test_contract_pin_and_frozen_receipt():
    pin = (C / "CONTRACTS_PIN").read_text(encoding="utf-8").split()[0]
    assert pin == PIN
    assert hashlib.sha256((C / "qorcap-readers.v0.json").read_bytes()).hexdigest() == READERS_SHA
    assert hashlib.sha256((C / "receipt.v0.json").read_bytes()).hexdigest() == RECEIPT_SHA
    compat = COMPAT
    assert compat["hash_chain"]["frames_hash"] == "sha256(JCS(qorcap))"
    assert compat["verdicts"] == ["VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL"]
    assert "frames.jsonl" in compat["bundle_files"]["meta"]
    assert "readers.json" in compat["bundle_files"]["meta"]


def test_qortime_checkout_present_when_required():
    """CI sets this. A missing checkout must fail the job, not skip the drift compare."""
    if os.getenv("QORCAP_CONTRACTS_REQUIRED") != "1":
        pytest.skip("set QORCAP_CONTRACTS_REQUIRED=1 to refuse a missing qortime checkout")
    assert QT is not None and (QT / "schema" / "contracts").is_dir(), "QORTIME checkout with schema/contracts is required"
    assert (QT / "schema" / "receipt.v0.json").is_file()
    head = subprocess.check_output(["git", "-C", str(QT), "rev-parse", "HEAD"], text=True).strip()
    assert head == PIN, f"qortime checkout {head} is not the vendored pin {PIN}"


@pytest.mark.skipif(QT is None or not (QT / "schema" / "contracts").exists(), reason="no qortime checkout with schema/contracts")
def test_vendored_contracts_equal_qortime_checkout():
    for p in (QT / "schema" / "contracts").glob("*.json"):
        assert (C / p.name).read_bytes() == p.read_bytes(), f"contracts/{p.name} drifted from qortime"
    assert (C / "receipt.v0.json").read_bytes() == (QT / "schema" / "receipt.v0.json").read_bytes()


def test_recorder_pause_caps_match_vendored_rule_v2():
    """Rule v2 caps are the ones the recorder recheck already uses. Nothing here widens them.

    motion_rules is referee scoring (PARTIAL below the floor, never FAIL). QorCap does not
    compute it. The floor, the sample size, and the bright-text window are locked so a
    re-vendor cannot loosen them without editing this test.
    """
    import qorcap_challenge as ch

    versions = COMPAT["challenge_rules"]["rule_versions"]
    v1, v2 = versions["1"], versions["2"]
    assert ch.CAPTURE_PAUSE_MAX_LUMA == v2["dark_menu_max_luma"] == 26
    assert ch.CAPTURE_PAUSE_MAX_LOWER == v2["dark_menu_max_lower"] == 16
    assert v2["pause_latency_ms"] == [0, ch.CAPTURE_PAUSE_MAX_LATENCY_MS] == [0, 2500]
    assert v2["consistency_ms"] == 300
    assert v1["pause_latency_ms"] == [0, 600]
    assert v1["dark_menu_max_luma"] == 16 and v1["dark_menu_max_lower"] == 8
    assert v1["dark_menu_max_luma"] < v2["dark_menu_max_luma"]
    assert v1["dark_menu_max_lower"] < v2["dark_menu_max_lower"]
    assert v1["dark_menu_bright_frac"] == v2["dark_menu_bright_frac"] == [0.005, 0.05]
    assert "dark_menu_bright" not in (HERE / "qorcap_challenge.py").read_text(encoding="utf-8")
    bundle = json.loads((C / "qorcap-bundle.v0.json").read_text(encoding="utf-8"))
    assert bundle["properties"]["challenge_rule"]["enum"] == [1, 2]
    assert set(versions) == {"1", "2"}
    motion = COMPAT["motion_rules"]
    assert motion["current"] == 3
    assert motion["floor_permille"] == 300
    assert motion["min_bins"] == 20
    assert motion["max_lag_bins"] == 3
    assert motion["sample_max_segments"] == 24
    assert motion["sample_hz"] == 10
    assert motion["sample_size"] == [640, 360]
    assert set(motion["rule_versions"]) == {"3"}
    assert COMPAT["checkpoint_timing"]["max_gap_ms"] == 10000
    assert COMPAT["checkpoint_timing"]["max_skew_ms"] == 3000
    leave = COMPAT["challenge_rules"]["menu_leave_buttons"]
    assert leave == ["CROSS", "CIRCLE"]


def test_sidecar_constants_match_registry():
    import qorcap_submit as qs
    assert qs.OFFICIAL_PUB == COMPAT["official_referee"]["pubkey"]
    pinned = tuple(COMPAT["bundle_files"]["meta"])
    assert tuple(qs.META_FILES) == pinned
    assert pinned[-2:] == ("frames.jsonl", "readers.json")
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    for f in ("qorcap_mnp.py", "qorcap_snp.py", "qorcap_tnp.py"):
        src += (HERE / f).read_text(encoding="utf-8")
    for prof in COMPAT["scorebug_profiles"]:
        assert prof in src


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
