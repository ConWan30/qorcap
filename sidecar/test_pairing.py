"""Upload list and pairing.json. No network, no ffmpeg, no qortime checkout."""
import io
import json
import zipfile
from pathlib import Path

import qorcap_pairing as pair
import qorcap_submit as qs

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _token(**over):
    token = {
        "type": "qortime-pairing/v1",
        "pairing_id": "ab" * 16,
        "nonce": "cd" * 16,
        "issued_ms": 1_700_000_000_000,
        "key_id": "ef" * 8,
        "sig": "12" * 64,
    }
    token.update(over)
    return token


def test_upload_names_match_the_vendored_registry():
    compat = json.loads((ROOT / "contracts" / "compat.json").read_text(encoding="utf-8"))
    pinned = tuple(compat["bundle_files"]["meta"])
    # Pending a QorTime compat.json re-copy. Not a contract until that lands.
    pending = ("frames.jsonl", "readers.json")
    assert qs.META_FILES == pinned + pending
    assert qs.upload_names() == qs.META_FILES
    assert pinned[-3:] == ("pairing.json", "device_attest.v0.json", "score_timeline.json")
    assert compat["pairing"]["type"] == "qortime-pairing/v1"
    assert compat["pairing"]["file"] == "pairing.json"
    assert compat["pairing"]["body"].startswith("ignored")
    assert compat["score_timeline"]["schema"] == "qorcap-score-timeline/v0"
    assert compat["score_timeline"]["file"] == "score_timeline.json"


def test_meta_zip_includes_optional_sidecars_only_when_present(tmp_path):
    bundle = tmp_path / "match"
    bundle.mkdir()
    receipt = {"schema": "qor-receipt/v0", "match": {"match_id": "m"}}
    (bundle / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (bundle / "score_timeline.json").write_text('{"schema":"qorcap-score-timeline/v0"}\n', encoding="utf-8")
    (bundle / "device_attest.v0.json").write_text("{}\n", encoding="utf-8")
    (bundle / "pairing.json").write_text("{}\n", encoding="utf-8")
    (bundle / "frames.jsonl").write_text('{"type":"header"}\n', encoding="utf-8")
    (bundle / "readers.json").write_text("{}\n", encoding="utf-8")
    names = set(zipfile.ZipFile(io.BytesIO(qs.meta_zip(bundle))).namelist())
    assert {"receipt.json", "score_timeline.json", "device_attest.v0.json", "pairing.json",
            "frames.jsonl", "readers.json"} <= names

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "receipt.json").write_text("{}", encoding="utf-8")
    bare_names = set(zipfile.ZipFile(io.BytesIO(qs.meta_zip(bare))).namelist())
    assert bare_names == {"receipt.json"}
    for name in ("score_timeline.json", "device_attest.v0.json", "pairing.json", "frames.jsonl", "readers.json"):
        assert name not in bare_names


def test_pairing_code_round_trips_and_file_does_not_touch_the_receipt(tmp_path):
    token = _token(sig="AB" * 64)
    raw = json.dumps(token, separators=(",", ":")).encode()
    code = pair.code_for(raw)
    assert code.startswith("qorpair1.")
    assert "\n" not in code
    assert pair.raw_from_code(code) == raw
    assert pair.parse_raw(pair.raw_from_code(json.dumps(token)))["pairing_id"] == token["pairing_id"]

    bundle = tmp_path / "match"
    bundle.mkdir()
    receipt_bytes = b'{"schema":"qor-receipt/v0","match":{"match_id":"m"}}\n'
    (bundle / "receipt.json").write_bytes(receipt_bytes)
    path = pair.write_pairing(bundle, raw)
    assert path.name == "pairing.json"
    assert path.read_bytes() == raw
    assert (bundle / "receipt.json").read_bytes() == receipt_bytes
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["type"] == "qortime-pairing/v1"
    assert written["sig"] == "AB" * 64
    assert "pairing" not in json.loads(receipt_bytes)
    text = "\n".join(pair.describe(raw))
    assert "does not change VERIFIED" in text
    assert code in text


def test_create_posts_empty_body_and_join_rejects_a_bare_id(tmp_path, capsys):
    token = _token()
    seen = {}

    class Resp:
        def read(self):
            return json.dumps(token).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        return Resp()

    bundle = tmp_path / "match"
    assert pair.apply_pairing(bundle, create=True, code=None, base="https://referee.test/", opener=opener) == 0
    assert seen == {"url": "https://referee.test/v1/pairings", "method": "POST", "body": b"{}"}
    assert json.loads((bundle / "pairing.json").read_text(encoding="utf-8"))["nonce"] == token["nonce"]
    out = capsys.readouterr().out
    assert "qorpair1." in out and "does not change VERIFIED" in out

    other = tmp_path / "other"
    assert pair.apply_pairing(other, create=False, code=token["pairing_id"], base="https://referee.test") == 2
    assert not (other / "pairing.json").exists()
    assert "qorpair1" in capsys.readouterr().out


def test_join_writes_the_shared_token(tmp_path, capsys):
    token = _token()
    bundle = tmp_path / "away"
    (bundle).mkdir()
    receipt = b'{"schema":"qor-receipt/v0"}\n'
    (bundle / "receipt.json").write_bytes(receipt)
    assert pair.apply_pairing(bundle, create=False, code=pair.code_for(json.dumps(token).encode()), base="https://unused") == 0
    assert (bundle / "receipt.json").read_bytes() == receipt
    assert json.loads((bundle / "pairing.json").read_text(encoding="utf-8"))["pairing_id"] == token["pairing_id"]
    capsys.readouterr()
