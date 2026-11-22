"""Frozen contract tests for qorcap-hand-timeline/v0 schemas and golden fixture pack."""
import hashlib
import json
from pathlib import Path
import re

import jsonschema

REPO = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO / "schemas" / "hand_timeline"
FIX_DIR = REPO / "tests" / "fixtures" / "hand_timeline"
GENESIS = "0" * 64
BANNED_RE = re.compile(
    r"(anti[- ]?cheat|attested|proof[- ]of[- ]human|verified\s+skill|proven|proves|approves|improves|disproves|provenance)",
    re.I,
)


def _jcs(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_draft2020_12_schemas_valid_and_note_byte42_r2_l2_labels():
    tl_schema = json.loads((SCHEMA_DIR / "hand_timeline.v0.schema.json").read_text("utf-8"))
    sum_schema = json.loads((SCHEMA_DIR / "hand_timeline_summary.v0.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator.check_schema(tl_schema)
    jsonschema.Draft202012Validator.check_schema(sum_schema)

    fx_desc = tl_schema["$defs"]["in"]["properties"]["trigger_fx_status"]["description"]
    assert "r2_raw = raw[42]" in fx_desc and "l2_raw = raw[43]" in fx_desc
    assert "[r2_raw, l2_raw]" in tl_schema["$defs"]["in"]["properties"]["bytes_42_43_raw"]["description"]


def test_manifest_hashes_and_lf_only():
    manifest = json.loads((FIX_DIR / "MANIFEST.json").read_text("utf-8"))
    entries = manifest["files"]
    assert len(entries) >= 20
    for item in entries:
        p = FIX_DIR / item["path"]
        raw = p.read_bytes()
        assert len(raw) == item["bytes"], item["path"]
        assert _sha256_hex(raw) == item["sha256"], item["path"]
        if item["path"].endswith((".json", ".jsonl")):
            assert b"\r" not in raw, f"CR byte in {item['path']}"


def test_golden_records_validate_against_schema_and_recompute_chains():
    tl_schema = json.loads((SCHEMA_DIR / "hand_timeline.v0.schema.json").read_text("utf-8"))
    sum_schema = json.loads((SCHEMA_DIR / "hand_timeline_summary.v0.schema.json").read_text("utf-8"))
    tl_val = jsonschema.Draft202012Validator(tl_schema)
    sum_val = jsonschema.Draft202012Validator(sum_schema)

    for case in ("187_edge_basic", "189_edge", "189_dualsense_fallback"):
        cdir = FIX_DIR / case
        raw_bytes = (cdir / "hand_raw.v0.bin").read_bytes()
        assert len(raw_bytes) % 80 == 0
        lines = (cdir / "hand_timeline.v0.jsonl").read_text("utf-8").splitlines()
        pending = []
        head = GENESIS
        for i, line in enumerate(lines, 1):
            rec = json.loads(line)
            assert _jcs(rec) == line, f"{case}:{i} not JCS"
            tl_val.validate(rec)
            if rec["type"] == "in":
                raw_idx = rec["counter"] - 1
                raw_rec = raw_bytes[raw_idx * 80 : (raw_idx + 1) * 80]
                usb_rep = raw_rec[16:80]
                if case == "187_edge_basic":
                    assert rec["trigger_fx_status"]["l2_raw"] == 0 and rec["trigger_fx_status"]["r2_raw"] == 0
                else:
                    assert rec["trigger_fx_status"]["r2_raw"] == usb_rep[42]
                    assert rec["trigger_fx_status"]["l2_raw"] == usb_rep[43]
                    assert rec["bytes_42_43_raw"] == [usb_rep[42], usb_rep[43]]
                if case == "189_dualsense_fallback":
                    assert rec["edge_bits"] == "UNMEASURED"
                    assert rec["physical_face"] == "UNMEASURED"
            if rec["type"] != "chunk":
                pending.append(rec)
                continue
            want_sha = _sha256_hex(_jcs(pending).encode("utf-8"))
            assert rec["sha256"] == want_sha
            assert rec["prev"] == head
            head = _sha256_hex(_jcs({"prev": head, "seq": rec["seq"], "sha256": want_sha}).encode("utf-8"))
            assert rec["head"] == head
            off, ln = rec["raw_offset"], rec["raw_len"]
            assert _sha256_hex(raw_bytes[off : off + ln]) == rec["raw_sha256"]
            pending.clear()
        assert not pending

        summary = json.loads((cdir / "hand_timeline_summary.v0.json").read_text("utf-8"))
        sum_val.validate(summary)
        assert summary["head"] == head
        assert summary["sha256"] == _sha256_hex((cdir / "hand_timeline.v0.jsonl").read_bytes())


def test_flag_off_has_zero_hand_keys_and_absent_hand_chunks_pinned():
    off_dir = FIX_DIR / "flag_off_zero_hand_keys"
    assert not (off_dir / "hand_timeline.v0.jsonl").exists()
    assert not (off_dir / "hand_raw.v0.bin").exists()
    forbidden = {"hand_chunk_seq", "hand_head", "hand_timeline_sha256", "hand_timeline_head", "hand_timeline_file", "hand_raw_file"}
    for line in (off_dir / "checkpoints.jsonl").read_text("utf-8").splitlines():
        q = json.loads(line)["qorcap"]
        assert q.get("hand_timeline") is False
        assert not (forbidden & set(q.keys()))

    absent_dir = FIX_DIR / "absent_hand_chunks"
    assert not (absent_dir / "hand_timeline.v0.jsonl").exists()
    manifest = json.loads((FIX_DIR / "MANIFEST.json").read_text("utf-8"))
    assert manifest["fixtures"]["absent_hand_chunks"]["err_contains"] == "hand_timeline.v0.jsonl: not found"


def test_crlf_hash_divergence_and_spec_checks():
    lf_bytes = (FIX_DIR / "189_edge" / "hand_timeline.v0.jsonl").read_bytes()
    crlf_bytes = lf_bytes.replace(b"\n", b"\r\n")
    assert _sha256_hex(lf_bytes) != _sha256_hex(crlf_bytes)

    spec_text = (REPO / "docs" / "HAND_RAW_V0_SPEC.md").read_text("utf-8")
    assert "MI_03" in spec_text and "UNKNOWN" in spec_text
    for path in [
        SCHEMA_DIR / "hand_timeline.v0.schema.json",
        SCHEMA_DIR / "hand_timeline_summary.v0.schema.json",
        REPO / "docs" / "HAND_RAW_V0_SPEC.md",
        FIX_DIR / "MANIFEST.json",
    ]:
        txt = path.read_text("utf-8")
        assert not BANNED_RE.search(txt), f"banned wording in {path}"
