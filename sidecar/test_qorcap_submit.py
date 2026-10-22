"""qorcap submit against an in-process QorTime referee service (needs a qortime checkout with qor_receipt.review)."""
import io
import json
import os
import random
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
QT = next((Path(p) for p in [os.getenv("QORTIME", ""), HERE.parent / "qortime", HERE.parent.parent / "qortime"]
           if p and (Path(p) / "qor_receipt" / "review").exists()), None)
pytestmark = [pytest.mark.skipif(QT is None, reason="no qortime checkout with the referee service"),
              pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")]
if QT:
    sys.path.insert(0, str(QT))
    sys.path.insert(0, str(QT / "tests"))
sys.path.insert(0, str(HERE))
import qorcap_submit as qs  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(tc):
    """urllib.urlopen stand-in that routes requests into a FastAPI TestClient."""
    import urllib.error

    def open_(rq, timeout=None):
        path = rq.full_url.split("://", 1)[1]
        path = path[path.index("/"):]
        r = tc.request(rq.get_method(), path, content=rq.data, headers=dict(rq.header_items()))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(rq.full_url, r.status_code, "err", {}, io.BytesIO(r.content))
        return _Resp(r.content)
    return open_


def _env(tmp_path, tokens=True, core=None):
    import review_fixture as F
    from fastapi.testclient import TestClient

    from qor_receipt.keys import SoftwareKey
    from qor_receipt.referee_service.app import create_app
    from qor_receipt.review.engine import Referee

    clock = {"t": F.W0}
    key = SoftwareKey()
    ref = Referee(key, workdir=tmp_path / "work", now=lambda: clock["t"], rng=random.Random(3),
                  core_factory=core or F.fake_factory(), tail_ms=8_000, random_segments=3)
    toks = []
    if tokens:
        for ph, iss, *_ in F.CHALLENGES:
            clock["t"] = F.W0 + iss
            toks.append(ref.issue_challenge(ph, "home", "local-test-review"))
    fx = F.build(tmp_path / "match-1", challenge_tokens=toks or None)
    clock["t"] = F.W0 + 120_000
    os.environ["QORTIME_POW_BITS_UNKNOWN"] = "8"  # keep the hashcash cheap in tests (production: 20 bits)
    tc = TestClient(create_app(ref))
    return F, key, fx, qs.Client("http://referee.test", opener=_opener(tc))


def test_submit_spot_check_end_to_end(tmp_path, capsys):
    F, key, fx, client = _env(tmp_path)
    code = qs.submit(tmp_path / "match-1", base="http://referee.test", client=client, device=fx["device"])
    out = capsys.readouterr().out
    # The test key is not on the built-in allowlist, so the honest local verdict is SELF_ATTESTED (exit 1).
    assert code == 1, out
    rec = json.loads((tmp_path / "match-1" / "receipt.official.json").read_text())
    assert rec["referee"]["pubkey"] == key.pubkey_hex and rec["review"]["checks"]["video_bytes"]["state"] == "pass"
    sub = json.loads((tmp_path / "match-1" / "submission.json").read_text())
    assert sub["state"] == "complete"
    assert "referee asked for" in out and "OFFICIAL RESULT" in out


def test_submit_reports_refusal(tmp_path, capsys):
    import review_fixture as F0

    F, key, fx, client = _env(tmp_path / "b", core=F0.fake_factory(home=3, away=0))  # referee reads a different final
    code = qs.submit(tmp_path / "b" / "match-1", base="http://referee.test", client=client, device=fx["device"])
    out = capsys.readouterr().out
    assert code == 2 and "NOT VERIFIED" in out and "referee read HOME 3" in out
    assert not (tmp_path / "b" / "match-1" / "receipt.official.json").exists()


def test_submit_rejected_bundle(tmp_path, capsys):
    F, key, fx, client = _env(tmp_path)
    p = tmp_path / "match-1" / "inputs.jsonl"
    p.write_bytes(p.read_bytes().replace(b'"b": 512', b'"b": 2', 1))
    code = qs.submit(tmp_path / "match-1", base="http://referee.test", client=client, device=fx["device"])
    out = capsys.readouterr().out
    assert code == 2 and "REJECTED" in out


def test_official_live_anchors_and_challenges(tmp_path):
    """v0.3.11: challenges open only at pause-capable moments from the gate (never on a timer / at a kickoff),
    one phase per quarter band, one open window at a time; anchors keep their 60 s cadence."""
    from qorcap_playcall import GateEvent
    F, key, fx, client = _env(tmp_path, tokens=False)

    class Api:
        posted = []

        def get(self, path, timeout=3):
            return {"chain_head": "c" * 64, "segments": 7, "checkpoint_head": "d" * 64}

        def post(self, path, body=None, timeout=5):
            self.posted.append((path, body))
            return {"ok": True}

    class Gate:
        events = []

        def latest(self, after_ms=0):
            e = self.events[-1] if self.events else None
            return e if e is not None and e.t_ms > after_ms else None

    t, wall, gate = [0.0], [1_000_000], Gate()
    api = Api()
    live = qs.OfficialLive("http://referee.test", tmp_path, "m1", "home", api, challenges=True, client=client,
                           clock=lambda: t[0], wall=lambda: wall[0], gate=lambda: gate, beep=lambda: None)
    live.step()
    assert live.anchors == 1 and live.issued == set()          # recording started: no challenge before a play call
    gate.events.append(GateEvent(1_000_100, "play_call", 1, "kickoff", 300))
    live.step()
    assert live.issued == {"kickoff"} and "at the line, before the snap" in live.prompts[0]
    tok = json.loads((tmp_path / "official_challenges.jsonl").read_text().splitlines()[0])
    assert tok["deadline_ms"] - tok["issued_ms"] == 90_000 and live.open_until_ms == tok["deadline_ms"]
    gate.events.append(GateEvent(1_000_200, "play_call", 3, "halftime", 300))
    live.step()
    assert live.issued == {"kickoff"}                           # kickoff window still open: wait
    wall[0] = tok["deadline_ms"] + 1
    gate.events.append(GateEvent(wall[0] + 5, "play_call", 3, "halftime", 290))
    t[0] = 61
    live.step()
    assert live.issued == {"kickoff", "halftime"} and live.anchors == 2
    wall[0] += 200_000
    gate.events.append(GateEvent(wall[0], "clock_proxy", 4, "end", 800))
    live.step()
    assert live.issued == {"kickoff", "halftime", "end"}
    toks = [json.loads(x) for x in (tmp_path / "official_challenges.jsonl").read_text().splitlines()]
    assert [x["phase"] for x in toks] == ["kickoff", "halftime", "end"]
    assert toks[0]["anchor"]["chain_head"] == "c" * 64
    assert [p for p, _ in api.posted] == ["/challenge"] * 3
    gates = [json.loads(x) for x in (tmp_path / "challenge_gates.jsonl").read_text().splitlines()]
    assert [g["gate"]["basis"] for g in gates] == ["play_call", "play_call", "clock_proxy"]
    assert len((tmp_path / "anchors.jsonl").read_text().splitlines()) == 2


def test_official_live_skips_a_missed_phase_and_retries_after_network_error(tmp_path):
    from qorcap_playcall import GateEvent

    class Api:
        def get(self, path, timeout=3):
            return {"chain_head": "c" * 64, "segments": 7}

        def post(self, path, body=None, timeout=5):
            return {"ok": True}

    class Flaky:
        n = 0

        def post_json(self, path, body):
            if path == "/v1/anchors":
                return {"type": "anchor"}
            self.n += 1
            if self.n == 1:
                raise OSError("network down")
            return {"nonce": "ab" * 16, "pattern": ["OPTIONS", "CROSS", "UP", "LEFT", "SQUARE"], "issued_ms": 5,
                    "deadline_ms": 90_005, "phase": body["phase"]}

    class Gate:
        events = []

        def latest(self, after_ms=0):
            e = self.events[-1] if self.events else None
            return e if e is not None and e.t_ms > after_ms else None

    t, gate = [0.0], Gate()
    live = qs.OfficialLive("http://x", tmp_path, "m1", "home", Api(), challenges=True, client=Flaky(),
                           clock=lambda: t[0], wall=lambda: 100_000, gate=lambda: gate, beep=lambda: None)
    gate.events.append(GateEvent(10, "play_call", 3, "halftime", 300))
    live.step()
    assert live.issued == set() and live.failures == 1 and live.skipped == {"kickoff"}
    gate.events.append(GateEvent(20, "play_call", 3, "halftime", 290))
    live.step()
    assert live.issued == set()                                 # retry cool-down
    t[0] = 20
    gate.events.append(GateEvent(30, "play_call", 3, "halftime", 280))
    live.step()
    assert live.issued == {"halftime"}
    lines = [json.loads(x) for x in (tmp_path / "challenge_gates.jsonl").read_text().splitlines()]
    assert lines[0]["skipped"] is True and lines[0]["phase"] == "kickoff"


def test_offer_submit_needs_explicit_yes(tmp_path, capsys):
    if not (HERE / "qorcap_match.py").exists():
        pytest.skip("no qorcap_match")
    import qorcap_match as qm

    assert qm.offer_submit(tmp_path, None, ask=lambda _p: "") is None
    assert "Not submitted" in capsys.readouterr().out


def test_submit_is_install_key_signed_and_unsigned_is_refused(tmp_path, capsys):
    """0.3.10: the POST carries X-Qor-Submit signed by the device (recorder) key; an unsigned POST (<= 0.3.9) gets 426."""
    F, key, fx, client = _env(tmp_path)
    seen = []
    inner = client._open

    def spy(rq, timeout=None):
        seen.append((rq.get_method(), rq.full_url, dict(rq.header_items())))
        return inner(rq, timeout=timeout)
    client._open = spy
    assert qs.submit(tmp_path / "match-1", base="http://referee.test", client=client, device=fx["device"]) == 1
    post = next(h for m, u, h in seen if m == "POST" and "/v1/submissions?" in u)
    hv = next(v for k, v in post.items() if k.lower() == "x-qor-submit")
    import base64
    h = json.loads(base64.urlsafe_b64decode(hv + "=" * (-len(hv) % 4)))
    assert h["pub"] == fx["device"].pubkey_hex
    legacy = qs.Client("http://referee.test", opener=inner)
    with pytest.raises(qs.RefereeError) as ei:
        legacy.call("POST", "/v1/submissions?mode=spot-check", qs.meta_zip(tmp_path / "match-1"), "application/zip")
    assert ei.value.code == 426 and "0.3.10" in json.dumps(ei.value.body)


def test_submit_from_another_pc_key_is_rejected(tmp_path, capsys):
    from qor_receipt.keys import SoftwareKey

    F, key, fx, client = _env(tmp_path)
    code = qs.submit(tmp_path / "match-1", base="http://referee.test", client=client, device=SoftwareKey())
    out = capsys.readouterr().out
    assert code == 2 and "REJECTED" in out and "recorder" in out


def test_client_signature_matches_qortime_guard():
    """Contract: QorCap's own X-Qor-Submit encoder verifies under QorTime's SubmitGuard (no drift)."""
    from qor_receipt.keys import SoftwareKey
    from qor_receipt.referee_service import submit_auth as A

    assert qs.SUBMIT_TYPE == A.SUBMIT_TYPE and qs.POW_PREFIX == A.POW_PREFIX
    k = SoftwareKey()
    g = A.SubmitGuard(require_recorder_match=False)
    g.pow_unknown = 6
    ch = g.challenge(k.pubkey_hex)
    body, head = b"zip-bytes", "ab" * 32
    hv = qs.submit_auth_header(k, path="/v1/submissions?mode=full", body=body, bundle_head=head, nonce=ch["nonce"],
                               pow_=qs.solve_pow(ch["nonce"], k.pubkey_hex, 6))
    h = g.precheck(hv)
    files = {"manifest.jsonl": b""}
    with pytest.raises(A.AuthError):  # head mismatch caught (manifest bytes differ)
        g.verify_body(h, method="POST", path="/v1/submissions?mode=full", body=body, files=files)
    import hashlib
    hv2 = qs.submit_auth_header(k, path="/p", body=body, bundle_head=hashlib.sha256(b"m").hexdigest(),
                                nonce=g.challenge(k.pubkey_hex)["nonce"], pow_="0")
    g.pow_unknown = 0
    g.verify_body(g.precheck(hv2), method="POST", path="/p", body=body, files={"manifest.jsonl": b"m"})


def test_official_key_status_accepts_only_root_or_valid_delegation():
    from qor_receipt import delegation as D
    from qor_receipt.keys import SoftwareKey

    assert qs.official_key_status({"pubkey": qs.OFFICIAL_PUB})[0]
    other = SoftwareKey()
    ok, why = qs.official_key_status({"pubkey": other.pubkey_hex})
    assert not ok
    fake_root = SoftwareKey()
    cert = D.make_delegation(fake_root, root_id="qortime-v1", delegate_pubkey=other.pubkey_hex, delegate_id="x",
                             serial=1, not_before_ms=0, not_after_ms=10**13 // 100)
    ok, why = qs.official_key_status({"pubkey": other.pubkey_hex, "delegation": cert})
    assert not ok and "delegation" in why


def test_fill_challenge_results_on_real_fmp4(tmp_path, capsys):
    """v0.3.11 fix for match 4 (44b8d01f): the receipt carried result=null although the presses matched. Finalize now
    re-runs the referee's rules on the bundle's own video + controller log: answered challenges become ok."""
    import review_fixture as F

    import qorcap_match as qm
    fx = F.build(tmp_path / "m")
    draft = json.loads((tmp_path / "m" / "receipt.draft.json").read_text()) if (tmp_path / "m" / "receipt.draft.json").exists() \
        else {"challenges": json.loads((tmp_path / "m" / "receipt.json").read_text())["challenges"]}
    chs = [dict(c, result=None) for c in draft["challenges"]]
    assert len(chs) == 3
    out = qm.fill_challenge_results(tmp_path / "m", {"challenges": chs})
    res = [c["result"] for c in out["challenges"]]
    assert all(r and r["ok"] and r["reverified_by"] == "recorder" and 0 <= r["latency_ms"] <= 600 for r in res), res
    lines = [json.loads(x) for x in (tmp_path / "m" / "challenge_results.jsonl").read_text().splitlines()]
    assert [x["phase"] for x in lines] == ["kickoff", "halftime", "end"]
    assert "PASSED" in capsys.readouterr().out
    # a window that saw no presses stays unanswered (never a FAIL on the player's own receipt)
    late = [dict(c, issued_ms=c["issued_ms"] + 200_000, deadline_ms=c["deadline_ms"] + 200_000) for c in chs]
    out2 = qm.fill_challenge_results(tmp_path / "m", {"challenges": late})
    assert [c["result"] for c in out2["challenges"]] == [None] * 3
    assert all(json.loads(x)["reason"] == "no_pause_press"
               for x in (tmp_path / "m" / "challenge_results.jsonl").read_text().splitlines())
    assert fx is not None


def test_submit_prompt_times_out_to_no(tmp_path, capsys):
    """v0.3.23: an unattended "Submit now?" prompt answers No after the timeout instead of waiting forever."""
    import os
    import qorcap_match as qm
    r, w = os.pipe()
    with os.fdopen(r) as fin, os.fdopen(w, "w") as fout:
        assert qm.ask_with_timeout("Submit now? [y/N] ", 0.3, stdin=fin) == ""
        fout.write("y\n")
        fout.flush()
        assert qm.ask_with_timeout("Submit now? [y/N] ", 2.0, stdin=fin) == "y"
    assert "No answer" in capsys.readouterr().out
    # the timeout answer is a plain No: nothing is submitted
    assert qm.offer_submit(tmp_path, None, ask=lambda p: qm.ask_with_timeout(p, 0.1, stdin=open(os.devnull))) is None


class _NoNet:
    """Client stand-in. submit() must not call it when the receipt is not submittable."""

    def __init__(self):
        self.calls = 0

    def call(self, *a, **k):
        self.calls += 1
        raise OSError("network")


def _resign(cp, key, match_id, role, ts):
    from qor_receipt.checkpoints import countersign

    body = {k: cp[k] for k in ("seq", "t_ms", "frames_hash", "input_hash", "prev", "hash")}
    return countersign(key, match_id, role, body, int(ts))


def _refused(bundle, capsys):
    net = _NoNet()
    code = qs.submit(bundle, base="http://referee.test", client=net)
    out = capsys.readouterr().out
    sub = json.loads((bundle / "submission.json").read_text())
    assert net.calls == 0, out
    assert code == 4, out
    assert sub["state"] == "not_submitted"
    return sub, out


def test_side_corrected_bundle_refused_before_network(tmp_path, capsys):
    """finalize --role re-signs every checkpoint offline. The referee rejects that; so does submit(), first."""
    from qor_receipt.keys import SoftwareKey

    _env(tmp_path)
    b = tmp_path / "match-1"
    rec = json.loads((b / "receipt.json").read_text())
    meta = json.loads((b / "bundle.json").read_text())
    local = SoftwareKey()
    mid = rec["match"]["match_id"]
    rec["checkpoints"] = {"away": [_resign(cp, local, mid, "away", cp["t_ms"]) for cp in rec["checkpoints"]["home"]]}
    rec["players"][0]["role"] = "away"
    rec["referee"]["key_kind"] = qs.OFFLINE_KEY_KIND
    meta["role"] = "away"
    (b / "receipt.json").write_text(json.dumps(rec), encoding="utf-8")
    (b / "bundle.json").write_text(json.dumps(meta), encoding="utf-8")
    sub, out = _refused(b, capsys)
    assert sub["checkpoint"] == 0 and sub["field"] == "referee_sig"
    assert "checkpoint 0" in out and "checkpoint 0" in sub["error"]


def test_tampered_timestamp_resigned_offline_is_refused(tmp_path, capsys):
    """One checkpoint's referee timestamp, re-signed offline, is not the live countersignature."""
    from qor_receipt.keys import SoftwareKey

    _env(tmp_path)
    b = tmp_path / "match-1"
    rec = json.loads((b / "receipt.json").read_text())
    local = SoftwareKey()
    mid = rec["match"]["match_id"]
    cps = rec["checkpoints"]["home"]
    cps[0] = _resign(cps[0], local, mid, "home", int(cps[0]["referee_ts_ms"]) + 5_000)
    rec["checkpoints"]["home"] = cps
    rec["referee"]["key_kind"] = qs.OFFLINE_KEY_KIND
    (b / "receipt.json").write_text(json.dumps(rec), encoding="utf-8")
    sub, out = _refused(b, capsys)
    assert sub["checkpoint"] == 0 and sub["field"] == "referee_sig"
    assert "checkpoint 0" in sub["error"] and "NOT SUBMITTABLE" in out


def test_offline_signed_receipt_refused_when_live_countersigned_log_exists(tmp_path, capsys):
    """Copied live signatures still must not be submitted under an offline key kind."""
    _env(tmp_path)
    b = tmp_path / "match-1"
    rec = json.loads((b / "receipt.json").read_text())
    rec["referee"]["key_kind"] = qs.OFFLINE_KEY_KIND
    (b / "receipt.json").write_text(json.dumps(rec), encoding="utf-8")
    sub, _out = _refused(b, capsys)
    assert sub["checkpoint"] == 0 and sub["field"] == "key_kind"
    (b / "countersigned.jsonl").unlink()
    assert qs.countersigned_mismatch(b) is None


def test_live_signed_bundle_still_passes(tmp_path, capsys):
    F, key, fx, client = _env(tmp_path)
    b = tmp_path / "match-1"
    assert (b / "countersigned.jsonl").stat().st_size > 0
    assert qs.countersigned_mismatch(b) is None
    code = qs.submit(b, base="http://referee.test", client=client, device=fx["device"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert json.loads((b / "submission.json").read_text())["state"] == "complete"
    assert "NOT SUBMITTABLE" not in out
