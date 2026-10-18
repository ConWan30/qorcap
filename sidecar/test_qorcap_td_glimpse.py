"""v0.3.23: the sidecar side of the Glimpse second opinion. Shadow by default; enforcing only with QORCAP_GLIMPSE_TD_VETO=1."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
import qorcap_reader as QR  # noqa: E402
from test_qorcap_touchdown import D02316, run  # noqa: E402


def core():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    return QR.ReaderCore(lambda b: None, None, AgreementWindow(), None, lambda tl: NS(final=False))


def test_shadow_by_default_writes_only_the_watchers_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv(QR.TD_VETO_ENV, raising=False)
    c = core()
    assert QR.wire_touchdown_glimpse(c, "match-20261018-120000") == "shadow"
    assert c.touchdown.veto is None and c.touchdown.listener is not None
    got, cc = run(D02316, td=c.touchdown)
    assert got[-1] == (21, 37, 2)
    lines = [json.loads(x) for x in (tmp_path / "QorCapGlimpse" / "match-20261018-120000" / "td_jumps.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["pending", "posted"] and lines[0]["cand"] == [21, 37]


def test_enforcing_needs_the_env_flag_and_reads_verdicts(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv(QR.TD_VETO_ENV, "0")
    assert QR.wire_touchdown_glimpse(core(), "m") == "shadow"
    monkeypatch.setenv(QR.TD_VETO_ENV, "1")
    c = core()
    assert QR.wire_touchdown_glimpse(c, "m") == "enforcing"
    d = tmp_path / "QorCapGlimpse" / "m"
    d.mkdir(parents=True, exist_ok=True)
    veto = QR.td_glimpse_veto("m")
    assert veto({"id": "x"}) is None
    (d / "td_verdicts.jsonl").write_text(json.dumps({"id": "x", "verdict": "disagree_old"}) + "\n" +
                                         json.dumps({"id": "y", "verdict": "agree"}) + "\n" +
                                         json.dumps({"id": "z", "verdict": "no_read"}) + "\n")
    assert veto({"id": "x"}) == "disagree" and veto({"id": "y"}) == "agree" and veto({"id": "z"}) is None


def test_no_match_no_wiring():
    c = core()
    assert QR.wire_touchdown_glimpse(c, None) == "off" and c.touchdown.listener is None and c.touchdown.veto is None


def test_offline_finalize_core_never_has_glimpse():
    import qorcap_match as QM
    c = QM.default_core_factory(lambda b: {})
    assert c.touchdown.listener is None and c.touchdown.veto is None
