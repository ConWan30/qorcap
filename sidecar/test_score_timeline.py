"""Signed score timeline sidecar. It is not a receipt field."""
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
QT = next((Path(p) for p in [os.getenv("QORTIME", ""), ROOT / "qortime", ROOT.parent / "qortime"]
           if p and (Path(p) / "qor_receipt").exists()), None)
pytestmark = pytest.mark.skipif(QT is None, reason="no qortime checkout")
if QT:
    sys.path.insert(0, str(QT))
sys.path.insert(0, str(HERE))
import qorcap_score_timeline as st  # noqa: E402


def _rows():
    return [
        {"t_ms": 1000, "home": 0, "away": 0, "quarter": 1, "clock_s": 240},
        {"t_ms": 1500, "home": 0, "away": 0, "quarter": 1, "clock_s": 230},
        {"type": "obs", "t_ms": 2000, "obs": "scorebug_absent"},
        {"t_ms": 8000, "home": 7, "away": 0, "quarter": 1},
        {"t_ms": 9000, "home": 7, "away": 3, "quarter": 2},
    ]


def test_health_observations_do_not_change_the_score_chain():
    """reader_lag and score_unagreed are not score rows. The signed chain is the scores alone."""
    scores = [
        {"t_ms": 1000, "home": 0, "away": 0, "quarter": 1},
        {"t_ms": 5000, "home": 3, "away": 0, "quarter": 1},
    ]
    base, head = st.chain_events(st.events_from_timeline(scores))
    mixed = scores + [
        {"t_ms": 2000, "until_ms": 4000, "obs": "reader_lag", "n": 1, "max_gap_ms": 2100},
        {"t_ms": 6000, "until_ms": 16000, "obs": "score_unagreed", "n": 4, "reasons": {"agreeing": 4}},
    ]
    again, head2 = st.chain_events(st.events_from_timeline(mixed))
    assert again == base and head2 == head
    assert st.events_from_timeline(mixed) == st.events_from_timeline(scores)


def test_collapse_keeps_the_first_timestamp_of_each_score():
    events = st.events_from_timeline(_rows())
    assert events == [
        {"t_ms": 1000, "home": 0, "away": 0, "quarter": 1},
        {"t_ms": 8000, "home": 7, "away": 0, "quarter": 1},
        {"t_ms": 9000, "home": 7, "away": 3, "quarter": 2},
    ]


def test_chain_links_and_device_signature(tmp_path):
    from qor_receipt.keys import SoftwareKey

    device = SoftwareKey()
    events = st.events_from_timeline(_rows())
    head = "ab" * 32
    doc = st.sign_doc(st.build(events, checkpoint_head=head), device)
    assert doc["signals"]["score_timeline"]["events"] == events
    assert doc["chain"][0]["prev"] == st.GENESIS
    assert doc["chain"][1]["prev"] == doc["chain"][0]["hash"]
    assert doc["chain_head"] == doc["chain"][-1]["hash"]
    assert st.verify_doc(doc) == []
    doc["signals"]["score_timeline"]["events"][1]["t_ms"] = 8001
    assert "signature" in st.verify_doc(doc) or "chain" in st.verify_doc(doc)
    # a one-field edit of the signed events is not a valid timeline
    broken = st.sign_doc(st.build(events, checkpoint_head=head), device)
    broken["signals"]["score_timeline"]["events"][0]["home"] = 99
    assert st.verify_doc(broken) != []


def test_sidecar_is_not_written_into_the_receipt(tmp_path):
    from qor_receipt.keys import SoftwareKey

    bundle = tmp_path / "match"
    bundle.mkdir()
    (bundle / "timeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _rows()), encoding="utf-8")
    receipt = {
        "schema": "qor-receipt/v0",
        "players": [{"role": "home"}],
        "checkpoints": {"home": [{"hash": "cd" * 32}]},
        "signals": {"motion_permille": {"home": None}},
    }
    raw = json.dumps(receipt)
    (bundle / "receipt.json").write_text(raw, encoding="utf-8")
    path = st.write_score_timeline(bundle, SoftwareKey(), receipt)
    assert (bundle / "receipt.json").read_text(encoding="utf-8") == raw
    assert "score_timeline" not in raw
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == st.SCHEMA
    assert doc["checkpoint_head"] == "cd" * 32
    assert doc["signals"]["score_timeline"]["events"][1] == {"t_ms": 8000, "home": 7, "away": 0, "quarter": 1}
    assert st.verify_doc(doc) == []
    schema = json.loads((ROOT / "contracts" / "receipt.v0.json").read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert "score_timeline" not in schema["properties"]
    assert "score_timeline" not in schema["properties"]["signals"]["properties"]
    side = json.loads((ROOT / "contracts" / "score_timeline.v0.json").read_text(encoding="utf-8"))
    assert side["properties"]["signals"]["properties"]["score_timeline"]["required"] == ["events"]
