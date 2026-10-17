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
    F, key, fx, client = _env(tmp_path, tokens=False)

    class Api:
        q = 1
        posted = []

        def get(self, path, timeout=3):
            if path.startswith("/checkpoints"):
                return {"chain_head": "c" * 64, "segments": 7, "checkpoint_head": "d" * 64}
            return {"score": {"quarter": self.q}}

        def post(self, path, body=None, timeout=5):
            self.posted.append((path, body))
            return {"ok": True}

    t = [0.0]
    api = Api()
    live = qs.OfficialLive("http://referee.test", tmp_path, "m1", "home", api, challenges=True, client=client,
                           clock=lambda: t[0])
    live.step()
    assert live.anchors == 1 and "kickoff" in live.issued
    api.q = 3
    t[0] = 30
    live.step()
    assert "halftime" in live.issued and live.anchors == 1  # next anchor only after 60 s
    api.q = 4
    t[0] = 61
    live.step()
    assert live.issued == {"kickoff", "halftime", "end"} and live.anchors == 2
    toks = [json.loads(x) for x in (tmp_path / "official_challenges.jsonl").read_text().splitlines()]
    assert [x["phase"] for x in toks] == ["kickoff", "halftime", "end"]
    assert toks[0]["anchor"]["chain_head"] == "c" * 64
    assert [p for p, _ in api.posted] == ["/challenge"] * 3
    assert len((tmp_path / "anchors.jsonl").read_text().splitlines()) == 2


def test_offer_submit_needs_explicit_yes(tmp_path, capsys):
    if not (HERE / "qorcap_match.py").exists():
        pytest.skip("no qorcap_match")
    import qorcap_match as qm

    assert qm.offer_submit(tmp_path, None, ask=lambda _p: "") is None
    assert "Not submitted" in capsys.readouterr().out
