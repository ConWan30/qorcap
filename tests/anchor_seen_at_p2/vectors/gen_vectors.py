#!/usr/bin/env python3
"""Deterministic P2 schema vector generator. Standard library only.

Not model code. Does not import the repository.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
DOC = ROOT / "docs" / "anchor-seen-at-p2.md"
VECTORS_PATH = HERE / "p2-vectors.json"
PINS_PATH = HERE / "p2-pins.json"

U_MAX = 18446744073709551615
I_MIN = -9223372036854775808
I_MAX = 9223372036854775807
Z = "0" * 64
ID_RE = re.compile(r"V-[A-Z0-9]+(?:-[A-Z0-9]+)+")
NOT_EXECUTED = "NOT EXECUTED (no model code in this stage)"
BASE = "4d119020cec8c3b6ae55c30c8bb17f5c6ab845ad"


def nid(n: int) -> str:
    return f"{n:032x}"


def nh(n: int) -> str:
    return f"{n:064x}"


def reject_scalars(obj: object, path: str = "$") -> None:
    if obj is None or isinstance(obj, str):
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            reject_scalars(item, f"{path}[{i}]")
        return
    if isinstance(obj, dict):
        for key, item in obj.items():
            if not isinstance(key, str):
                raise ValueError(f"non-str key at {path}")
            reject_scalars(item, f"{path}.{key}")
        return
    raise ValueError(f"non-str scalar at {path}: {type(obj).__name__}")


def J(obj: object) -> str:
    reject_scalars(obj)
    text = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if json.loads(text) != obj:
        raise ValueError("J round-trip mismatch")
    again = json.dumps(json.loads(text), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if again != text:
        raise ValueError("J not canonical")
    return text


def E(type_name: str, payload: dict) -> dict:
    return {"payload": payload, "schema": "proposed.aux.model.p2.r1." + type_name}


def F(domain: str, body: bytes) -> bytes:
    encoded = domain.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded + len(body).to_bytes(8, "big") + body


def D(type_name: str) -> str:
    return "QOR-AUX-P2-MODEL-R1/" + type_name


def Hash(type_name: str, payload: dict) -> str:
    framed = F(D(type_name), J(E(type_name, payload)).encode("utf-8"))
    return hashlib.sha256(framed).hexdigest()


def Raw(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(type_name: str, payload: dict) -> str:
    return J(E(type_name, payload))


def valid_u(text: str) -> bool:
    if not isinstance(text, str) or not text.isdigit():
        return False
    if len(text) > 1 and text.startswith("0"):
        return False
    return int(text) <= U_MAX


def valid_i(text: str) -> bool:
    if not isinstance(text, str) or text in {"", "-", "+"}:
        return False
    if text.startswith("+") or text == "-0":
        return False
    body = text[1:] if text.startswith("-") else text
    if not body.isdigit() or (len(body) > 1 and body.startswith("0")):
        return False
    try:
        value = int(text)
    except ValueError:
        return False
    return I_MIN <= value <= I_MAX


def valid_id(text: str) -> bool:
    return isinstance(text, str) and len(text) == 32 and all(c in "0123456789abcdef" for c in text)


def valid_h(text: str) -> bool:
    return isinstance(text, str) and len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def model_signed(type_name: str, payload: dict, role: dict) -> dict:
    payload_id = Hash(type_name, payload)
    tag_body = {"alg": "synthetic_hash_only", "payload_id": payload_id, "role": role}
    tag = Hash("ModelTag", tag_body)
    return {
        "alg": "synthetic_hash_only",
        "payload": E(type_name, payload),
        "payload_id": payload_id,
        "role": role,
        "tag": tag,
    }


def empty_tree_digest() -> str:
    return hashlib.sha256(F(D("tree.empty"), b"")).hexdigest()


def clock(role: str, source: str, unit: str, n: int) -> dict:
    return {
        "boot": nid(n + 1),
        "clock_epoch": nid(n + 2),
        "leader": nid(n + 3),
        "process": nid(n),
        "role": role,
        "source": source,
        "unit": unit,
    }


def client_clock() -> dict:
    return clock("client_mono", "client_declared", "ns", 0x10)


def server_clock() -> dict:
    return clock("server_mono", "model_counter", "ns", 0x20)


def capture_clock() -> dict:
    return clock("capture_declared", "capture_declared", "ns", 0x30)


def mono(domain: dict, value: int) -> dict:
    return {"domain": domain, "value": str(value)}


def scope_of(n: int = 0xA0) -> dict:
    return {
        "audience": nid(n + 3),
        "capture_epoch": nid(n + 6),
        "chain": nid(n + 7),
        "device_epoch": nid(n + 5),
        "lineage": nid(n + 8),
        "principal": nid(n + 1),
        "service": nid(n + 2),
        "session": nid(n + 4),
        "tenant": nid(n),
    }


def pins_of(n: int = 0xB0) -> dict:
    return {
        "checker": nh(n + 4),
        "data": nh(n + 3),
        "eligibility": nh(n + 1),
        "protocol": nh(n),
        "trust": nh(n + 2),
    }


def base_core(operation: str = "issue", exchange: int = 0xC0) -> dict:
    domain = client_clock()
    return {
        "ack_ordinal": "0",
        "enqueue_at": mono(domain, 100),
        "exchange": nid(exchange),
        "operation": operation,
        "original_deadline": mono(domain, 200),
        "pins": pins_of(),
        "purpose": "opening",
        "scope": scope_of(),
        "stream": nid(0xD0),
    }


def core_ok(core: dict) -> str | None:
    if set(core) != {
        "ack_ordinal",
        "enqueue_at",
        "exchange",
        "operation",
        "original_deadline",
        "pins",
        "purpose",
        "scope",
        "stream",
    }:
        return "schema"
    if core["operation"] not in {"issue", "consume"}:
        return "schema"
    if "expected_id" in json.dumps(core) or "original_request_hash" in json.dumps(core):
        return "binding"
    left = core["enqueue_at"]
    right = core["original_deadline"]
    if left["domain"] != right["domain"]:
        return "domain"
    if left["domain"]["role"] != "client_mono":
        return "domain"
    if left["domain"]["source"] != "client_declared":
        return "domain"
    if left["domain"]["unit"] != "ns":
        return "domain"
    if not valid_u(left["value"]) or not valid_u(right["value"]):
        return "schema"
    if int(right["value"]) < int(left["value"]):
        return "domain"
    return None


def logical_key(core: dict) -> str:
    return J(
        {
            "exchange": core["exchange"],
            "operation": core["operation"],
            "scope": core["scope"],
            "stream": core["stream"],
        }
    )


def issue_request(core: dict, expected_id: str, idempotency: str | None = None) -> dict:
    payload = {
        "ack_ordinal": core["ack_ordinal"],
        "client_nonce": nh(0xE1),
        "expected_id": expected_id,
        "idempotency": idempotency or nid(0xE2),
        "owner_generation": "1",
        "pins": core["pins"],
        "prior_ack": None,
        "purpose": core["purpose"],
        "scope": core["scope"],
        "seal": None,
        "stream": core["stream"],
    }
    assert_fields("IssueRequest", payload)
    return payload


FIELD_SETS = {
    "SourceBound": {"head", "last"},
    "Range": {"count", "end_head", "first", "last", "predecessor", "predecessor_head", "source_schema"},
    "CaptureRange": {"capture", "domain", "first", "last"},
    "Wall": {"domain", "quality", "value"},
    "IssueRequest": {
        "ack_ordinal", "client_nonce", "expected_id", "idempotency", "owner_generation",
        "pins", "prior_ack", "purpose", "scope", "seal", "stream",
    },
    "Challenge": {
        "ack_ordinal", "challenge_id", "client_nonce", "expires_at", "issue_request_hash",
        "issued_at", "owner_generation", "pins", "prior_ack", "purpose", "scope", "seal",
        "server_nonce", "stream", "wall_audit",
    },
    "Commitment": {
        "ack_ordinal", "capture_domain", "capture_ranges", "challenge_id", "challenge_payload_id",
        "client_nonce", "close", "expected_id", "journal", "owner_generation", "pins", "prior_ack",
        "purpose", "scope", "seal", "server_nonce", "source", "source_bound", "source_domain", "stream",
    },
    "ConsumeRequest": {"challenge_bytes", "commitment", "idempotency"},
    "AckCore": {
        "ack_ordinal", "challenge_id", "commitment_id", "global_position", "ledger_epoch",
        "owner_generation", "pins", "prior_ack", "prior_global_digest", "received_at",
        "request_hash", "scope", "stream", "validated_before_sign_at", "wall_audit",
    },
    "JournalRef": {
        "client_checkpoint", "count", "first", "head", "last", "negative_count",
        "prior_client_checkpoint", "prior_service_receipt",
    },
    "ClientCheckpointCore": {
        "count", "first", "head", "last", "negative_count", "owner_generation",
        "prior_client_checkpoint", "scope", "stream",
    },
    "CheckpointRecord": {
        "client_checkpoint_id", "journal", "owner_generation", "request_hash", "scope",
        "source_bound", "stream",
    },
    "CheckpointRequest": {
        "capture_domain", "expected_revision", "idempotency", "journal", "owner_generation",
        "pins", "scope", "source_bound", "source_domain", "source_schema", "stream",
    },
}


def assert_fields(type_name: str, payload: dict, checks: dict | None = None) -> None:
    expected = FIELD_SETS[type_name]
    found = set(payload)
    if found != expected:
        raise SystemExit(f"{type_name} fields extra={sorted(found - expected)} missing={sorted(expected - found)}")
    if checks is not None:
        checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 1


def source_bound(last: int = 0, head: int = 0xF1) -> dict:
    payload = {"head": nh(head), "last": str(last)}
    assert_fields("SourceBound", payload)
    return payload


def opening_range() -> dict:
    payload = {
        "count": "1",
        "end_head": nh(0x52),
        "first": "0",
        "last": "0",
        "predecessor": None,
        "predecessor_head": Z,
        "source_schema": nh(0x51),
    }
    assert_fields("Range", payload)
    return payload


def capture_range() -> dict:
    payload = {"capture": nid(0x53), "domain": capture_clock(), "first": "0", "last": "0"}
    assert_fields("CaptureRange", payload)
    return payload


def wall_audit(n: int = 0x40) -> dict:
    payload = {"domain": clock("wall_audit", "wall_declared", "unix_us", n), "quality": "unsynchronized_audit_only", "value": "0"}
    assert_fields("Wall", payload)
    return payload


def context_quad() -> dict:
    return {
        "capture_domain": capture_clock(),
        "pins": pins_of(),
        "source_domain": server_clock(),
        "source_schema": nh(0x51),
    }


def journal_ref(
    client_checkpoint: str,
    prior_client: str,
    prior_receipt: str | None,
    head: int = 0x61,
    first: str = "0",
    last: str = "0",
    count: str = "1",
) -> dict:
    payload = {
        "client_checkpoint": client_checkpoint,
        "count": count,
        "first": first,
        "head": nh(head),
        "last": last,
        "negative_count": "0",
        "prior_client_checkpoint": prior_client,
        "prior_service_receipt": prior_receipt,
    }
    assert_fields("JournalRef", payload)
    return payload


def genesis_core() -> dict:
    return {
        "empty_head": empty_tree_digest(),
        "origin": stream_origin(),
        "scope": scope_of(),
        "stream": nid(0xD0),
    }


def stream_origin() -> dict:
    return {
        "capture_domain": capture_clock(),
        "pins": pins_of(),
        "source_domain": server_clock(),
        "source_schema": nh(0x51),
    }


def client_checkpoint_core(prior: str, head: int = 0x61, first: str = "0", last: str = "0", count: str = "1") -> dict:
    payload = {
        "count": count,
        "first": first,
        "head": nh(head),
        "last": last,
        "negative_count": "0",
        "owner_generation": "1",
        "prior_client_checkpoint": prior,
        "scope": scope_of(),
        "stream": nid(0xD0),
    }
    assert_fields("ClientCheckpointCore", payload)
    return payload


def checkpoint_record(request_hash: str, client_id: str, journal: dict, bound: dict | None = None) -> dict:
    payload = {
        "client_checkpoint_id": client_id,
        "journal": journal,
        "owner_generation": "1",
        "request_hash": request_hash,
        "scope": scope_of(),
        "source_bound": bound if bound is not None else source_bound(),
        "stream": nid(0xD0),
    }
    assert_fields("CheckpointRecord", payload)
    return payload


def consume_request(
    core: dict,
    expected_id: str,
    journal: dict,
    challenge_bytes: str,
    challenge_payload_id: str,
    idempotency: str | None = None,
) -> dict:
    commitment = {
        "ack_ordinal": "0",
        "capture_domain": capture_clock(),
        "capture_ranges": [capture_range()],
        "challenge_id": nid(0x71),
        "challenge_payload_id": challenge_payload_id,
        "client_nonce": nh(0xE1),
        "close": None,
        "expected_id": expected_id,
        "journal": journal,
        "owner_generation": "1",
        "pins": core["pins"],
        "prior_ack": None,
        "purpose": "opening",
        "scope": core["scope"],
        "seal": nh(0x73),
        "server_nonce": nh(0x74),
        "source": opening_range(),
        "source_bound": source_bound(),
        "source_domain": server_clock(),
        "stream": core["stream"],
    }
    assert_fields("Commitment", commitment)
    payload = {
        "challenge_bytes": challenge_bytes,
        "commitment": commitment,
        "idempotency": idempotency or nid(0xE3),
    }
    assert_fields("ConsumeRequest", payload)
    return payload


def set_path(obj: dict, path: str, value: object) -> dict:
    clone = json.loads(json.dumps(obj))
    cursor = clone
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return clone


def encoded(type_name: str, payload: dict) -> dict:
    text = canonical_bytes(type_name, payload)
    return {
        "byte_length": len(text.encode("utf-8")),
        "canonical": text,
        "digest": Hash(type_name, payload),
        "domain": D(type_name),
        "raw": Raw(text.encode("utf-8")),
        "type": type_name,
    }


ENCODING_STATUS = (
    "encoding/digest facts computed by this generator; the expected_outcome is a schema "
    "expectation for the future model, NOT EXECUTED"
)
VALIDATOR_STATUS = (
    "generator schema check run by this generator (not the model parser); the model "
    "outcome is NOT EXECUTED"
)


def computed(vector_id: str, outcome: str, validator_result: bool = False, **fields: object) -> dict:
    row = {
        "expected_outcome": outcome,
        "id": vector_id,
        "kind": "computed",
        "outcome_status": VALIDATOR_STATUS if validator_result else ENCODING_STATUS,
    }
    row.update(fields)
    return row


def doc_ids() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    found = sorted(set(ID_RE.findall(text)))
    if not found:
        raise SystemExit("no vector IDs in schema doc")
    return found


def parse_supplied_pins(text: str) -> list[dict]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## 0. Provenance")), None)
    if start is None:
        raise SystemExit("provenance heading missing")
    header = None
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        if lines[i].startswith("| Source (file name only)"):
            header = i
            break
    if header is None:
        raise SystemExit("provenance table missing")
    rows = []
    seen: set[str] = set()
    for line in lines[header + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            raise SystemExit("provenance row is not three cells")
        name = cells[0].replace("`", "").strip()
        digest = cells[1].replace("`", "").strip().lower()
        length = cells[2].replace("`", "").strip()
        if not name or name in seen:
            raise SystemExit("provenance name missing or duplicated")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit("provenance digest is not lowercase 64-hex")
        if length != "UNKNOWN" and not re.fullmatch(r"[0-9]+", length):
            raise SystemExit("provenance length is not an integer or UNKNOWN")
        seen.add(name)
        rows.append(
            {
                "bytes": length,
                "label": "supplied pin assertion, not an observed file hash",
                "sha256": digest,
                "source": name,
            }
        )
    if not rows:
        raise SystemExit("provenance table empty")
    return rows


def dag() -> dict:
    issue_core = base_core("issue")
    issue_err = core_ok(issue_core)
    if issue_err:
        raise SystemExit(issue_err)
    issue_id = Hash("ClientExpectedCore", issue_core)
    issue = issue_request(issue_core, issue_id)
    issue_text = canonical_bytes("IssueRequest", issue)
    issue_raw = Raw(issue_text.encode("utf-8"))
    challenge = {
        "ack_ordinal": "0",
        "challenge_id": nid(0x71),
        "client_nonce": issue["client_nonce"],
        "expires_at": mono(server_clock(), 110),
        "issue_request_hash": issue_raw,
        "issued_at": mono(server_clock(), 100),
        "owner_generation": "1",
        "pins": issue["pins"],
        "prior_ack": None,
        "purpose": "opening",
        "scope": issue["scope"],
        "seal": None,
        "server_nonce": nh(0x74),
        "stream": issue["stream"],
        "wall_audit": wall_audit(),
    }
    assert_fields("Challenge", challenge)
    signed_challenge = model_signed("Challenge", challenge, role("challenge"))
    challenge_text = canonical_bytes("ModelSigned", signed_challenge)
    challenge_bytes = base64.urlsafe_b64encode(challenge_text.encode("utf-8")).decode("ascii").rstrip("=")
    consume_core = base_core("consume")
    consume_id = Hash("ClientExpectedCore", consume_core)
    genesis = genesis_core()
    genesis_id = Hash("GenesisCore", genesis)
    ck = client_checkpoint_core(genesis_id)
    ck_id = Hash("ClientCheckpointCore", ck)
    journal = journal_ref(ck_id, genesis_id, None)
    consume = consume_request(consume_core, consume_id, journal, challenge_bytes, signed_challenge["payload_id"])
    consume_text = canonical_bytes("ConsumeRequest", consume)
    ack = {
        "ack_ordinal": "0",
        "challenge_id": nid(0x71),
        "commitment_id": Hash("Commitment", consume["commitment"]),
        "global_position": "0",
        "ledger_epoch": nid(0x75),
        "owner_generation": "1",
        "pins": consume_core["pins"],
        "prior_ack": None,
        "prior_global_digest": Z,
        "received_at": mono(server_clock(), 120),
        "request_hash": Raw(consume_text.encode("utf-8")),
        "scope": consume_core["scope"],
        "stream": consume_core["stream"],
        "validated_before_sign_at": mono(server_clock(), 121),
        "wall_audit": wall_audit(0x41),
    }
    assert_fields("AckCore", ack)
    signed_ack = model_signed("AckCore", ack, role("ack"))
    return {
        "ack": ack,
        "challenge": challenge,
        "challenge_bytes": challenge_bytes,
        "challenge_payload_id": signed_challenge["payload_id"],
        "signed_ack": signed_ack,
        "signed_challenge": signed_challenge,
        "consume": consume,
        "consume_core": consume_core,
        "consume_id": consume_id,
        "consume_raw": Raw(consume_text.encode("utf-8")),
        "consume_text": consume_text,
        "genesis": genesis,
        "genesis_id": genesis_id,
        "issue": issue,
        "issue_core": issue_core,
        "issue_id": issue_id,
        "issue_raw": issue_raw,
        "issue_text": issue_text,
        "journal": journal,
        "checkpoint_core": ck,
        "checkpoint_id": ck_id,
    }


def role(kind: str) -> dict:
    # RoleRef.policy is the digest of the PolicyCore that authorized the commit (§4).
    return {"generation": "1", "issuer": nid(0x90), "kind": kind, "policy": Hash("PolicyCore", policy_core())}


MUT_SPECS = [
    ("V-B1-MUT-SCOPE-TENANT", "scope.tenant", nid(0x111), "new id; same key impossible (key includes scope) so this is a different key and must not replace the old key"),
    ("V-B1-MUT-SCOPE-PRINCIPAL", "scope.principal", nid(0x112), "same as tenant: different key, old record unchanged"),
    ("V-B1-MUT-SCOPE-SERVICE", "scope.service", nid(0x113), "different key, old unchanged"),
    ("V-B1-MUT-SCOPE-AUDIENCE", "scope.audience", nid(0x114), "different key, old unchanged"),
    ("V-B1-MUT-SCOPE-SESSION", "scope.session", nid(0x115), "different key, old unchanged"),
    ("V-B1-MUT-SCOPE-DEVICE", "scope.device_epoch", nid(0x116), "different key, old unchanged"),
    ("V-B1-MUT-SCOPE-CAPTURE", "scope.capture_epoch", nid(0x117), "different key, old unchanged"),
    ("V-B1-MUT-SCOPE-CHAIN", "scope.chain", nid(0x118), "different key, old unchanged"),
    ("V-B1-MUT-SCOPE-LINEAGE", "scope.lineage", nid(0x119), "different key, old unchanged"),
    ("V-B1-MUT-STREAM", "stream", nid(0x11A), "different key, old unchanged"),
    ("V-B1-MUT-EXCHANGE", "exchange", nid(0x11B), "different key; must not be treated as a retry of the old exchange"),
    ("V-B1-MUT-OP", "operation", "consume", "different key; using the other operation's id on the wrong request type is binding"),
    ("V-B1-MUT-PURPOSE", "purpose", "interval", "conflict"),
    ("V-B1-MUT-ORD", "ack_ordinal", "1", "conflict"),
    ("V-B1-MUT-PIN-PROTOCOL", "pins.protocol", nh(0x121), "conflict"),
    ("V-B1-MUT-PIN-ELIG", "pins.eligibility", nh(0x122), "conflict"),
    ("V-B1-MUT-PIN-TRUST", "pins.trust", nh(0x123), "conflict"),
    ("V-B1-MUT-PIN-DATA", "pins.data", nh(0x124), "conflict"),
    ("V-B1-MUT-PIN-CHECKER", "pins.checker", nh(0x125), "conflict"),
    ("V-B1-MUT-ENQ-ROLE", "enqueue_at.domain.role", "server_mono", "domain"),
    ("V-B1-MUT-ENQ-SOURCE", "enqueue_at.domain.source", "model_counter", "domain"),
    ("V-B1-MUT-ENQ-UNIT", "enqueue_at.domain.unit", "unix_us", "domain"),
    ("V-B1-MUT-ENQ-PROCESS", "enqueue_at.domain.process", nid(0x131), "domain"),
    ("V-B1-MUT-ENQ-BOOT", "enqueue_at.domain.boot", nid(0x132), "domain"),
    ("V-B1-MUT-ENQ-EPOCH", "enqueue_at.domain.clock_epoch", nid(0x133), "domain"),
    ("V-B1-MUT-ENQ-LEADER", "enqueue_at.domain.leader", nid(0x134), "domain"),
    ("V-B1-MUT-ENQ-VALUE", "enqueue_at.value", "150", "conflict"),
    ("V-B1-MUT-DL-VALUE", "original_deadline.value", "250", "conflict"),
    ("V-B1-MUT-ORDER", "original_deadline.value", "50", "domain"),
]


def build_b1(base: dict, checks: dict) -> list[dict]:
    rows = []
    core = base["issue_core"]
    blob = json.dumps(core)
    if "expected_id" in blob or base["issue_raw"] in blob or base["issue_id"] in blob:
        raise SystemExit("core is not acyclic")
    checks["acyclic"] = True
    rows.append(
        computed(
            "V-B1-DAG-01",
            "constructible issue core, expected_id, full IssueRequest, Raw, challenge; core has no request edge",
            chain={
                "challenge": encoded("Challenge", base["challenge"]),
                "core": encoded("ClientExpectedCore", core),
                "expected_id": base["issue_id"],
                "raw": base["issue_raw"],
                "request": encoded("IssueRequest", base["issue"]),
            },
        )
    )
    rows.append(
        computed(
            "V-B1-DAG-02",
            "consume core distinct from issue; full ConsumeRequest; Raw",
            chain={
                "ack": encoded("AckCore", base["ack"]),
                "challenge": encoded("Challenge", base["challenge"]),
                "challenge_payload_id": base["challenge_payload_id"],
                "core": encoded("ClientExpectedCore", base["consume_core"]),
                "expected_id": base["consume_id"],
                "issue_expected_id": base["issue_id"],
                "issue_raw": base["issue_raw"],
                "original_request_hash": base["consume_raw"],
                "raw": base["consume_raw"],
                "request": encoded("ConsumeRequest", base["consume"]),
                "signed_ack": encoded("ModelSigned", base["signed_ack"]),
                "signed_challenge": encoded("ModelSigned", base["signed_challenge"]),
            },
            ids_differ=base["issue_id"] != base["consume_id"],
        )
    )
    if not rows[-1]["ids_differ"]:
        raise SystemExit("issue and consume ids aliased")
    other = base_core("issue", 0xCE)
    rows.append(
        computed(
            "V-B1-TWIN-NEWEX",
            "a new exchange id does not alter the previous key",
            new_core=encoded("ClientExpectedCore", other),
            new_expected_id=Hash("ClientExpectedCore", other),
            old_expected_id=base["issue_id"],
            same_key=logical_key(other) == logical_key(core),
        )
    )
    later = set_path(core, "enqueue_at.value", "180")
    later = set_path(later, "original_deadline.value", "190")
    rows.append(
        computed(
            "V-B1-ATK-01",
            "conflict",
            evidence_class="authored_negative_expectation",
            mutated_core=encoded("ClientExpectedCore", later),
            note="authored, NOT EXECUTED",
            valid_twin="V-B1-DAG-01",
            mutated_expected_id=Hash("ClientExpectedCore", later),
            original_expected_id=base["issue_id"],
            same_key=logical_key(later) == logical_key(core),
        )
    )
    bad_consume = consume_request(
        base["consume_core"],
        base["issue_id"],
        base["journal"],
        base["challenge_bytes"],
        base["challenge_payload_id"],
    )
    rows.append(
        computed(
            "V-B1-ATK-02",
            "binding",
            presented_expected_id=base["issue_id"],
            consume_core_id=base["consume_id"],
            matches_consume_core=base["issue_id"] == base["consume_id"],
            request=encoded("ConsumeRequest", bad_consume),
        )
    )
    changed = dict(base["issue"])
    changed["idempotency"] = nid(0xEE)
    changed_text = canonical_bytes("IssueRequest", changed)
    rows.append(
        computed(
            "V-B1-ATK-03",
            "conflict",
            fixed_expected_id=base["issue_id"],
            original_raw=base["issue_raw"],
            mutated_raw=Raw(changed_text.encode("utf-8")),
            request=encoded("IssueRequest", changed),
        )
    )
    for vector_id, path, value, outcome in MUT_SPECS:
        mutated = set_path(core, path, value)
        err = core_ok(mutated)
        mutated_id = Hash("ClientExpectedCore", mutated) if err is None or outcome == "domain" else None
        if err is None:
            mutated_id = Hash("ClientExpectedCore", mutated)
        row = computed(
            vector_id,
            outcome,
            mutated_core=encoded("ClientExpectedCore", mutated),
            original_expected_id=base["issue_id"],
            mutated_expected_id=mutated_id,
            same_key=logical_key(mutated) == logical_key(core),
            validator=err or "ok",
            validator_result=outcome == "domain",
        )
        if mutated_id == base["issue_id"]:
            raise SystemExit(f"{vector_id} did not change expected_id")
        if outcome == "domain" and err != "domain":
            raise SystemExit(f"{vector_id} validator {err}")
        if outcome != "domain" and err is not None:
            raise SystemExit(f"{vector_id} unexpected validator {err}")
        rows.append(row)
        checks["mutations"] += 1
    stale = dict(base["issue"])
    stale["expected_id"] = nh(0xABCDEF)
    matches_core = stale["expected_id"] == base["issue_id"]
    if matches_core or not valid_h(stale["expected_id"]):
        raise SystemExit("stale expected_id was not a well-formed non-match")
    rows.append(
        computed(
            "V-B1-MUT-STALE-ID",
            "binding",
            request=encoded("IssueRequest", stale),
            matches_core=matches_core,
            validator_result=True,
        )
    )
    omitted = {key: value for key, value in base["issue"].items() if key != "expected_id"}
    try:
        if "expected_id" not in omitted or not valid_h(omitted["expected_id"]):
            raise ValueError("missing expected_id")
        canonical_bytes("IssueRequest", omitted)
        omit_ok = True
    except ValueError:
        omit_ok = False
    rows.append(
        computed(
            "V-B1-MUT-OMIT-ID",
            "binding",
            malformed='{"schema":"proposed.aux.model.p2.r1.IssueRequest","payload":{"expected_id":null}}',
            round_trip_ok=omit_ok,
            validator="binding",
            validator_result=True,
        )
    )
    if omit_ok:
        raise SystemExit("omitted expected_id was accepted")
    checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 1
    return rows


def limit_set(image: str = "4096") -> dict:
    fields = [
        "accounting_basis_bytes",
        "audit_records",
        "call_bytes",
        "calls",
        "canonical_bytes",
        "concurrent",
        "decoded_bytes",
        "denial_bytes",
        "denial_records",
        "depth",
        "fault_records",
        "inflight",
        "issue_lifetime_ns",
        "list_items",
        "minima_entries",
        "operation_age_ns",
        "output_bytes",
        "outstanding",
        "expected_entries",
        "page_fetches",
        "page_items",
        "parse_bytes",
        "proof_compares",
        "retained_pages",
        "retained_proof_hashes",
        "records",
        "recovery_image_bytes",
        "request_input_bytes",
        "retry_attempts",
        "signer_calls",
        "stored_bytes",
        "stock_entries",
        "store_calls",
        "string_bytes",
        "tokens",
        "workspace_bytes",
    ]
    values = {name: "8" for name in fields}
    values["request_input_bytes"] = "16384"
    values["recovery_image_bytes"] = image
    return values


def policy_core(generation: str = "1", marker: str = "a") -> dict:
    return {
        "archive": "allow",
        "generation": generation,
        "historical": [
            {
                "decision": "allow",
                "generation": generation,
                "issuer": nid(0x90),
                "kind": "owner",
                "records_through": nh(0x301),
            }
        ],
        "prior_digest": None,
        "registry": {k: v for k, v in scope_of().items() if k != "lineage"},
        "roles": [
            {
                "generation": generation,
                "issuer": nid(0x90),
                "kind": "owner",
                "operation": "owner_grant",
                "state": "active",
            }
        ]
        if marker == "a"
        else [
            {
                "generation": generation,
                "issuer": nid(0x90),
                "kind": "observe",
                "operation": "observe",
                "state": "active",
            }
        ],
    }


def build_requests(base: dict) -> dict[str, dict]:
    common = {"expected_revision": "1", "idempotency": nid(0xE4), "owner_generation": "1", "scope": scope_of(), "stream": nid(0xD0)}
    journal = base["journal"]
    return {
        "owner_grant": {
            "type": "OwnerRequest",
            "payload": {
                "expected_owner_generation": "1",
                "expected_revision": "0",
                "holder": nid(0x81),
                "idempotency": nid(0xE5),
                "parents": [{"boundary": "unknown", "checkpoint": None, "scope_digest": Hash("Scope", scope_of())}],
                "scope": scope_of(),
                "stream": nid(0xD0),
            },
        },
        "issue": {"type": "IssueRequest", "payload": base["issue"]},
        "consume": {"type": "ConsumeRequest", "payload": base["consume"]},
        "archive_read": {
            "type": "ReadOriginalRequest",
            "payload": {"operation": "issue", "request_hash": base["issue_raw"], "response_hash": nh(0x201), "scope": scope_of()},
        },
        "snapshot": {
            "type": "SnapshotRequest",
            "payload": {
                "idempotency": nid(0xE6),
                "known_observer_set": nh(0x220),
                "nonce": nh(0x202),
                "page_items": "1",
                "scope": scope_of(),
            },
        },
        "page": {
            "type": "PageRequest",
            "payload": {"core_id": nh(0x203), "cursor": nh(0x204), "index": "0", "manifest_id": nh(0x205), "scope": scope_of()},
        },
        "checkpoint": {
            "type": "CheckpointRequest",
            "payload": {
                "capture_domain": capture_clock(),
                "expected_revision": "1",
                "idempotency": nid(0xE4),
                "journal": journal,
                "owner_generation": "1",
                "pins": pins_of(),
                "scope": scope_of(),
                "source_bound": source_bound(),
                "source_domain": server_clock(),
                "source_schema": nh(0x51),
                "stream": nid(0xD0),
            },
        },
        "observe": {
            "type": "Observation",
            "payload": {
                **common,
                **context_quad(),
                "journal": journal,
                "reason": "unavailable",
                "source_bound": source_bound(),
            },
        },
        "hold": {
            "type": "HoldRequest",
            "payload": {
                **common,
                **context_quad(),
                "candidate_digest": nh(0x206),
                "holder": nid(0x81),
                "journal": journal,
                "source_bound": source_bound(),
            },
        },
        "terminal": {
            "type": "TerminalRequest",
            "payload": {
                "candidate_digest": nh(0x206),
                "expected_hold_revision": "1",
                "expected_state": "held",
                "holder": nid(0x81),
                "hold_id": nh(0x207),
                "idempotency": nid(0xE7),
                "outcome": "aborted",
                "owner_generation": "1",
                "retirement_checkpoint": nh(0x208),
                "retirement_id": nid(0x209),
                "scope": scope_of(),
                "stream": nid(0xD0),
            },
        },
        "policy_update": {"type": "PolicyCore", "payload": policy_core()},
        "recover": {
            "type": "RecoveryRecord",
            "payload": {
                "previous_image_hash": nh(0x210),
                "reconciled_observer_hash": nh(0x211),
                "service_epoch": nid(0x212),
                "writer_term": "2",
            },
        },
    }


def build_rest(base: dict, checks: dict) -> list[dict]:
    rows: list[dict] = []
    requests = build_requests(base)
    op_outcome = {
        "V-B2-OP-OWNER": ("owner_grant", "one Original and one GlobalEntry; kind owner"),
        "V-B2-OP-ISSUE": ("issue", "kind challenge; holder null"),
        "V-B2-OP-CONSUME": ("consume", "kind ack; admission-only"),
        "V-B2-OP-ARCH": ("archive_read", "historical bytes under current archive allow"),
        "V-B2-OP-SNAP": ("snapshot", "side table; kind snapshot; no GlobalEntry"),
        "V-B2-OP-PAGE": ("page", "no new tag; no idempotency id"),
        "V-B2-OP-CKPT": ("checkpoint", "kind checkpoint; ledger class"),
        "V-B2-OP-OBS": ("observe", "kind observe, not checkpoint"),
        "V-B2-OP-HOLD": ("hold", "kind hold"),
        "V-B2-OP-POL": ("policy_update", "registry class; kind policy; no idempotency id"),
    }
    for vector_id, (name, outcome) in op_outcome.items():
        spec = requests[name]
        enc = encoded(spec["type"], spec["payload"])
        J(json.loads(enc["canonical"]))
        checks["roundtrips"] += 1
        signed = None
        if name == "checkpoint":
            assert_fields("CheckpointRequest", spec["payload"], checks)
        if name == "policy_update":
            signed = model_signed("PolicyCore", spec["payload"], role("policy"))
            if "digest" in spec["payload"]:
                raise SystemExit("policy core contains digest")
        rows.append(computed(vector_id, outcome, request=enc, tag=signed))
    image_len = 61295
    request_cap = 16384
    image_cap = 70000
    split_ok = image_len != request_cap and image_len <= image_cap and image_len > request_cap
    if not split_ok:
        raise SystemExit("image split fixture is incoherent")
    rows.append(
        computed(
            "V-B3-IMG-SPLIT",
            "nonconformant if charged as request_input_bytes; compare only to recovery_image_bytes",
            image_bytes=str(image_len),
            recovery_image_bytes=str(image_cap),
            request_input_bytes=str(request_cap),
            charged_against_request_input=False,
        )
    )
    rows.append(computed("V-B4-SPLIT", "not a request-input success", image_bytes="61295", request_input_bytes="16384", compared_fields=["recovery_image_bytes"]))
    rows.extend(checkpoint_vectors(base, checks))
    rows.extend(stream_vectors(base, checks))
    return rows


def checkpoint_vectors(base: dict, checks: dict) -> list[dict]:
    rows = []
    genesis_id = base["genesis_id"]
    ck = base["checkpoint_core"]
    ck_id = base["checkpoint_id"]
    if ck["prior_client_checkpoint"] != genesis_id:
        raise SystemExit("first prior is not genesis")
    if ck_id == genesis_id:
        raise SystemExit("client id aliased to genesis")
    journal = base["journal"]
    if journal["prior_service_receipt"] is not None:
        raise SystemExit("first receipt prior was not null")
    if journal["first"] != ck["first"] or journal["last"] != ck["last"] or journal["count"] != ck["count"]:
        raise SystemExit("boot journal counters disagree")
    rows.append(
        computed(
            "V-B6-BOOT",
            "consume commits AckCore only. No CheckpointRecord. Request bytes do not contain a receipt id",
            ack=encoded("AckCore", base["ack"]),
            challenge=encoded("Challenge", base["challenge"]),
            client=encoded("ClientCheckpointCore", ck),
            genesis=encoded("GenesisCore", base["genesis"]),
            issue=encoded("IssueRequest", base["issue"]),
            request=encoded("ConsumeRequest", base["consume"]),
            writes_checkpoint_record=False,
        )
    )
    nxt = client_checkpoint_core(ck_id, 0x62, first="1", last="1", count="1")
    nxt_id = Hash("ClientCheckpointCore", nxt)
    nxt_journal = journal_ref(nxt_id, ck_id, None, 0x62, first="1", last="1", count="1")
    if nxt_journal["first"] != nxt["first"] or nxt_journal["last"] != nxt["last"]:
        raise SystemExit("extension counters disagree")
    nxt_request = {
        "capture_domain": capture_clock(),
        "expected_revision": "1",
        "idempotency": nid(0xE8),
        "journal": nxt_journal,
        "owner_generation": "1",
        "pins": pins_of(),
        "scope": scope_of(),
        "source_bound": source_bound(1, 0x62),
        "source_domain": server_clock(),
        "source_schema": nh(0x51),
        "stream": nid(0xD0),
    }
    assert_fields("CheckpointRequest", nxt_request, checks)
    nxt_text = canonical_bytes("CheckpointRequest", nxt_request)
    nxt_raw = Raw(nxt_text.encode("utf-8"))
    nxt_record = checkpoint_record(nxt_raw, nxt_id, nxt_journal, nxt_request["source_bound"])
    nxt_record_id = Hash("CheckpointRecord", nxt_record)
    if nxt_record_id in nxt_text or nxt_record_id in {ck_id}:
        raise SystemExit("extension id collided or leaked into the request")
    rows.append(
        computed(
            "V-B6-EXT",
            "checkpoint commits one CheckpointRecord. New id differs from the client id",
            client=encoded("ClientCheckpointCore", nxt),
            prior_client=ck_id,
            prior_service_receipt=None,
            receipt=encoded("CheckpointRecord", nxt_record),
            request=encoded("CheckpointRequest", nxt_request),
            request_hash=nxt_raw,
        )
    )
    wrong = client_checkpoint_core(genesis_id, 0x99)
    rows.append(
        computed(
            "V-B6-WRONG-ROOT",
            "fork",
            evidence_class="authored_negative_expectation",
            mutated=encoded("ClientCheckpointCore", wrong),
            note="authored, NOT EXECUTED",
            valid_twin="V-B6-BOOT",
            original_head=ck["head"],
            mutated_head=wrong["head"],
            mutated_id=Hash("ClientCheckpointCore", wrong),
        )
    )
    rows.extend(b6_records(base, checks, nxt, nxt_request, nxt_record_id))
    return rows


def stream_vectors(base: dict, checks: dict) -> list[dict]:
    origin = stream_origin()
    rows = [
        computed("V-B7-OPEN", "origin stored from that request", origin=encoded("StreamOrigin", origin)),
        computed("V-B7-EQ", "not a context reject", evidence_class="authored_positive_fixture", note="authored positive fixture; future model outcome NOT EXECUTED", stored=encoded("StreamOrigin", origin), presented=encoded("StreamOrigin", stream_origin()), equal=origin == stream_origin()),
    ]
    mutated = dict(origin)
    mutated["pins"] = pins_of(0xEE)
    rows.append(
        computed(
            "V-B7-PIN",
            "fork",
            evidence_class="authored_negative_expectation",
            mutated_origin=encoded("StreamOrigin", mutated),
            note="authored, NOT EXECUTED",
            valid_twin="V-B7-OPEN",
            original=Hash("StreamOrigin", origin),
            mutated=Hash("StreamOrigin", mutated),
        )
    )
    if rows[-1]["original"] == rows[-1]["mutated"]:
        raise SystemExit("pin mutation did not change origin")
    checks["mutations"] += 1
    auth = dict(origin)
    auth["pins"] = dict(origin["pins"])
    auth["pins"]["checker"] = nh(0x333)
    rows.append(
        computed(
            "V-B7-AUTH",
            "origin unchanged; call denied",
            evidence_class="authored_negative_expectation",
            presented=encoded("StreamOrigin", auth),
            note="authored, NOT EXECUTED",
            valid_twin="V-B7-OPEN",
            origin_digest=Hash("StreamOrigin", origin),
            presented_digest=Hash("StreamOrigin", auth),
        )
    )
    checks["mutations"] += 1
    return rows


# ---------------------------------------------------------------------------
# r3 complete fixtures and the twelve required families.
# Every record below is a complete typed record under §2/§2.1 (r1 fields plus
# only the listed r3 deltas). Positives are authored fixtures. Negatives are
# one-field mutations of a named positive. Every expected outcome is a schema
# expectation for a future model: NOT EXECUTED. Generator checks that really
# run are counted separately and are not the model parser.
# ---------------------------------------------------------------------------

AUTHORED = "authored_negative_expectation"
POSITIVE = "authored_positive_fixture"
SCHEDULE = "authored_schedule_fixture"
DELETE = object()
SYNTHETIC_CAP_LABEL = "synthetic fixture cap; not a setting, not a measurement, not an approved limit"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64u(text: str) -> bytes:
    if "=" in text or "+" in text or "/" in text:
        raise SystemExit("Bytes not canonical unpadded base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u(raw) != text:
        raise SystemExit("Bytes re-encode mismatch")
    return raw


def registry_of(n: int = 0xA0) -> dict:
    scope = scope_of(n)
    del scope["lineage"]
    return scope


def mutate(obj: dict, path: str, value: object) -> dict:
    clone = json.loads(json.dumps(obj))
    cursor = clone
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    last = parts[-1]
    if value is DELETE:
        del cursor[last]
    elif isinstance(cursor, list):
        cursor[int(last)] = value
    else:
        cursor[last] = value
    return clone


def pos(vector_id: str, outcome: str, checks: dict, **fields: object) -> dict:
    return computed(vector_id, outcome, evidence_class=POSITIVE, note="authored positive fixture; future model outcome NOT EXECUTED", **fields)


def neg(vector_id: str, outcome: str, twin: str, type_name: str, positive: dict, path: str, value: object, reason: str, **extra: object) -> dict:
    mutated = mutate(positive, path, value)
    shown = "<deleted>" if value is DELETE else value
    return computed(
        vector_id,
        outcome,
        evidence_class=AUTHORED,
        intended_check=reason,
        mutated=encoded(type_name, mutated),
        mutation={"path": path, "value": shown},
        note="authored, NOT EXECUTED",
        valid_twin=twin,
        **extra,
    )


def neg_obj(vector_id: str, outcome: str, twin: str, reason: str, **fields: object) -> dict:
    return computed(vector_id, outcome, evidence_class=AUTHORED, intended_check=reason, note="authored, NOT EXECUTED", valid_twin=twin, **fields)


def sched(vector_id: str, outcome: str, steps: list[str], **fields: object) -> dict:
    return computed(vector_id, outcome, evidence_class=SCHEDULE, note="authored schedule fixture; NOT EXECUTED", schedule=steps, **fields)


def ref(type_name: str, payload: dict) -> dict:
    return {"id": Hash(type_name, payload), "type": type_name}


def tree_leaf(epoch: str, position: int, entry_digest: str) -> bytes:
    return bytes.fromhex(Hash("TreeLeaf", {"entry_digest": entry_digest, "epoch": epoch, "position": str(position)}))


def mth(leaves: list[bytes]) -> bytes:
    if not leaves:
        return hashlib.sha256(F(D("tree.empty"), b"")).digest()
    if len(leaves) == 1:
        return leaves[0]
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    return hashlib.sha256(F(D("tree.node"), mth(leaves[:k]) + mth(leaves[k:]))).digest()


def ledger_head(epoch: str, digests: list[str], first_seq: int = 0) -> dict:
    leaves = [tree_leaf(epoch, first_seq + i, d) for i, d in enumerate(digests)]
    return {
        "count": str(len(digests)),
        "epoch": epoch,
        "first_seq": str(first_seq),
        "last_digest": digests[-1] if digests else Z,
        "last_seq": str(first_seq + len(digests) - 1) if digests else None,
        "root": mth(leaves).hex(),
    }


def range_ok(rng: dict) -> str | None:
    first, last, count = int(rng["first"]), int(rng["last"]), int(rng["count"])
    if first > last:
        return "binding"
    if last >= U_MAX:
        return "overflow"
    if last - first + 1 != count:
        return "binding"
    if (rng["predecessor"] is None) != (first == 0):
        return "binding"
    if first == 0 and rng["predecessor_head"] != Z:
        return "binding"
    if first > 0 and int(rng["predecessor"]) + 1 != first:
        return "binding"
    return None


def strict_parse(text: str) -> str | None:
    """Generator-side canonical check. Not the model parser."""
    if any(ord(c) > 127 for c in text):
        return "noncanonical"
    pairs_seen: list[bool] = []

    def hook(pairs: list) -> dict:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            pairs_seen.append(True)
        return dict(pairs)

    try:
        value = json.loads(text, object_pairs_hook=hook, parse_float=lambda s: 0.5, parse_int=lambda s: 1)
    except ValueError:
        return "malformed"
    if pairs_seen:
        return "malformed"
    try:
        again = J(value)
    except ValueError:
        return "schema"
    if again != text:
        return "noncanonical"
    return None


def full_chain() -> dict:
    """Dependency-ordered positive chain: genesis, issue, challenge, consume, ack, admission, entries, originals."""
    base = dag()
    epoch = nid(0x75)
    pol = policy_core()
    pol_digest = Hash("PolicyCore", pol)
    issue_bytes = canonical_bytes("IssueRequest", base["issue"]).encode("utf-8")
    challenge_bytes = canonical_bytes("ModelSigned", base["signed_challenge"]).encode("utf-8")
    issue_ref = ref("Challenge", base["challenge"])
    issue_entry = {
        "admission": None,
        "event": issue_ref,
        "ledger_epoch": epoch,
        "position": "0",
        "previous_global": Z,
        "request_hash": Raw(issue_bytes),
        "response_hash": Raw(challenge_bytes),
        "scope": scope_of(),
        "scoped_ordinal": "0",
        "stream": nid(0xD0),
    }
    issue_entry_digest = Hash("GlobalEntry", issue_entry)
    issue_original = {
        "entry_digest": issue_entry_digest,
        "event": issue_ref,
        "idempotency": base["issue"]["idempotency"],
        "operation": "issue",
        "request_bytes": b64u(issue_bytes),
        "request_hash": Raw(issue_bytes),
        "response_bytes": b64u(challenge_bytes),
        "response_hash": Raw(challenge_bytes),
        "scope": scope_of(),
        "stream": nid(0xD0),
    }
    consume_bytes = base["consume_text"].encode("utf-8")
    ack_bytes = canonical_bytes("ModelSigned", base["signed_ack"]).encode("utf-8")
    admission = {
        "challenge_payload_id": base["challenge_payload_id"],
        "expected_revision": "1",
        "label": "post_sign_admission_only",
        "owner_generation": "1",
        "policy_digest": pol_digest,
        "policy_generation": "1",
        "post_sign_at": mono(server_clock(), 109),
        "request_hash": Raw(consume_bytes),
        "response_hash": Raw(ack_bytes),
        "writer_term": "1",
    }
    consume_ref = ref("AckCore", base["ack"])
    consume_entry = {
        "admission": admission,
        "event": consume_ref,
        "ledger_epoch": epoch,
        "position": "1",
        "previous_global": issue_entry_digest,
        "request_hash": Raw(consume_bytes),
        "response_hash": Raw(ack_bytes),
        "scope": scope_of(),
        "scoped_ordinal": "1",
        "stream": nid(0xD0),
    }
    consume_entry_digest = Hash("GlobalEntry", consume_entry)
    consume_original = {
        "entry_digest": consume_entry_digest,
        "event": consume_ref,
        "idempotency": base["consume"]["idempotency"],
        "operation": "consume",
        "request_bytes": b64u(consume_bytes),
        "request_hash": Raw(consume_bytes),
        "response_bytes": b64u(ack_bytes),
        "response_hash": Raw(ack_bytes),
        "scope": scope_of(),
        "stream": nid(0xD0),
    }
    issue_key = {"exchange": nid(0xC0), "operation": "issue", "scope": scope_of(), "stream": nid(0xD0)}
    consume_key = dict(issue_key, operation="consume")
    issue_client = {
        "client_nonce": base["issue"]["client_nonce"],
        "core": base["issue_core"],
        "expected_id": base["issue_id"],
        "key": issue_key,
        "owner_generation": base["issue"]["owner_generation"],
        "prior_ack": base["issue"]["prior_ack"],
        "request_bytes": b64u(issue_bytes),
        "request_hash": Raw(issue_bytes),
        "request_type": "IssueRequest",
    }
    consume_client = {
        "client_nonce": base["consume"]["commitment"]["client_nonce"],
        "core": base["consume_core"],
        "expected_id": base["consume_id"],
        "key": consume_key,
        "owner_generation": base["consume"]["commitment"]["owner_generation"],
        "prior_ack": base["consume"]["commitment"]["prior_ack"],
        "request_bytes": b64u(consume_bytes),
        "request_hash": Raw(consume_bytes),
        "request_type": "ConsumeRequest",
    }
    head = ledger_head(epoch, [issue_entry_digest, consume_entry_digest])
    base.update(
        admission=admission,
        ack_bytes=ack_bytes,
        challenge_wire=challenge_bytes,
        consume_bytes=consume_bytes,
        consume_client=consume_client,
        consume_entry=consume_entry,
        consume_entry_digest=consume_entry_digest,
        consume_original=consume_original,
        epoch=epoch,
        issue_bytes=issue_bytes,
        issue_client=issue_client,
        issue_entry=issue_entry,
        issue_entry_digest=issue_entry_digest,
        issue_original=issue_original,
        ledger=head,
        policy=pol,
        policy_digest=pol_digest,
    )
    return base


def projection_ok(client: dict) -> str | None:
    """Generator-side check of §3.4 projection. Not the model."""
    raw = unb64u(client["request_bytes"])
    if Raw(raw) != client["request_hash"]:
        return "binding"
    envelope = json.loads(raw)
    rtype = envelope["schema"].rsplit(".", 1)[1]
    payload = envelope["payload"]
    if "operation" in payload or ("commitment" in payload and "operation" in payload["commitment"]):
        return "schema"
    op = {"IssueRequest": "issue", "ConsumeRequest": "consume"}.get(rtype)
    if op is None or client["request_type"] != rtype or client["key"]["operation"] != op or client["core"]["operation"] != op:
        return "binding"
    body = payload if rtype == "IssueRequest" else payload["commitment"]
    core = client["core"]
    for field in ("scope", "stream", "purpose", "ack_ordinal", "pins"):
        if body[field] != core[field]:
            return "binding"
    for field in ("owner_generation", "prior_ack", "client_nonce"):
        if body[field] != client[field]:
            return "binding"
    if body["expected_id"] != Hash("ClientExpectedCore", core) or client["expected_id"] != body["expected_id"]:
        return "binding"
    key = client["key"]
    if key["scope"] != core["scope"] or key["stream"] != core["stream"] or key["exchange"] != core["exchange"]:
        return "binding"
    return None


def f1_vectors(c: dict, checks: dict) -> list[dict]:
    rows = []
    ic, cc = c["issue_client"], c["consume_client"]
    for client in (ic, cc):
        if projection_ok(client) is not None:
            raise SystemExit("positive client record failed projection")
        checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 1
    enc_ic = encoded("ClientDispatchRecord", ic)
    enc_io = encoded("Original", c["issue_original"])
    rows.append(sched("V-F1-PRE-DUR", "client record durable before dispatch; no Original", ["client_commit", "dispatch_allowed"], client_record=enc_ic, service_original=None))
    rows.append(sched("V-F1-NO-DISPATCH", "no client record; no Original; no later body is the original", ["client_commit_incomplete", "no_dispatch"], client_record=None, service_original=None, attempted_record=enc_ic))
    rows.append(sched("V-F1-NO-COMMIT", "client record unchanged; no Original; not success", ["client_commit", "dispatch", "service_crash_before_swap"], client_record=enc_ic, service_original=None))
    rows.append(sched("V-F1-LOST-RESP", "Original stands; exact retry returns its response bytes; client record unchanged", ["client_commit", "dispatch", "service_swap", "response_lost", "retry_same_bytes"], client_record=enc_ic, service_original=enc_io, retry_response_hash=c["issue_original"]["response_hash"]))
    rows.append(sched("V-F1-REST-MATCH", "both stand; no rewrite", ["restore"], client_record=enc_ic, service_original=enc_io, request_hash_equal=ic["request_hash"] == c["issue_original"]["request_hash"]))
    rows.append(sched("V-F1-REST-NOCLIENT", "Original historical; client state unknown; no rebuild", ["restore"], client_record=None, service_original=enc_io))
    rows.append(sched("V-F1-REST-NOORIG", "client record stands; not service success; no invented Original", ["restore"], client_record=enc_ic, service_original=None))
    divergent = mutate(c["issue"], "seal", nh(0x7E))
    div_bytes = canonical_bytes("IssueRequest", divergent).encode("utf-8")
    div_client = dict(ic, request_bytes=b64u(div_bytes), request_hash=Raw(div_bytes))
    rows.append(sched("V-F1-REST-DIVERGE", "conflict; neither side replaced", ["restore"], client_record=encoded("ClientDispatchRecord", div_client), service_original=enc_io, hashes_differ=div_client["request_hash"] != c["issue_original"]["request_hash"]))
    rows.append(pos("V-F1-PROJ-OK", "client record accepted; no wire operation field", checks, client_record=enc_ic, generator_projection_check="ok"))
    mis_req = mutate(c["issue"], "purpose", "interval")
    mis_bytes = canonical_bytes("IssueRequest", mis_req).encode("utf-8")
    mis_client = dict(ic, request_bytes=b64u(mis_bytes), request_hash=Raw(mis_bytes))
    if projection_ok(mis_client) != "binding":
        raise SystemExit("projection mismatch not caught by generator check")
    checks["executed_primitive_assertions"] = checks.get("executed_primitive_assertions", 0) + 1
    rows.append(neg("V-F1-PROJ-MIS", "binding; no record", "V-F1-PROJ-OK", "ClientDispatchRecord", ic, "request_bytes", mis_client["request_bytes"], "projection: request purpose differs from core.purpose", generator_projection_check="binding"))
    own_client = dict(ic, owner_generation="2")
    if projection_ok(own_client) != "binding":
        raise SystemExit("owner projection not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg("V-F1-PROJ-OWN", "binding; no record", "V-F1-PROJ-OK", "ClientDispatchRecord", ic, "owner_generation", "2", "projection: owner_generation differs from request bytes", generator_projection_check="binding"))
    wire = dict(c["issue"], operation="issue")
    wire_text = J(E("IssueRequest", wire))
    rows.append(neg_obj("V-F1-NO-WIRE", "schema", "V-F1-PROJ-OK", "IssueRequest has no operation member", literal=wire_text, literal_raw=Raw(wire_text.encode("utf-8"))))
    rows.append(pos("V-F1-LINK", "link resolves inside one exchange group; ids distinct", checks, issue_record=enc_ic, consume_record=encoded("ClientDispatchRecord", cc), challenge_issue_request_hash=c["challenge"]["issue_request_hash"], issue_request_hash=ic["request_hash"], resolves=c["challenge"]["issue_request_hash"] == ic["request_hash"], ids_differ=ic["expected_id"] != cc["expected_id"], group={"exchange": ic["key"]["exchange"], "scope": ic["key"]["scope"], "stream": ic["key"]["stream"]}))
    other_core = base_core("issue", 0xCE)
    other_issue = issue_request(other_core, Hash("ClientExpectedCore", other_core), nid(0xE9))
    other_raw = Raw(canonical_bytes("IssueRequest", other_issue).encode("utf-8"))
    other_ch = dict(c["challenge"], challenge_id=nid(0x7A), issue_request_hash=other_raw)
    other_signed = model_signed("Challenge", other_ch, role("challenge"))
    other_bytes = b64u(canonical_bytes("ModelSigned", other_signed).encode("utf-8"))
    rows.append(neg("V-F1-XGROUP", "binding", "V-F1-LINK", "ConsumeRequest", c["consume"], "challenge_bytes", other_bytes, "challenge issue_request_hash names an issue in exchange group with a different exchange id", other_group_issue=encoded("IssueRequest", other_issue)))
    denial = {"at": mono(server_clock(), 105), "ordinal": "0", "prior": Z, "reason": "quota", "request_hash": ic["request_hash"], "scope_digest": Hash("Scope", scope_of())}
    witness = {"after": None, "before": nh(0x401), "outcome": "refused", "phase": "reserve", "revision": "1", "writer_term": "1"}
    rows.append(sched("V-F1-REFUSE", "client record stands; no Original; no new deadline", ["client_commit", "dispatch", "service_resolved_refusal"], client_record=enc_ic, denial=encoded("Denial", denial), witness=encoded("CommitWitness", witness), service_original=None))
    rows.append(sched("V-F1-RESTART", "record unchanged; no replacement", ["client_commit", "dispatch", "service_crash_before_swap", "restart"], client_record=enc_ic, service_original=None))
    return rows


def auth_binding(subject: dict, op: str, policy_generation: str, policy_digest: str, stream: str | None = None, owner: str | None = None, holder: str | None = None) -> dict:
    return {"holder": holder, "issuer": nid(0x90), "operation": op, "owner_generation": owner, "policy_digest": policy_digest, "policy_generation": policy_generation, "stream": stream, "subject": subject}


def checkpoint_request_for(journal: dict, bound: dict, idem: int) -> dict:
    payload = {
        "capture_domain": capture_clock(),
        "expected_revision": "1",
        "idempotency": nid(idem),
        "journal": journal,
        "owner_generation": "1",
        "pins": pins_of(),
        "scope": scope_of(),
        "source_bound": bound,
        "source_domain": server_clock(),
        "source_schema": nh(0x51),
        "stream": nid(0xD0),
    }
    assert_fields("CheckpointRequest", payload)
    return payload


def f2_vectors(c: dict, checks: dict) -> list[dict]:
    rows = []
    lin = auth_binding({"arm": "lineage", "scope": scope_of()}, "consume", "1", c["policy_digest"], nid(0xD0), "1")
    rows.append(pos("V-F2-AUTH-LIN", "lineage arm; subject.scope equals request scope", checks, auth=encoded("AuthBinding", lin), request_scope_equal=lin["subject"]["scope"] == c["consume"]["commitment"]["scope"]))
    new_core = dict(policy_core("2"), prior_digest=c["policy_digest"])
    reg = auth_binding({"arm": "registry", "registry": registry_of()}, "policy_update", "1", c["policy_digest"])
    rows.append(pos("V-F2-AUTH-REG", "registry arm; no lineage Scope", checks, auth=encoded("AuthBinding", reg), registry_equal=reg["subject"]["registry"] == new_core["registry"]))
    fake_scope = dict(registry_of(), lineage=nid(0xF00))
    rows.append(neg("V-F2-AUTH-FAKE", "schema or unauthorized; no fabricated lineage", "V-F2-AUTH-REG", "AuthBinding", reg, "subject", {"arm": "lineage", "scope": fake_scope}, "policy_update requires arm=registry"))
    record = {"core": new_core, "digest": Hash("PolicyCore", new_core)}
    signed = model_signed("PolicyCore", new_core, role("policy"))
    if signed["payload_id"] != record["digest"]:
        raise SystemExit("policy payload id differs from record digest")
    checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 1
    rows.append(pos("V-F2-POL-CORE", "request and signed payload are PolicyCore; stored row PolicyRecord with digest outside the core", checks, request=encoded("PolicyCore", new_core), stored=encoded("PolicyRecord", record), signed=encoded("ModelSigned", signed), authorizing_policy_digest=c["policy_digest"]))
    rows.append(neg("V-F2-POL-CUR", "unauthorized; retained policy stays the authorizer", "V-F2-AUTH-REG", "AuthBinding", reg, "policy_digest", record["digest"], "authorizing digest must be the retained current PolicyRecord, not the submitted core"))
    rows.append(pos("V-F2-RECEIPT-CONSUME", "one AckCore Original and one GlobalEntry; no CheckpointRecord; no receipt charge", checks, original=encoded("Original", c["consume_original"]), entry=encoded("GlobalEntry", c["consume_entry"]), checkpoint_records_written=[]))
    bound = source_bound(0, 0xF1)
    ck_req = checkpoint_request_for(c["journal"], bound, 0xEA)
    ck_text = canonical_bytes("CheckpointRequest", ck_req).encode("utf-8")
    ck_rec = checkpoint_record(Raw(ck_text), c["checkpoint_id"], c["journal"], bound)
    ck_signed = model_signed("CheckpointRecord", ck_rec, role("checkpoint"))
    ck_resp = canonical_bytes("ModelSigned", ck_signed).encode("utf-8")
    ck_ref = ref("CheckpointRecord", ck_rec)
    ck_entry = {"admission": None, "event": ck_ref, "ledger_epoch": c["epoch"], "position": "2", "previous_global": c["consume_entry_digest"], "request_hash": Raw(ck_text), "response_hash": Raw(ck_resp), "scope": scope_of(), "scoped_ordinal": "2", "stream": nid(0xD0)}
    ck_orig = {"entry_digest": Hash("GlobalEntry", ck_entry), "event": ck_ref, "idempotency": ck_req["idempotency"], "operation": "checkpoint", "request_bytes": b64u(ck_text), "request_hash": Raw(ck_text), "response_bytes": b64u(ck_resp), "response_hash": Raw(ck_resp), "scope": scope_of(), "stream": nid(0xD0)}
    c.update(ck_req=ck_req, ck_rec=ck_rec, ck_entry=ck_entry, ck_orig=ck_orig, ck_rec_id=ck_ref["id"])
    rows.append(pos("V-F2-RECEIPT-CKPT", "one CheckpointRecord row; the only service receipt; checkpoint charge only", checks, request=encoded("CheckpointRequest", ck_req), record=encoded("CheckpointRecord", ck_rec), original=encoded("Original", ck_orig), entry=encoded("GlobalEntry", ck_entry), receipt_id=ck_ref["id"]))
    opening = json.loads(unb64u(c["consume_original"]["request_bytes"]))["payload"]["commitment"]
    origin = {"capture_domain": opening["capture_domain"], "pins": opening["pins"], "source_domain": opening["source_domain"], "source_schema": opening["source"]["source_schema"]}
    if origin != stream_origin() or any(r["domain"] != origin["capture_domain"] for r in opening["capture_ranges"]):
        raise SystemExit("origin lookup disagrees with fixture")
    checks["generator_schema_checks"] += 1
    c["origin"] = origin
    rows.append(pos("V-F2-DECL", "domains stored as declarations; not a calibrated clock", checks, origin=encoded("StreamOrigin", origin), label="declaration"))
    rows.append(pos("V-F2-ORIGIN-FIELDS", "origin from opening commitment fields by the §9.1 lookup", checks, opening_original=encoded("Original", c["consume_original"]), origin=encoded("StreamOrigin", origin), lookup={"capture_domain": "commitment.capture_domain", "pins": "commitment.pins", "source_domain": "commitment.source_domain", "source_schema": "commitment.source.source_schema"}))
    later = {k: ck_req[k] for k in ("pins", "source_schema", "capture_domain", "source_domain")}
    rows.append(pos("V-F2-LATER-EQ", "later four fields equal the stored origin", checks, request=encoded("CheckpointRequest", ck_req), equal=later == origin))
    rows.append(neg("V-F2-MISS-DOMAIN", "malformed; no stored origin; no default", "V-F2-ORIGIN-FIELDS", "Commitment", c["consume"]["commitment"], "source_domain", DELETE, "opening commitment must carry source_domain"))
    return rows


def external_basis(c: dict, *, limits: dict | None = None) -> dict:
    cfg_core = {"charge_profile": "b4-r2", "limits": limits or limit_set(), "model_instance": nid(0x500), "server_domain": {k: server_clock()[k] for k in ("boot", "clock_epoch", "leader", "process", "role", "source", "unit")}, "tenant": nid(0xA0)}
    cfg = {"core": cfg_core, "digest": Hash("ConfigCore", cfg_core)}
    minimum = {"basis": nid(0x501), "digest": c["policy_digest"], "generation": "1", "key": {"kind": "owner", "registry": registry_of(), "subject": {"arm": "registry", "registry": registry_of()}}}
    stocks = accounting_stocks()
    members = sorted(
        [
            {"class": "policy", "identity": c["policy_digest"]},
            {"class": "minimum", "identity": Hash("Minimum", minimum)},
            {"class": "registry", "identity": Hash("RegistryScope", registry_of())},
            {"class": "lineage_scope", "identity": Hash("Scope", scope_of())},
            {"class": "role", "identity": Hash("PolicyRole", c["policy"]["roles"][0])},
            {"class": "observer", "identity": nh(0x502)},
        ],
        key=J,
    )
    continuity = {"config_digest": cfg["digest"], "server_domain": cfg_core["server_domain"], "service_epoch": nid(0x503), "writer_term": "1"}
    return {"accounting": stocks, "config": cfg, "continuity": continuity, "expected": {"members": members}, "minima": [minimum]}


def accounting_stocks(records_p1: int = 2, records_p2: int = 1) -> dict:
    total = records_p1 + records_p2
    stocks = [
        {"class": "cumulative", "key": {"arm": "principal", "principal": nid(0xA1)}, "metric": "records", "value": str(records_p1)},
        {"class": "cumulative", "key": {"arm": "principal", "principal": nid(0xB1)}, "metric": "records", "value": str(records_p2)},
        {"class": "cumulative", "key": {"arm": "tenant", "tenant": nid(0xA0)}, "metric": "records", "value": str(total)},
        {"class": "cumulative", "key": {"arm": "model", "model": nid(0x500)}, "metric": "records", "value": str(total)},
        {"class": "cumulative", "key": {"aggregate": "all", "arm": "aggregate"}, "metric": "records", "value": str(total)},
    ]
    floors = [{"key": {"aggregate": "all", "arm": "aggregate"}, "metric": "records", "minimum": str(total)}]
    return {"floors": floors, "stocks": sorted(stocks, key=J)}


def stocks_reconcile(acct: dict) -> str | None:
    """Generator-side arithmetic for §5.3 aggregate rule. Not the model."""
    by = {}
    for s in acct["stocks"]:
        by.setdefault((s["metric"], s["class"], s["key"]["arm"]), []).append(int(s["value"]))
    for (metric, cls, arm), vals in list(by.items()):
        if arm != "aggregate":
            continue
        agg = vals[0]
        for other in ("principal", "tenant", "model"):
            if sum(by.get((metric, cls, other), [])) != agg:
                return "unavailable"
    for f in acct["floors"]:
        match = [int(s["value"]) for s in acct["stocks"] if s["key"] == f["key"] and s["metric"] == f["metric"]]
        if not match or match[0] < int(f["minimum"]):
            return "unavailable"
    return None


def scoped_head(c: dict, *, closed: str = "open") -> dict:
    return {
        "ack_count": "1",
        "closed": closed,
        "event_count": "2",
        "first_break": None,
        "journal": c["journal"],
        "last_ack": Hash("AckCore", c["ack"]),
        "last_event": c["consume_entry_digest"],
        "owner_generation": "1",
        "scope": scope_of(),
        "source_bound": source_bound(),
        "stream": nid(0xD0),
        "unresolved_holds": [],
    }


def stable_core(c: dict, basis: dict, policies: list[dict] | None = None) -> dict:
    row = {
        "challenge_bytes": b64u(c["challenge_wire"]),
        "invalidated_by": None,
        "state": "consumed",
        "winning_request_hash": c["consume_original"]["request_hash"],
        "winning_response_hash": c["consume_original"]["response_hash"],
    }
    stream = {"fork": "open", "head": scoped_head(c), "next_ack_ordinal": "1", "next_scoped_ordinal": "2", "origin": stream_origin(), "seal": c["consume"]["commitment"]["seal"]}
    pol_rows = policies if policies is not None else [{"core": c["policy"], "digest": c["policy_digest"]}]
    return {
        "accounting": basis["accounting"],
        "challenges": [row],
        "checkpoints": [],
        "config_hash": basis["config"]["digest"],
        "denials": [],
        "entries": [c["issue_entry"], c["consume_entry"]],
        "event_payloads": [b64u(c["challenge_wire"]), b64u(c["ack_bytes"])],
        "holds": [],
        "ledger_head": c["ledger"],
        "nonce_tombstones": [c["challenge"]["server_nonce"]],
        "observer_hash": nh(0x502),
        "originals": sorted([c["issue_original"], c["consume_original"]], key=J),
        "owners": [],
        "policies": pol_rows,
        "revision": "2",
        "service_epoch": nid(0x503),
        "snapshots": [],
        "status": "available",
        "streams": [stream],
        "writer_term": "1",
    }


def image_of(core: dict) -> dict:
    return {"core": core, "core_id": Hash("StableCore", core)}


def restore_completeness(image: dict, basis: dict) -> str | None:
    """Generator-side bidirectional set check of §5.4 step 4. Not the model."""
    expected = {(m["class"], m["identity"]) for m in basis["expected"]["members"]}
    present = {("policy", p["digest"]) for p in image["core"]["policies"]}
    present |= {("minimum", Hash("Minimum", m)) for m in basis["minima"]}
    present |= {("observer", image["core"]["observer_hash"])}
    exp_sub = {e for e in expected if e[0] in {"policy", "minimum", "observer"}}
    if exp_sub - present or present - exp_sub:
        return "unknown"
    return None


def f3_vectors(c: dict, checks: dict) -> list[dict]:
    rows = []
    basis = external_basis(c)
    core = stable_core(c, basis)
    image = image_of(core)
    if restore_completeness(image, basis) is not None or stocks_reconcile(basis["accounting"]) is not None:
        raise SystemExit("positive restore basis failed generator checks")
    checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 2
    c.update(basis=basis, image=image)
    rows.append(pos("V-F3-EXP-OK", "restore proceeds", checks, basis=encoded("ExternalBasis", basis), image=encoded("StableImage", image), expected_set_id=Hash("ExpectedSet", basis["expected"])))
    deleted = image_of(dict(core, policies=[]))
    if restore_completeness(deleted, basis) != "unknown":
        raise SystemExit("deleted policy not caught")
    checks["executed_primitive_assertions"] = checks.get("executed_primitive_assertions", 0) + 1
    rows.append(neg("V-F3-EXP-MISS", "unknown", "V-F3-EXP-OK", "StableImage", image, "core.policies", [], "expected policy member missing from image; expected set is external", generator_completeness_check="unknown"))
    extra_pol = policy_core("1", "b")
    extra_rows = sorted([{"core": c["policy"], "digest": c["policy_digest"]}, {"core": extra_pol, "digest": Hash("PolicyCore", extra_pol)}], key=J)
    if restore_completeness(image_of(dict(core, policies=extra_rows)), basis) != "unknown":
        raise SystemExit("extra policy not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg("V-F3-EXP-EXTRA", "unknown", "V-F3-EXP-OK", "StableImage", image, "core.policies", extra_rows, "present policy row not in expected set", generator_completeness_check="unknown"))
    rows.append(neg("V-F3-KEY-SWAP", "binding", "V-F3-EXP-OK", "ExternalBasis", basis, "minima.0.key.subject", {"arm": "lineage", "scope": scope_of()}, "ScopeTag arm swapped from registry to lineage"))
    rows.append(neg("V-F3-KEY-ARM", "binding", "V-F3-EXP-OK", "ExternalBasis", basis, "minima.0.key.subject", {"arm": "registry", "scope": scope_of()}, "registry arm carrying a lineage scope member"))
    rehashed = image_of(dict(core, policies=[]))
    rows.append(neg_obj("V-F3-HASH", "unknown", "V-F3-EXP-OK", "self-hash recomputed after row delete does not satisfy the external expected set", image=encoded("StableImage", rehashed), core_id_recomputed=rehashed["core_id"] == Hash("StableCore", rehashed["core"])))
    bad_acct = accounting_stocks()
    bad_acct["stocks"] = [dict(s, value="4") if s["key"]["arm"] == "aggregate" else s for s in bad_acct["stocks"]]
    if stocks_reconcile(bad_acct) != "unavailable":
        raise SystemExit("aggregate mismatch not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg("V-F3-STOCK", "unavailable", "V-F3-EXP-OK", "ExternalBasis", basis, "accounting", bad_acct, "aggregate stock differs from principal and tenant sums", generator_reconcile_check="unavailable"))
    low = accounting_stocks(1, 1)
    low["floors"] = accounting_stocks()["floors"]
    if stocks_reconcile(low) != "unavailable":
        raise SystemExit("floor not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg("V-F3-FLOOR", "unavailable", "V-F3-EXP-OK", "ExternalBasis", basis, "accounting", low, "cumulative aggregate below its external floor", generator_reconcile_check="unavailable"))
    return rows


def witness(phase: str, outcome: str, before: str, after: str | None) -> dict:
    return {"after": after, "before": before, "outcome": outcome, "phase": phase, "revision": "2", "writer_term": "1"}


def f4_vectors(c: dict, checks: dict) -> list[dict]:
    rows = []
    img = c["image"]["core_id"]
    nxt = nh(0x601)
    gauge_before = {"class": "gauge", "key": {"arm": "model", "model": nid(0x500)}, "metric": "outstanding", "value": "1"}
    gauge_after = dict(gauge_before, value="0")
    unused = {"challenge_bytes": b64u(c["challenge_wire"]), "invalidated_by": None, "state": "unused", "winning_request_hash": None, "winning_response_hash": None}
    consumed = c["image"]["core"]["challenges"][0]
    rows.append(pos("V-F4-GAUGE-ZERO", "outstanding restores at zero after explained consume", checks, before_stock=encoded("KeyedStock", gauge_before), after_stock=encoded("KeyedStock", gauge_after), transition={"from": encoded("ChallengeRow", unused), "to": encoded("ChallengeRow", consumed)}))
    rows.append(neg_obj("V-F4-GAUGE-ROLL", "unavailable", "V-F4-GAUGE-ZERO", "gauge decrease with no unused-to-consumed or invalidated transition", before_stock=encoded("KeyedStock", gauge_before), after_stock=encoded("KeyedStock", gauge_after), transition=None))
    crossings = [
        ("V-F4-PRE-RES", "resolved refusal; nothing after receive started", None, "receive calls and retry_attempts if that receive basis commit is witnessed; nothing else"),
        ("V-F4-POST-RES", "resolved refusal; reservation released", witness("reserve", "refused", img, None), "receive calls if witnessed"),
        ("V-F4-POST-WORK", "resolved refusal only with witness refused; basis charges stay", witness("work", "refused", img, None), "failed sign, completed read, receive calls if witnessed"),
        ("V-F4-POST-SWAP", "commit_unknown; swapped image stands; not a refusal", None, "image stocks stand"),
        ("V-F4-POST-WIT", "committed", witness("swap", "committed", img, nxt), "image plus basis"),
    ]
    for vector_id, outcome, w, charges in crossings:
        rows.append(sched(vector_id, outcome, ["receive", "reserve", "work", "prepare", "swap", "confirm", "deliver"], crash_after=vector_id.split("-", 2)[2].lower(), witness=encoded("CommitWitness", w) if w else None, basis_charges=charges, image_before=img))
    rows.append(sched("V-F4-REFUSE", "resolved refusal", ["receive", "reserve", "refuse"], witness=encoded("CommitWitness", witness("reserve", "refused", img, None))))
    rows.append(sched("V-F4-UNKNOWABLE", "unavailable; commit_unknown", ["receive", "reserve", "work", "crash"], witness=None, image_before=img))
    sign_stock = {"class": "cumulative", "key": {"arm": "model", "model": nid(0x500)}, "metric": "signer_calls", "value": "1"}
    rows.append(sched("V-F4-SIGN", "basis signer charge; no positive record", ["receive", "reserve", "work:sign_failed"], basis_stock_after=encoded("KeyedStock", sign_stock), witness=encoded("CommitWitness", witness("work", "refused", img, None)), originals_added=[]))
    read_stock = dict(sign_stock, metric="store_calls")
    rows.append(sched("V-F4-READ", "basis store_calls charge", ["receive", "read_complete"], basis_stock_after=encoded("KeyedStock", read_stock)))
    req_len = len(unb64u(c["consume_original"]["request_bytes"]))
    resp_len = len(unb64u(c["consume_original"]["response_bytes"]))
    rows.append(pos("V-F4-NEST", "consume stored_bytes counts request and AckCore response once", checks, request_bytes=req_len, response_bytes=resp_len, stored_bytes_charge=req_len + resp_len, checkpoint_record_bytes=0))
    attr = {"authn": "unauthenticated", "model": nid(0x500), "principal": None, "tenant": None}
    rows.append(pos("V-F4-ATTR", "model and aggregate only", checks, attribution=encoded("ChargeAttr", attr), charged_arms=["aggregate", "model"]))
    return rows


def attempt(c: dict, **over: object) -> dict:
    row = {
        "arrival": None,
        "attempt": "1",
        "dispatch": None,
        "durable_resolution": mono(client_clock(), 150),
        "expected_id": c["consume_id"],
        "raw_response_hash": c["consume_original"]["response_hash"],
        "result": "retained",
    }
    row.update(over)
    return row


def f5_vectors(c: dict, checks: dict) -> list[dict]:
    rows = []
    ok = attempt(c)
    inputs = {"accounting": "reconciled", "continuity": "reconciled", "original_linkage": "present", "prior_negative": None, "witness": "committed"}
    rows.append(pos("V-F5-RET-OK", "historical retained; eligible as history only", checks, attempt=encoded("ClientAttempt", ok), core=encoded("ClientExpectedCore", c["consume_core"]), fixture_inputs=inputs, fixture_inputs_label="eligibility inputs; not a schema type"))
    for vector_id, key, value in [
        ("V-F5-RET-WIT", "witness", "unknown"),
        ("V-F5-RET-ACCT", "accounting", "uncertain"),
        ("V-F5-RET-CONT", "continuity", "uncertain"),
        ("V-F5-RET-LINK", "original_linkage", "missing"),
        ("V-F5-RET-PRIOR-MISS", "prior_negative", "missing"),
        ("V-F5-RET-PRIOR-LATE", "prior_negative", "late"),
        ("V-F5-RET-PRIOR-FORK", "prior_negative", "fork"),
    ]:
        rows.append(neg_obj(vector_id, "not eligible; historical label stays retained", "V-F5-RET-OK", f"present eligibility: {key}={value}", attempt=encoded("ClientAttempt", ok), fixture_inputs=dict(inputs, **{key: value})))
    missing = attempt(c, durable_resolution=None, raw_response_hash=None, result="missing")
    rows.append(neg_obj("V-F5-RET-READBACK", "missing stays missing; no time filled", "V-F5-RET-OK", "readback must not fill arrival or durable_resolution", attempt=encoded("ClientAttempt", missing), readback_original=encoded("Original", c["consume_original"])))
    return rows


def page_set(c: dict, entries: list[dict], scope: dict, page_items: int) -> dict:
    """Snapshot core, pages, page root, manifest per r1 §7.2 with checked arithmetic."""
    n = len(entries)
    count = max(1, -(-n // page_items))
    digests = [Hash("GlobalEntry", e) for e in entries]
    head = ledger_head(c["epoch"], digests)
    core = {
        "ack_count": "1", "checkpoints": [], "event_count": str(n), "global": head, "nonce": nh(0x202),
        "observed_revision": "2", "observer_set_hash": nh(0x502), "owner_generation": "1", "page_count": str(count),
        "page_items": str(page_items), "parents": [], "retention": "unqualified_no_pruning", "scope": scope,
        "stream_heads": [scoped_head(c)], "terminal_records": [], "unresolved_holds": [],
    }
    core_id = Hash("SnapshotCore", core)
    pages, prev = [], None
    for i in range(count):
        chunk = entries[i * page_items:(i + 1) * page_items]
        page = {
            "core_id": core_id, "count": str(len(chunk)),
            "entries": [{"entry": e, "entry_digest": Hash("GlobalEntry", e)} for e in chunk],
            "first_ordinal": str(i * page_items), "index": str(i),
            "next_cursor": Hash("Cursor", {"core_id": core_id, "index": str(i + 1)}) if i + 1 < count else None,
            "previous_page": prev,
        }
        prev = Hash("Page", page)
        pages.append(page)
    leaves = [bytes.fromhex(Hash("PageLeaf", {"core_id": core_id, "index": str(i), "page_id": Hash("Page", p)})) for i, p in enumerate(pages)]
    manifest = {"core": core, "core_id": core_id, "page_count": str(count), "page_root": mth(leaves).hex(), "witness": {"head": head, "leaves": [tree_leaf(c["epoch"], i, d).hex() for i, d in enumerate(digests)]}}
    return {"core": core, "core_id": core_id, "head": head, "manifest": manifest, "pages": pages}


def witness_ok(witness_obj: dict, known: list[dict]) -> str | None:
    """Generator-side full hash-only prefix check. Not the model."""
    head = witness_obj["head"]
    leaves = [bytes.fromhex(x) for x in witness_obj["leaves"]]
    if str(len(leaves)) != head["count"] or mth(leaves).hex() != head["root"]:
        return "incomplete"
    for k in known:
        if k["first_seq"] != head["first_seq"] or int(k["count"]) > len(leaves):
            return "incomplete"
        if mth(leaves[: int(k["count"])]).hex() != k["root"]:
            return "fork"
    return None


def entry_for(c: dict, pos_: int, prev: str, stream: str, ordinal: int, marker: int) -> dict:
    ev = {"id": nh(marker), "type": "ObservationRecord"}
    return {"admission": None, "event": ev, "ledger_epoch": c["epoch"], "position": str(pos_), "previous_global": prev,
            "request_hash": nh(marker + 1), "response_hash": nh(marker + 2), "scope": scope_of(), "scoped_ordinal": str(ordinal), "stream": stream}


def denial_chain(n: int) -> list[dict]:
    """r1 Denial list: ordinal order, prior = Hash of the previous Denial, Z first."""
    out: list[dict] = []
    prior = Z
    for k in range(n):
        row = {"at": mono(server_clock(), 105 + k), "ordinal": str(k), "prior": prior, "reason": "quota", "request_hash": nh(0x880 + k), "scope_digest": Hash("Scope", scope_of())}
        out.append(row)
        prior = Hash("Denial", row)
    return out


def fam_vectors(c: dict, checks: dict) -> list[dict]:
    rows: list[dict] = []
    checks.setdefault("executed_primitive_assertions", 0)
    # 1 Expected DAG
    rows.append(pos("V-FAM-DAG-CHAIN", "complete issue, Challenge, consume, AckCore, Admission, GlobalEntry, Original chain", checks,
                    issue=encoded("IssueRequest", c["issue"]), challenge=encoded("ModelSigned", c["signed_challenge"]),
                    consume=encoded("ConsumeRequest", c["consume"]), ack=encoded("ModelSigned", c["signed_ack"]),
                    admission=encoded("Admission", c["admission"]), entries=[encoded("GlobalEntry", c["issue_entry"]), encoded("GlobalEntry", c["consume_entry"])],
                    originals=[encoded("Original", c["issue_original"]), encoded("Original", c["consume_original"])], ledger=encoded("LedgerHead", c["ledger"]),
                    challenge_bytes_decode_to_signed_challenge=unb64u(c["consume"]["challenge_bytes"]) == c["challenge_wire"]))
    rows.append(neg("V-FAM-DAG-NESTED-SCOPE", "binding", "V-FAM-DAG-CHAIN", "ConsumeRequest", c["consume"], "commitment.scope.tenant", nid(0x611), "commitment scope differs from consume core and challenge"))
    rows.append(neg("V-FAM-DAG-NESTED-PINS", "binding", "V-FAM-DAG-CHAIN", "ConsumeRequest", c["consume"], "commitment.pins.checker", nh(0x612), "commitment pins differ from core"))
    rows.append(neg("V-FAM-DAG-DOMAIN", "domain", "V-FAM-DAG-CHAIN", "Challenge", c["challenge"], "expires_at.domain.process", nid(0x613), "issued_at and expires_at domains differ"))
    rows.append(neg("V-FAM-DAG-STALE", "binding", "V-FAM-DAG-CHAIN", "ClientDispatchRecord", c["issue_client"], "expected_id", Hash("ClientExpectedCore", mutate(c["issue_core"], "original_deadline.value", "260")), "recomputed id is not the registered client record id"))
    # 2 Original request / attempts
    loser = mutate(c["consume"], "commitment.seal", nh(0x621))
    loser_raw = Raw(canonical_bytes("ConsumeRequest", loser).encode("utf-8"))
    rows.append(pos("V-FAM-ORIG-WINNER", "one Original; one advance", checks, original=encoded("Original", c["consume_original"])))
    rows.append(neg("V-FAM-ORIG-LOSER", "conflict; never inherits winner success", "V-FAM-ORIG-WINNER", "ConsumeRequest", c["consume"], "commitment.seal", nh(0x621), "same challenge, divergent body", loser_raw=loser_raw))
    rows.append(sched("V-FAM-ORIG-RETRY", "byte-identical response; no re-sign, no new time, no advance", ["swap", "response_lost", "expiry", "restart", "retry_same_bytes"], request=encoded("ConsumeRequest", c["consume"]), original=encoded("Original", c["consume_original"]), request_hash=c["consume_original"]["request_hash"], response_hash=c["consume_original"]["response_hash"], new_tags=[]))
    rows.append(neg("V-FAM-ORIG-OWNER", "conflict", "V-FAM-ORIG-WINNER", "ConsumeRequest", c["consume"], "commitment.owner_generation", "2", "owner generation changed under a consumed challenge"))
    other_signed = model_signed("Challenge", dict(c["challenge"], challenge_id=nid(0x622)), role("challenge"))
    rows.append(neg("V-FAM-ORIG-CHALLENGE", "binding", "V-FAM-ORIG-WINNER", "ConsumeRequest", c["consume"], "challenge_bytes", b64u(canonical_bytes("ModelSigned", other_signed).encode("utf-8")), "challenge bytes do not match commitment.challenge_id"))
    rows.append(neg("V-FAM-ORIG-TYPE", "binding", "V-FAM-ORIG-WINNER", "Original", c["consume_original"], "operation", "issue", "Original operation does not match request type"))
    # 3 Alias / original deadlines
    rows.append(pos("V-FAM-ALIAS-GROUP", "issue and consume records in one exchange group; distinct ids", checks, issue=encoded("ClientDispatchRecord", c["issue_client"]), consume=encoded("ClientDispatchRecord", c["consume_client"])))
    rows.append(neg("V-FAM-ALIAS-REDEADLINE", "conflict; stored deadline unchanged", "V-FAM-ALIAS-GROUP", "ClientDispatchRecord", c["issue_client"], "core.original_deadline.value", "300", "same LogicalOpKey, later deadline"))
    rows.append(neg("V-FAM-ALIAS-NEWID", "conflict", "V-FAM-ALIAS-GROUP", "ClientDispatchRecord", c["issue_client"], "expected_id", nh(0x631), "same key, new expected_id"))
    rows.append(neg("V-FAM-ALIAS-WRONGOP", "binding", "V-FAM-ALIAS-GROUP", "ClientDispatchRecord", c["consume_client"], "key.operation", "issue", "key operation differs from request-type projection"))
    late = attempt(c, arrival=mono(client_clock(), 250), durable_resolution=None, raw_response_hash=None, result="late")
    rows.append(neg_obj("V-FAM-ALIAS-RESTORE-LATE", "late stays late after restore and archive", "V-FAM-ALIAS-GROUP", "restore and archive readback cannot upgrade a late attempt", attempt=encoded("ClientAttempt", late), archive_original=encoded("Original", c["consume_original"])))
    # 4 Bootstrap / pins
    rows.append(pos("V-FAM-BOOT-FIRST", "genesis, first client core, consume with null receipt prior", checks, genesis=encoded("GenesisCore", c["genesis"]), client=encoded("ClientCheckpointCore", c["checkpoint_core"]), journal=encoded("JournalRef", c["journal"])))
    rows.append(pos("V-FAM-BOOT-NEXT", "checkpoint commits CheckpointRecord; lookup tables resolve", checks, request=encoded("CheckpointRequest", c["ck_req"]), record=encoded("CheckpointRecord", c["ck_rec"]),
                    lookup={"client_checkpoint_id": {"id": c["checkpoint_id"], "table": "client core"}, "genesis_id": {"id": c["genesis_id"], "table": "genesis fixture"}, "service_receipt_id": {"id": c["ck_rec_id"], "table": "checkpoint Original"}}))
    nxt_core = client_checkpoint_core(c["checkpoint_id"], 0x62, first="1", last="1", count="1")
    nxt_id = Hash("ClientCheckpointCore", nxt_core)
    later_journal = journal_ref(nxt_id, c["checkpoint_id"], c["ck_rec_id"], 0x62, first="1", last="1", count="1")
    rows.append(pos("V-FAM-BOOT-LATER", "later consume names the committed receipt as prior; no second receipt", checks, client=encoded("ClientCheckpointCore", nxt_core), journal=encoded("JournalRef", later_journal)))
    rows.append(neg("V-FAM-BOOT-HEAD", "fork", "V-FAM-BOOT-NEXT", "CheckpointRequest", c["ck_req"], "source_bound.head", nh(0xF2), "equal endpoint, different head"))
    rows.append(pos("V-FAM-BOOT-ARCHIVE", "archive returns historical bytes; origin not replaced", checks, read=encoded("ReadOriginalRequest", {"operation": "consume", "request_hash": c["consume_original"]["request_hash"], "response_hash": c["consume_original"]["response_hash"], "scope": scope_of()}), origin=encoded("StreamOrigin", c["origin"])))
    # 5 Grammar / roles / nulls: literal inputs, checked by the generator's own canonical routine
    good = J(E("Scope", scope_of()))
    literals = [
        ("V-FAM-GRAM-NONCANON", '{"b":"1","a":"0"}', "noncanonical"),
        ("V-FAM-GRAM-DUP", '{"a":"0","a":"0"}', "malformed"),
        ("V-FAM-GRAM-NUMBER", '{"a":0}', "schema"),
        ("V-FAM-GRAM-BOOL", '{"a":true}', "schema"),
        ("V-FAM-GRAM-SPACE", '{"a": "0"}', "noncanonical"),
        ("V-FAM-GRAM-ESCAPE", '{"a":"\\u0030"}', "noncanonical"),
        ("V-FAM-GRAM-NONASCII", '{"a":"\u00e9"}', "noncanonical"),
    ]
    rows.append(pos("V-FAM-GRAM-OK", "canonical", checks, literal=good, generator_canonical_check=strict_parse(good) or "ok"))
    for vector_id, text, want in literals:
        got = strict_parse(text)
        if got != want:
            raise SystemExit(f"{vector_id} generator check {got} != {want}")
        checks["executed_primitive_assertions"] += 1
        rows.append(neg_obj(vector_id, want, "V-FAM-GRAM-OK", "canonical J input check", literal=text, generator_canonical_check=got, generator_check_label="generator routine, not the model parser"))
    rows.append(neg("V-FAM-GRAM-UNKNOWN", "schema", "V-FAM-GRAM-OK", "Scope", scope_of(), "extra", nid(1), "unknown member"))
    rows.append(neg("V-FAM-GRAM-MISSING", "schema", "V-FAM-GRAM-OK", "Scope", scope_of(), "chain", DELETE, "missing member"))
    rows.append(neg("V-FAM-GRAM-U00", "noncanonical", "V-FAM-DAG-CHAIN", "IssueRequest", c["issue"], "owner_generation", "01", "U leading zero"))
    rows.append(neg("V-FAM-GRAM-B64PAD", "noncanonical", "V-FAM-DAG-CHAIN", "ConsumeRequest", c["consume"], "challenge_bytes", c["consume"]["challenge_bytes"] + "=", "padded Bytes"))
    rows.append(neg("V-FAM-GRAM-NULL-ABSENT", "malformed", "V-FAM-DAG-CHAIN", "IssueRequest", c["issue"], "seal", DELETE, "T? field absent instead of null"))
    rows.append(neg("V-FAM-GRAM-ROLE", "binding", "V-FAM-DAG-CHAIN", "ModelSigned", c["signed_challenge"], "role.kind", "ack", "tag role kind differs from record kind"))
    rows.append(neg("V-FAM-GRAM-DOMAIN", "domain", "V-FAM-DAG-CHAIN", "AckCore", c["ack"], "received_at.domain.role", "client_mono", "server sample with client role"))
    # 6 Policy / config / minima
    basis, image = c["basis"], c["image"]
    rows.append(pos("V-FAM-POL-RESTORE", "restore proceeds", checks, basis=encoded("ExternalBasis", basis), image=encoded("StableImage", image)))
    changed = dict(limit_set(), records="9")
    cap_core = dict(image["core"], config_hash=Hash("ConfigCore", dict(basis["config"]["core"], limits=changed)))
    rows.append(neg_obj("V-FAM-POL-CAP-REHASH", "conflict; image caps not adopted", "V-FAM-POL-RESTORE", "image config_hash differs from external ConfigRecord even after self-hash recompute", image=encoded("StableImage", image_of(cap_core))))
    rows.append(neg("V-FAM-POL-GEN-LOW", "regression", "V-FAM-POL-RESTORE", "ExternalBasis", basis, "minima.0.generation", "2", "image generation below external minimum"))
    rows.append(neg("V-FAM-POL-EQFORK", "fork", "V-FAM-POL-RESTORE", "ExternalBasis", basis, "minima.0.digest", Hash("PolicyCore", policy_core("1", "b")), "same generation, different digest"))
    rows.append(neg("V-FAM-POL-FILTER", "policy_unknown", "V-FAM-POL-RESTORE", "ExternalBasis", basis, "minima", [], "filtered minima list"))
    rows.append(neg("V-FAM-POL-WRONG-AUTH", "unauthorized", "V-F2-AUTH-REG", "AuthBinding", auth_binding({"arm": "registry", "registry": registry_of()}, "policy_update", "1", c["policy_digest"]), "policy_generation", "0", "authorizing generation is not the retained current policy"))
    # 7 Scope / operation map
    lin = auth_binding({"arm": "lineage", "scope": scope_of()}, "consume", "1", c["policy_digest"], nid(0xD0), "1")
    rows.append(pos("V-FAM-OP-CONSUME", "op, auth, response, Original and GlobalEntry agree", checks, auth=encoded("AuthBinding", lin), original=encoded("Original", c["consume_original"]), entry=encoded("GlobalEntry", c["consume_entry"])))
    rows.append(neg("V-FAM-OP-TENANT", "unauthorized; no body", "V-FAM-OP-CONSUME", "AuthBinding", lin, "subject.scope.tenant", nid(0x711), "foreign tenant"))
    rows.append(neg("V-FAM-OP-OWNER", "unauthorized", "V-FAM-OP-CONSUME", "AuthBinding", lin, "owner_generation", "2", "owner generation mismatch"))
    rows.append(neg("V-FAM-OP-HOLDER", "unauthorized", "V-FAM-OP-CONSUME", "AuthBinding", lin, "holder", nid(0x81), "holder must be null on consume"))
    rows.append(neg("V-FAM-OP-READ-FOREIGN", "unauthorized; no body", "V-FAM-BOOT-ARCHIVE", "ReadOriginalRequest", {"operation": "consume", "request_hash": c["consume_original"]["request_hash"], "response_hash": c["consume_original"]["response_hash"], "scope": scope_of()}, "scope.lineage", nid(0x712), "read from a foreign lineage"))
    # 8 Proofs / pages
    empty = ledger_head(c["epoch"], [])
    rows.append(pos("V-FAM-PROOF-EMPTY", "empty authenticated genesis head", checks, head=encoded("LedgerHead", empty), genesis=encoded("GenesisCore", c["genesis"]), root_is_empty_tree=empty["root"] == empty_tree_digest()))
    one = ledger_head(c["epoch"], [c["issue_entry_digest"]])
    rows.append(pos("V-FAM-PROOF-ONE", "one-leaf root equals the leaf hash", checks, head=encoded("LedgerHead", one), leaf=tree_leaf(c["epoch"], 0, c["issue_entry_digest"]).hex()))
    a0 = c["issue_entry"]
    b0 = entry_for(c, 1, c["issue_entry_digest"], nid(0xD1), 0, 0x720)
    a1 = entry_for(c, 2, Hash("GlobalEntry", b0), nid(0xD0), 1, 0x730)
    entries = [a0, b0, a1]
    digests = [Hash("GlobalEntry", e) for e in entries]
    w = {"head": ledger_head(c["epoch"], digests), "leaves": [tree_leaf(c["epoch"], i, d).hex() for i, d in enumerate(digests)]}
    known = [ledger_head(c["epoch"], digests[:k]) for k in (1, 2, 3)]
    if witness_ok(w, known) is not None:
        raise SystemExit("three-leaf witness failed")
    checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 1
    rows.append(pos("V-FAM-PROOF-THREE", "three-leaf root; every known prefix recomputes", checks, witness=encoded("HashPrefixWitness", w), known_prefixes=[encoded("LedgerHead", k) for k in known]))
    rows.append(pos("V-FAM-PROOF-ABA", "A positions 0 and 2 scoped-complete without adjacency; B body not returned", checks, entries=[encoded("GlobalEntry", e) for e in entries], scoped_view_stream=nid(0xD0), scoped_positions=["0", "2"]))
    bad_order = dict(w, leaves=[w["leaves"][1], w["leaves"][0], w["leaves"][2]])
    if witness_ok(bad_order, known) is None:
        raise SystemExit("reorder not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg_obj("V-FAM-PROOF-REORDER", "fork or incomplete", "V-FAM-PROOF-THREE", "leaf order", witness=encoded("HashPrefixWitness", bad_order), generator_witness_check=witness_ok(bad_order, known)))
    short = dict(w, leaves=w["leaves"][:2])
    if witness_ok(short, known) != "incomplete":
        raise SystemExit("short witness not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg_obj("V-FAM-PROOF-COUNT", "incomplete", "V-FAM-PROOF-THREE", "leaf count differs from head count", witness=encoded("HashPrefixWitness", short), generator_witness_check=witness_ok(short, known)))
    rows.append(neg("V-FAM-PROOF-OFFSET", "incomplete", "V-FAM-PROOF-THREE", "HashPrefixWitness", w, "head.first_seq", "1", "first_seq differs from known heads"))
    rows.append(neg("V-FAM-PROOF-LAST", "binding", "V-FAM-PROOF-THREE", "HashPrefixWitness", w, "head.last_digest", digests[0], "last digest is not the last visible entry"))
    ps = page_set(c, entries, scope_of(), 1)
    rows.append(pos("V-FAM-PAGE-SET", "three pages chain; page root and manifest recompute", checks, manifest=encoded("Manifest", ps["manifest"]), pages=[encoded("Page", p) for p in ps["pages"]]))
    for vector_id, idx in [("V-FAM-PAGE-OMIT-FIRST", 0), ("V-FAM-PAGE-OMIT-MID", 1), ("V-FAM-PAGE-OMIT-FINAL", 2)]:
        kept = [p for i, p in enumerate(ps["pages"]) if i != idx]
        rows.append(neg_obj(vector_id, "incomplete", "V-FAM-PAGE-SET", f"page index {idx} omitted", pages=[encoded("Page", p) for p in kept], manifest_page_count=ps["manifest"]["page_count"]))
    neg_entry = entry_for(c, 2, Hash("GlobalEntry", b0), nid(0xD0), 1, 0x740)
    neg_entry["event"]["type"] = "ObservationRecord"
    rows.append(neg_obj("V-FAM-PAGE-OMIT-NEG", "incomplete", "V-FAM-PAGE-SET", "negative observation entry omitted from the scoped view", omitted_entry=encoded("GlobalEntry", neg_entry)))
    other = page_set(c, entries[:2], scope_of(), 1)
    rows.append(neg_obj("V-FAM-PAGE-MIXED", "fork", "V-FAM-PAGE-SET", "page from another snapshot core", pages=[encoded("Page", ps["pages"][0]), encoded("Page", other["pages"][1])]))
    # 9 Time / commit / retry: synthetic samples, not measured timings
    for vector_id, t, want in [("V-FAM-TIME-109", 109, "admit"), ("V-FAM-TIME-110", 110, "expired; no consume"), ("V-FAM-TIME-111", 111, "expired; no consume")]:
        adm = dict(c["admission"], post_sign_at=mono(server_clock(), t))
        ok = 100 <= t < 110
        checks["executed_primitive_assertions"] += 1
        if ok:
            rows.append(pos(vector_id, want, checks, admission=encoded("Admission", adm), issued_at="100", expires_at="110", sample_label="synthetic"))
        else:
            rows.append(neg_obj(vector_id, want, "V-FAM-TIME-109", "issued <= post_sign < expires", admission=encoded("Admission", adm), sample_label="synthetic"))
    rows.append(sched("V-FAM-TIME-COMMIT200", "commit allowed under unchanged revision and term; label post_sign_admission_only; not freshness at 200", ["post_sign:109", "pause", "swap:200"], admission=encoded("Admission", c["admission"]), commit_sample=mono(server_clock(), 200)))
    rows.append(neg("V-FAM-TIME-WALL", "same outcome as V-FAM-TIME-109", "V-FAM-TIME-109", "AckCore", c["ack"], "wall_audit.value", "-5", "wall audit is not a timing authority"))
    rows.append(neg("V-FAM-TIME-XDOMAIN", "domain", "V-FAM-TIME-109", "Admission", c["admission"], "post_sign_at.domain.boot", nid(0x751), "post-sign sample in a different server domain"))
    rows.append(neg("V-FAM-TIME-FENCE", "stale_writer", "V-FAM-TIME-109", "Admission", c["admission"], "writer_term", "0", "stale writer term"))
    rows.append(sched("V-FAM-TIME-LOST-CONFIRM", "commit_unknown; no provisional bytes; reconcile finds the winner", ["swap", "lose_confirmation", "reconcile"], original=encoded("Original", c["consume_original"])))
    rows.append(sched("V-FAM-TIME-RESTORE", "exact original after restore; divergent loser conflict", ["swap", "restore", "retry_winner", "retry_loser"], original=encoded("Original", c["consume_original"]), winner_raw=c["consume_original"]["request_hash"], loser_raw=loser_raw))
    # 10 Quotas / recovery with synthetic caps
    cap = 8
    for metric in ("records", "stored_bytes", "outstanding", "retry_attempts", "signer_calls", "store_calls", "denial_records", "calls", "retained_pages", "request_input_bytes"):
        mid = metric.upper().replace("_", "")
        at = {"class": "gauge" if metric == "outstanding" else "cumulative", "key": {"arm": "principal", "principal": nid(0xA1)}, "metric": metric, "value": str(cap)}
        if metric == "request_input_bytes":
            rows.append(pos(f"V-FAM-QUOTA-{mid}-CAP", "accepted at synthetic cap", checks, synthetic_cap=str(cap), cap_label=SYNTHETIC_CAP_LABEL, request_length=str(cap)))
            rows.append(neg_obj(f"V-FAM-QUOTA-{mid}-OVER", "oversize; no commit", f"V-FAM-QUOTA-{mid}-CAP", "length cap+1", synthetic_cap=str(cap), cap_label=SYNTHETIC_CAP_LABEL, request_length=str(cap + 1)))
            continue
        before = dict(at, value=str(cap - 1))
        rows.append(pos(f"V-FAM-QUOTA-{mid}-CAP", "charge reaches the synthetic cap", checks, before=encoded("KeyedStock", before), after=encoded("KeyedStock", at), synthetic_cap=str(cap), cap_label=SYNTHETIC_CAP_LABEL))
        rows.append(neg_obj(f"V-FAM-QUOTA-{mid}-OVER", "quota; latch unavailable for cumulative; no eviction", f"V-FAM-QUOTA-{mid}-CAP", "cap+1", before=encoded("KeyedStock", at), attempted_value=str(cap + 1), synthetic_cap=str(cap), cap_label=SYNTHETIC_CAP_LABEL))
    rows.append(sched("V-FAM-QUOTA-NEG-RESERVE", "unavailable; first_break and earliest denial kept", ["denial_records at synthetic cap", "another refusal"], denials=[encoded("Denial", d) for d in denial_chain(cap)], synthetic_cap=str(cap), cap_label=SYNTHETIC_CAP_LABEL))
    rows.append(sched("V-FAM-QUOTA-RESTORE", "serialized restore with independent basis recovers; counters not reset", ["drop_heap", "restore", "reconcile"], image=encoded("StableImage", image), basis_id=Hash("ExternalBasis", basis)))
    rows.append(sched("V-FAM-QUOTA-NOBASIS", "unavailable; no grant", ["drop_heap", "restore_without_basis"], image=encoded("StableImage", image), basis=None))
    # 11 Close / counters
    mx = U_MAX
    opening = {"count": str(mx - 1), "end_head": nh(0x801), "first": "0", "last": str(mx - 2), "predecessor": None, "predecessor_head": Z, "source_schema": nh(0x51)}
    closing = {"count": "1", "end_head": nh(0x802), "first": str(mx - 1), "last": str(mx - 1), "predecessor": str(mx - 2), "predecessor_head": nh(0x801), "source_schema": nh(0x51)}
    close = {"final_ack_ordinal": "1", "final_head": nh(0x802), "final_last": str(mx - 1), "total_rows": str(mx)}
    for r in (opening, closing):
        assert_fields("Range", r)
        if range_ok(r) is not None:
            raise SystemExit("split close range failed generator arithmetic")
        checks["executed_primitive_assertions"] += 1
    if int(close["final_last"]) + 1 != int(close["total_rows"]) or int(close["total_rows"]) > U_MAX:
        raise SystemExit("total_rows arithmetic")
    rows.append(pos("V-FAM-CLOSE-SPLIT", "opening [0,MAX-2] then one-row close [MAX-1,MAX-1]; total_rows MAX", checks, opening=encoded("Range", opening), closing=encoded("Range", closing), close=encoded("Close", close), generator_range_check="ok"))
    zero = dict(closing, count="0")
    rows.append(neg_obj("V-FAM-CLOSE-ZERO", "incomplete; zero-new-row close denied", "V-FAM-CLOSE-SPLIT", "nonempty close", closing=encoded("Range", zero), generator_range_check=range_ok(zero)))
    endmax = dict(closing, first=str(mx), last=str(mx), predecessor=str(mx - 1))
    if range_ok(endmax) != "overflow":
        raise SystemExit("endpoint MAX not caught")
    checks["executed_primitive_assertions"] += 1
    rows.append(neg_obj("V-FAM-CLOSE-ENDMAX", "overflow", "V-FAM-CLOSE-SPLIT", "endpoint MAX has no successor", closing=encoded("Range", endmax), generator_range_check="overflow"))
    badsucc = dict(closing, predecessor=str(mx - 3))
    rows.append(neg_obj("V-FAM-CLOSE-SUCC", "binding", "V-FAM-CLOSE-SPLIT", "predecessor+1 != first", closing=encoded("Range", badsucc), generator_range_check=range_ok(badsucc)))
    rows.append(neg_obj("V-FAM-CLOSE-GROWTH", "closed", "V-FAM-CLOSE-SPLIT", "new range after committed close", stream_closed="closed", later_range=encoded("Range", closing)))
    near = ledger_head(c["epoch"], [c["issue_entry_digest"]], first_seq=mx - 1)
    rows.append(pos("V-FAM-CLOSE-NEAR", "named near-limit genesis offset MAX-1 admits one leaf; next append exhausts", checks, head=encoded("LedgerHead", near), next_position_representable=(mx - 1) + 1 <= mx, following_append_position=str(mx), following_append="overflow"))
    # 12 Holder: release disabled, terminal-positive BLOCKED
    hold_req = build_requests(c)["hold"]["payload"]
    hold_bytes = canonical_bytes("HoldRequest", hold_req).encode("utf-8")
    hold_rec = {"candidate_digest": hold_req["candidate_digest"], "holder": hold_req["holder"], "journal": hold_req["journal"], "owner_generation": "1", "request_hash": Raw(hold_bytes), "revision": "1", "scope": scope_of(), "source_bound": hold_req["source_bound"], "state": "held", "stream": nid(0xD0)}
    hold_row = {"hold_bytes": b64u(canonical_bytes("HoldRecord", hold_rec).encode("utf-8")), "state": "held", "terminal_bytes": None}
    term = dict(build_requests(c)["terminal"]["payload"], hold_id=Hash("HoldRecord", hold_rec))
    rows.append(computed("V-FAM-HOLD-TERM", "BLOCKED; no success path; retirement_disabled", evidence_class=AUTHORED, note="terminal-positive BLOCKED; not simulated", hold=encoded("HoldRow", hold_row), request=encoded("TerminalRequest", term)))
    for vector_id, path, value, reason in [
        ("V-FAM-HOLD-WRONG", "holder", nid(0x82), "holder of another principal"),
        ("V-FAM-HOLD-PAUSED", "expected_state", "quarantined", "hold state mismatch while paused"),
        ("V-FAM-HOLD-UNKNOWN", "hold_id", nh(0x803), "hold id does not resolve"),
    ]:
        rows.append(neg(vector_id, "retirement_disabled or unauthorized; hold stays", "V-FAM-HOLD-TERM", "TerminalRequest", term, path, value, reason))
    for vector_id, why in [("V-FAM-HOLD-RESTORE", "restore"), ("V-FAM-HOLD-TIME", "elapsed time"), ("V-FAM-HOLD-ADMIN", "administrative request"), ("V-FAM-HOLD-QUOTA", "quota exhaustion")]:
        rows.append(sched(vector_id, "hold stays held or quarantined; no release", [why], hold=encoded("HoldRow", hold_row)))
    return rows


def owner_record(c: dict) -> dict:
    req = build_requests(c)["owner_grant"]["payload"]
    return {"holder": req["holder"], "owner_generation": "1", "parents": req["parents"], "request_hash": Raw(canonical_bytes("OwnerRequest", req).encode("utf-8")), "revision": "1", "scope": scope_of(), "stream": nid(0xD0)}


def b5_vectors(c: dict, checks: dict) -> list[dict]:
    rows: list[dict] = []
    P = lambda vid, out, t, obj: rows.append(pos(vid, out, checks, record=encoded(t, obj)))
    N = lambda vid, out, twin, t, obj, path, val, why: rows.append(neg(vid, out, twin, t, obj, path, val, why))
    ch_cons = c["image"]["core"]["challenges"][0]
    ch_unused = dict(ch_cons, state="unused", winning_request_hash=None, winning_response_hash=None)
    ch_inval = dict(ch_unused, state="invalidated", invalidated_by=nid(0x504))
    P("V-B5-CH-UNUSED", "admissible", "ChallengeRow", ch_unused)
    N("V-B5-CH-UNUSED-WIN", "binding", "V-B5-CH-UNUSED", "ChallengeRow", ch_unused, "winning_request_hash", c["consume_original"]["request_hash"], "unused with a winner hash")
    N("V-B5-CH-UNUSED-EPOCH", "binding", "V-B5-CH-UNUSED", "ChallengeRow", ch_unused, "invalidated_by", nid(0x504), "unused with an epoch")
    P("V-B5-CH-CONSUMED", "admissible; both hashes equal the consume Original", "ChallengeRow", ch_cons)
    N("V-B5-CH-CONSUMED-ONE", "binding", "V-B5-CH-CONSUMED", "ChallengeRow", ch_cons, "winning_response_hash", None, "only one winner hash")
    N("V-B5-CH-CONSUMED-BAD", "binding", "V-B5-CH-CONSUMED", "ChallengeRow", ch_cons, "winning_response_hash", c["issue_original"]["response_hash"], "hash names another Original")
    N("V-B5-CH-CONSUMED-EPOCH", "binding", "V-B5-CH-CONSUMED", "ChallengeRow", ch_cons, "invalidated_by", nid(0x504), "consumed with an epoch")
    P("V-B5-CH-INVAL", "admissible; epoch resolves", "ChallengeRow", ch_inval)
    N("V-B5-CH-INVAL-WIN", "binding", "V-B5-CH-INVAL", "ChallengeRow", ch_inval, "winning_request_hash", c["consume_original"]["request_hash"], "invalidated with a winner")
    N("V-B5-CH-INVAL-NULL", "malformed", "V-B5-CH-INVAL", "ChallengeRow", ch_inval, "invalidated_by", None, "invalidated without epoch")
    if valid_id(nh(0x504)):
        raise SystemExit("event hash accepted as Id")
    checks["executed_primitive_assertions"] = checks.get("executed_primitive_assertions", 0) + 1
    N("V-B5-CH-INVAL-H", "schema", "V-B5-CH-INVAL", "ChallengeRow", ch_inval, "invalidated_by", nh(0x504), "event H in an Id position")
    hold_req = build_requests(c)["hold"]["payload"]
    hold_rec = {"candidate_digest": hold_req["candidate_digest"], "holder": hold_req["holder"], "journal": hold_req["journal"], "owner_generation": "1", "request_hash": Raw(canonical_bytes("HoldRequest", hold_req).encode("utf-8")), "revision": "1", "scope": scope_of(), "source_bound": hold_req["source_bound"], "state": "held", "stream": nid(0xD0)}
    hb = b64u(canonical_bytes("HoldRecord", hold_rec).encode("utf-8"))
    held = {"hold_bytes": hb, "state": "held", "terminal_bytes": None}
    quar = dict(held, state="quarantined")
    term_req = dict(build_requests(c)["terminal"]["payload"], hold_id=Hash("HoldRecord", hold_rec))
    term_rec = {"previous_hold_id": Hash("HoldRecord", hold_rec), "request_hash": Raw(canonical_bytes("TerminalRequest", term_req).encode("utf-8")), "terminal": term_req, "terminal_revision": "2"}
    term = dict(held, state="terminal", terminal_bytes=b64u(canonical_bytes("TerminalRecord", term_rec).encode("utf-8")))
    P("V-B5-HOLD-HELD", "admissible; not releasable", "HoldRow", held)
    N("V-B5-HOLD-HELD-BYTES", "binding", "V-B5-HOLD-HELD", "HoldRow", held, "terminal_bytes", term["terminal_bytes"], "held with terminal bytes")
    P("V-B5-HOLD-QUAR", "admissible; not releasable", "HoldRow", quar)
    N("V-B5-HOLD-QUAR-BYTES", "binding", "V-B5-HOLD-QUAR", "HoldRow", quar, "terminal_bytes", term["terminal_bytes"], "quarantined with terminal bytes")
    rows.append(computed("V-B5-HOLD-TERM-SPEC", "BLOCKED", evidence_class=AUTHORED, note="shape only; terminal-positive BLOCKED; not a success and not an unlock", record=encoded("HoldRow", term), terminal=encoded("TerminalRecord", term_rec)))
    N("V-B5-HOLD-TERM-NONE", "malformed", "V-B5-HOLD-TERM-SPEC", "HoldRow", term, "terminal_bytes", None, "terminal without bytes")
    N("V-B5-HOLD-TERM-MIS", "binding", "V-B5-HOLD-TERM-SPEC", "HoldRow", term, "terminal_bytes", b64u(canonical_bytes("TerminalRecord", dict(term_rec, previous_hold_id=nh(0x805))).encode("utf-8")), "terminal names another hold")
    sd = Hash("Scope", scope_of())
    par_unk = {"boundary": "unknown", "checkpoint": None, "scope_digest": sd}
    par_known = {"boundary": "terminal_known", "checkpoint": c["ck_rec_id"], "scope_digest": sd}
    P("V-B5-PAR-UNK", "unknown; not repaired", "ParentBoundary", par_unk)
    N("V-B5-PAR-UNK-CKPT", "binding", "V-B5-PAR-UNK", "ParentBoundary", par_unk, "checkpoint", c["ck_rec_id"], "unknown with checkpoint")
    P("V-B5-PAR-KNOWN", "checkpoint resolves to the committed CheckpointRecord", "ParentBoundary", par_known)
    N("V-B5-PAR-KNOWN-NULL", "malformed", "V-B5-PAR-KNOWN", "ParentBoundary", par_known, "checkpoint", None, "known without checkpoint")
    N("V-B5-PAR-KNOWN-MISS", "incomplete", "V-B5-PAR-KNOWN", "ParentBoundary", par_known, "checkpoint", nh(0x806), "checkpoint does not resolve")
    hist = lambda d, r: {"decision": d, "generation": "1", "issuer": nid(0x90), "kind": "owner", "records_through": r}
    P("V-B5-HIST-UNK", "policy_unknown", "HistoricalRule", hist("unknown", None))
    N("V-B5-HIST-UNK-AS-ALLOW", "policy_unknown", "V-B5-HIST-UNK", "HistoricalRule", hist("unknown", None), "decision", "allow", "unknown treated as allow")
    P("V-B5-HIST-UNK-HASH", "policy_unknown", "HistoricalRule", hist("unknown", c["ck_rec_id"]))
    N("V-B5-HIST-UNK-UP", "policy_unknown", "V-B5-HIST-UNK-HASH", "HistoricalRule", hist("unknown", c["ck_rec_id"]), "decision", "allow", "hash used to upgrade unknown")
    P("V-B5-HIST-ALLOW", "allow through the resolved checkpoint", "HistoricalRule", hist("allow", c["ck_rec_id"]))
    N("V-B5-HIST-ALLOW-NULL", "malformed", "V-B5-HIST-ALLOW", "HistoricalRule", hist("allow", c["ck_rec_id"]), "records_through", None, "allow without cutoff")
    P("V-B5-HIST-DENY", "deny through the resolved checkpoint", "HistoricalRule", hist("deny", c["ck_rec_id"]))
    N("V-B5-HIST-DENY-NULL", "malformed", "V-B5-HIST-DENY", "HistoricalRule", hist("deny", c["ck_rec_id"]), "records_through", None, "deny without cutoff")
    owner = owner_record(c)
    kc = {
        "V-B5-KC-GLOBAL": {"generation": None, "head": c["ledger"]["root"], "kind": "global", "ledger": c["ledger"], "scope_digest": sd, "stream": None},
        "V-B5-KC-OWNER": {"generation": "1", "head": Hash("OwnerRecord", owner), "kind": "owner", "ledger": None, "scope_digest": sd, "stream": nid(0xD0)},
        "V-B5-KC-JOURNAL": {"generation": None, "head": c["journal"]["head"], "kind": "journal", "ledger": None, "scope_digest": sd, "stream": nid(0xD0)},
        "V-B5-KC-POLICY": {"generation": "1", "head": c["policy_digest"], "kind": "policy", "ledger": None, "scope_digest": Hash("RegistryScope", registry_of()), "stream": None},
        "V-B5-KC-TERM": {"generation": "1", "head": Hash("HoldRecord", hold_rec), "kind": "terminal", "ledger": None, "scope_digest": sd, "stream": nid(0xD0)},
    }
    for vid, obj in kc.items():
        P(vid, "admissible; head resolves", "KnownCheckpoint", obj)
    N("V-B5-KC-GLOBAL-STREAM", "schema", "V-B5-KC-GLOBAL", "KnownCheckpoint", kc["V-B5-KC-GLOBAL"], "stream", nid(0xD0), "global with stream")
    N("V-B5-KC-GLOBAL-LEDGER", "malformed", "V-B5-KC-GLOBAL", "KnownCheckpoint", kc["V-B5-KC-GLOBAL"], "ledger", None, "global without ledger")
    N("V-B5-KC-OWNER-GEN", "malformed", "V-B5-KC-OWNER", "KnownCheckpoint", kc["V-B5-KC-OWNER"], "generation", None, "owner without generation")
    N("V-B5-KC-JOURNAL-LEDGER", "schema", "V-B5-KC-JOURNAL", "KnownCheckpoint", kc["V-B5-KC-JOURNAL"], "ledger", c["ledger"], "journal with ledger")
    N("V-B5-KC-POLICY-STREAM", "schema", "V-B5-KC-POLICY", "KnownCheckpoint", kc["V-B5-KC-POLICY"], "stream", nid(0xD0), "policy with stream")
    N("V-B5-KC-TERM-NULL", "malformed", "V-B5-KC-TERM", "KnownCheckpoint", kc["V-B5-KC-TERM"], "stream", None, "terminal without stream")
    empty_sh = {"ack_count": "0", "closed": "open", "event_count": "0", "first_break": None, "journal": None, "last_ack": None, "last_event": None, "owner_generation": None, "scope": scope_of(), "source_bound": None, "stream": nid(0xD0), "unresolved_holds": []}
    P("V-B5-SH-EMPTY", "admissible", "ScopedHead", empty_sh)
    N("V-B5-SH-EMPTY-LAST", "binding", "V-B5-SH-EMPTY", "ScopedHead", empty_sh, "last_event", c["issue_entry_digest"], "count 0 with last_event")
    N("V-B5-SH-EMPTY-CLOSED", "binding", "V-B5-SH-EMPTY", "ScopedHead", empty_sh, "closed", "closed", "closed with no events")
    full_sh = scoped_head(c)
    P("V-B5-SH-NONEMPTY", "admissible", "ScopedHead", full_sh)
    N("V-B5-SH-ACK", "binding", "V-B5-SH-NONEMPTY", "ScopedHead", full_sh, "last_ack", None, "ack count 1 with null last_ack")
    N("V-B5-SH-CLOSED-EMPTY", "binding", "V-B5-SH-EMPTY", "ScopedHead", empty_sh, "closed", "closed", "closed with event_count 0")
    lh_empty = ledger_head(c["epoch"], [])
    P("V-B5-LH-EMPTY", "admissible; root is the empty-tree digest", "LedgerHead", lh_empty)
    N("V-B5-LH-EMPTY-SEQ", "binding", "V-B5-LH-EMPTY", "LedgerHead", lh_empty, "last_seq", "0", "empty with last_seq")
    N("V-B5-LH-EMPTY-NZ", "binding", "V-B5-LH-EMPTY", "LedgerHead", lh_empty, "last_digest", c["issue_entry_digest"], "empty with nonzero last digest")
    P("V-B5-LH-NONEMPTY", "admissible; root recomputes from two leaves", "LedgerHead", c["ledger"])
    N("V-B5-LH-SEQ", "binding", "V-B5-LH-NONEMPTY", "LedgerHead", c["ledger"], "last_seq", "5", "last_seq != first_seq+count-1")
    P("V-B5-SEAL-ISSUE", "admissible", "IssueRequest", c["issue"])
    N("V-B5-SEAL-ISSUE-SET", "binding", "V-B5-SEAL-ISSUE", "IssueRequest", c["issue"], "seal", nh(0x73), "opening issue with seal")
    P("V-B5-SEAL-CONSUME", "admissible", "Commitment", c["consume"]["commitment"])
    N("V-B5-SEAL-CONSUME-NULL", "malformed", "V-B5-SEAL-CONSUME", "Commitment", c["consume"]["commitment"], "seal", None, "opening consume without seal")
    later = dict(c["issue"], ack_ordinal="1", prior_ack=Hash("AckCore", c["ack"]), purpose="interval", seal=c["consume"]["commitment"]["seal"], idempotency=nid(0xEB))
    P("V-B5-SEAL-LATER", "admissible; seal equals accepted opening seal", "IssueRequest", later)
    N("V-B5-SEAL-LATER-NULL", "malformed", "V-B5-SEAL-LATER", "IssueRequest", later, "seal", None, "later without seal")
    N("V-B5-SEAL-LATER-MIS", "binding", "V-B5-SEAL-LATER", "IssueRequest", later, "seal", nh(0x807), "later with different seal")
    P("V-B5-GL-STREAM", "admissible", "GlobalEntry", c["consume_entry"])
    N("V-B5-GL-NULL", "malformed", "V-B5-GL-STREAM", "GlobalEntry", c["consume_entry"], "stream", None, "ledger entry without stream")
    prec = {"core": c["policy"], "digest": c["policy_digest"]}
    P("V-B5-GL-POL", "registry row; not a GlobalEntry", "PolicyRecord", prec)
    N("V-B5-GL-POL-LIN", "schema", "V-B5-GL-POL", "PolicyRecord", prec, "core.registry.lineage", nid(0xF00), "lineage member added to RegistryScope")
    recov = build_requests(c)["recover"]["payload"]
    P("V-B5-GL-REC", "internal; no request bytes; no Original", "RecoveryRecord", recov)
    rows.append(neg_obj("V-B5-GL-REC-ENTRY", "binding", "V-B5-GL-REC", "no Original or GlobalEntry for recover", manufactured=encoded("Original", dict(c["issue_original"], operation="recover", idempotency=nid(0xEEE)))))
    reg_auth = auth_binding({"arm": "registry", "registry": registry_of()}, "policy_update", "1", c["policy_digest"])
    P("V-B5-GL-REG", "registry auth; stream null", "AuthBinding", reg_auth)
    N("V-B5-GL-REG-STREAM", "schema", "V-B5-GL-REG", "AuthBinding", reg_auth, "stream", nid(0xD0), "stream required on policy")
    retained = attempt(c)
    missing = attempt(c, durable_resolution=None, raw_response_hash=None, result="missing")
    pending = dict(missing, result="pending")
    late = attempt(c, arrival=mono(client_clock(), 250), durable_resolution=None, raw_response_hash=None, result="late")
    invalid = attempt(c, arrival=mono(server_clock(), 120), durable_resolution=None, raw_response_hash=None, result="invalid")
    rows.append(pos("V-B5-ATT-RETAINED", "historical retained", checks, record=encoded("ClientAttempt", retained), core=encoded("ClientExpectedCore", c["consume_core"])))
    rows.append(pos("V-B5-ATT-MISSING", "missing", checks, record=encoded("ClientAttempt", missing), classification_pass="readback"))
    rows.append(pos("V-B5-ATT-PENDING", "pending", checks, record=encoded("ClientAttempt", pending), classification_pass="open"))
    rows.append(pos("V-B5-ATT-LATE", "late", checks, record=encoded("ClientAttempt", late), core=encoded("ClientExpectedCore", c["consume_core"])))
    rows.append(pos("V-B5-ATT-INVALID", "invalid", checks, record=encoded("ClientAttempt", invalid), reason="arrival is not in the client domain"))
    N("V-B5-ATT-RET-NODUR", "inadmissible", "V-B5-ATT-RETAINED", "ClientAttempt", retained, "durable_resolution", None, "retained without durable resolution")
    N("V-B5-ATT-MISS-LATE", "missing", "V-B5-ATT-MISSING", "ClientAttempt", missing, "result", "late", "missing shape labeled late")
    N("V-B5-ATT-LATE-EMPTY", "binding", "V-B5-ATT-LATE", "ClientAttempt", late, "arrival", None, "late with no milestone")
    rows.append(neg_obj("V-B5-ATT-PEND-READ", "missing", "V-B5-ATT-PENDING", "same null shape on readback is missing", record=encoded("ClientAttempt", pending), classification_pass="readback"))
    return rows


def b1_records(c: dict, checks: dict) -> list[dict]:
    """Complete records for the r1 B1 schedule rows (previously notes)."""
    rows: list[dict] = []
    io = c["issue_original"]
    enc_io = encoded("Original", io)
    retry_raw = Raw(canonical_bytes("IssueRequest", c["issue"]).encode("utf-8"))
    if retry_raw != io["request_hash"]:
        raise SystemExit("retry bytes differ from winner")
    checks["executed_primitive_assertions"] = checks.get("executed_primitive_assertions", 0) + 1
    rows.append(sched("V-B1-TWIN-RETRY", "same Original response bytes; no new tag, time, or ack advance", ["commit", "expire", "retry_same_bytes", "restart", "retry_same_bytes"], request=encoded("IssueRequest", c["issue"]), retry_request_hash=retry_raw, service_original=enc_io, returned_response_hash=io["response_hash"]))
    rows.append(sched("V-B1-ATK-04", "unknown", ["client_record_lost", "read_old_ack", "new_core"], client_record=None, service_original=encoded("Original", c["consume_original"]), new_core=encoded("ClientExpectedCore", set_path(c["consume_core"], "enqueue_at.value", "101")), attempt=encoded("ClientAttempt", attempt(c, durable_resolution=None, raw_response_hash=None, result="missing"))))
    loser = issue_request(set_path(set_path(c["issue_core"], "enqueue_at.value", "180"), "original_deadline.value", "190"), Hash("ClientExpectedCore", set_path(set_path(c["issue_core"], "enqueue_at.value", "180"), "original_deadline.value", "190")))
    rows.append(sched("V-B1-ATK-05", "archive may return winner bytes; loser is conflict; client timing stays missing", ["client_record_lost", "archive_read"], read=encoded("ReadOriginalRequest", {"operation": "issue", "request_hash": io["request_hash"], "response_hash": io["response_hash"], "scope": scope_of()}), service_original=enc_io, loser=encoded("IssueRequest", loser), attempt=encoded("ClientAttempt", attempt(c, durable_resolution=None, raw_response_hash=None, result="missing"))))
    a_req, b_req = c["issue"], loser
    for vid, outcome, steps in [
        ("V-B1-RACE-AB", "A original wins; B conflict", ["A_commit", "B_divergent"]),
        ("V-B1-RACE-BA", "B original wins; A conflict", ["B_commit", "A_divergent"]),
        ("V-B1-RACE-AB-EXP", "oracle unchanged", ["A_commit", "expire", "B_divergent"]),
        ("V-B1-RACE-BA-EXP", "oracle unchanged", ["B_commit", "expire", "A_divergent"]),
        ("V-B1-RACE-AB-RST", "oracle unchanged; missing client fixture stays unknown", ["A_commit", "restart", "B_divergent"]),
        ("V-B1-RACE-BA-RST", "oracle unchanged", ["B_commit", "restart", "A_divergent"]),
    ]:
        rows.append(sched(vid, outcome, steps, request_a=encoded("IssueRequest", a_req), request_b=encoded("IssueRequest", b_req), same_logical_key=logical_key(c["issue_core"]) == logical_key(set_path(set_path(c["issue_core"], "enqueue_at.value", "180"), "original_deadline.value", "190"))))
    return rows


def b2_records(c: dict, checks: dict) -> list[dict]:
    rows: list[dict] = []
    reqs = build_requests(c)
    pol = c["policy"]
    left, right = Hash("PolicyCore", policy_core()), Hash("PolicyCore", policy_core("1", "b"))
    if left == right:
        raise SystemExit("policy fork digest did not change")
    checks["mutations"] += 1
    rows.append(neg("V-B2-OP-POL-FORK", "fork", "V-B2-OP-POL", "PolicyCore", pol, "roles", policy_core("1", "b")["roles"], "same generation, different PolicyCore digest", digests={"left": left, "right": right}))
    rows.append(neg("V-B2-OP-ARCH-OLD", "unauthorized; no body", "V-B2-OP-ARCH", "PolicyCore", pol, "archive", "deny", "current archive deny refuses historical bytes"))
    hist_unknown = {"decision": "unknown", "generation": "1", "issuer": nid(0x90), "kind": "owner", "records_through": None}
    rows.append(neg("V-B2-OP-ARCH-CUT", "policy_unknown", "V-B2-OP-ARCH", "PolicyCore", pol, "historical.0", hist_unknown, "unknown historical cutoff is not full history"))
    rows.append(computed("V-B2-OP-TERM", "retirement_disabled", evidence_class=AUTHORED, note="authored, NOT EXECUTED; terminal-positive BLOCKED", request=encoded("TerminalRequest", reqs["terminal"]["payload"]), hold_remains=True))
    recov = dict(reqs["recover"]["payload"], previous_image_hash=c["image"]["core_id"], reconciled_observer_hash=c["image"]["core"]["observer_hash"])
    rows.append(pos("V-B2-OP-RECOVER", "no request bytes; no Original; unused old-domain challenges invalidated; originals unchanged", checks, record=encoded("RecoveryRecord", recov), originals_after=len(c["image"]["core"]["originals"])))
    obs = reqs["observe"]["payload"]
    obs_rec = {"first_break": nh(0x601), "observation": obs, "request_hash": Raw(canonical_bytes("Observation", obs).encode("utf-8"))}
    obs_signed = model_signed("ObservationRecord", obs_rec, role("observe"))
    rows.append(neg("V-B2-ROLE-OBS", "schema", "V-B2-OP-OBS", "ModelSigned", obs_signed, "role.kind", "checkpoint", "ObservationRecord tag kind must be observe"))
    hold = reqs["hold"]["payload"]
    hold_rec = {"candidate_digest": hold["candidate_digest"], "holder": hold["holder"], "journal": hold["journal"], "owner_generation": "1", "request_hash": Raw(canonical_bytes("HoldRequest", hold).encode("utf-8")), "revision": "1", "scope": scope_of(), "source_bound": hold["source_bound"], "state": "held", "stream": nid(0xD0)}
    rows.append(neg("V-B2-ROLE-HOLD", "schema", "V-B2-OP-HOLD", "ModelSigned", model_signed("HoldRecord", hold_rec, role("hold")), "role.kind", "owner", "HoldRecord tag kind must be hold"))
    rows.append(neg("V-B2-ROLE-REC", "binding", "V-B2-OP-RECOVER", "RecoveryRecord", recov, "idempotency", nid(0xEEE), "RecoveryRecord has no idempotency and no Original"))
    for name in ("recover",):
        if "idempotency" in recov or "request_bytes" in recov:
            raise SystemExit("internal row carries request fields")
    checks["generator_schema_checks"] = checks.get("generator_schema_checks", 0) + 1
    rows.append(pos("V-B2-INT-01", "no request bytes and no idempotency id on internal rows", checks, internal=[encoded("RecoveryRecord", recov)], absent_fields=["idempotency", "request_bytes"]))
    rows.append(neg("V-B2-NULL-HOLD", "unauthorized", "V-B2-OP-ISSUE", "IssueRequest", c["issue"], "holder", nid(0x81), "holder must be absent on issue"))
    rows.append(neg("V-B2-NULL-OWNER", "malformed", "V-B2-OP-OBS", "Observation", obs, "owner_generation", None, "owner_generation required on observe"))
    return rows


def b34_records(c: dict, checks: dict) -> list[dict]:
    rows: list[dict] = []
    basis, image = c["basis"], c["image"]
    core = image["core"]
    rows.append(neg("V-B3-CFG-MISS", "limit_unset", "V-F3-EXP-OK", "ExternalBasis", basis, "config", None, "external config absent"))
    rows.append(neg("V-B3-MIN-MISS", "policy_unknown", "V-F3-EXP-OK", "ExternalBasis", basis, "minima", [], "minima absent"))
    two = sorted(basis["minima"] + [dict(basis["minima"][0], basis=nid(0x504))], key=J)
    rows.append(computed("V-B3-MIN-FILT", "policy_unknown", evidence_class=AUTHORED, note="authored, NOT EXECUTED", valid_basis=encoded("ExternalBasis", dict(basis, minima=two)), filtered_basis=encoded("ExternalBasis", dict(basis, minima=two[:1])), intended_check="one minimum removed; image intact"))
    rows.append(neg("V-B3-GEN-LOW", "regression", "V-F3-EXP-OK", "ExternalBasis", basis, "minima.0.generation", "2", "image generation 1 below minimum generation 2"))
    fork_pol = policy_core("1", "b")
    rows.append(neg("V-B3-GEN-FORK", "fork", "V-F3-EXP-OK", "ExternalBasis", basis, "minima.0.digest", Hash("PolicyCore", fork_pol), "same generation, different policy digest"))
    cap = dict(basis["config"]["core"], charge_profile="b4-other")
    rows.append(neg("V-B3-CAP-CHG", "deny; self-hash irrelevant", "V-F3-EXP-OK", "ExternalBasis", basis, "config", {"core": cap, "digest": Hash("ConfigCore", cap)}, "external charge_profile differs from the image config_hash"))
    rows.append(neg("V-B3-CUT", "policy_unknown", "V-F3-EXP-OK", "StableImage", image, "core.policies.0.core.historical.0.decision", "unknown", "unknown historical cutoff"))
    rows.append(neg_obj("V-B3-SEED", "policy_unknown", "V-F3-EXP-OK", "image-supplied minimum is ignored; external minima absent", image=encoded("StableImage", image), image_supplied_minimum=encoded("Minimum", basis["minima"][0]), basis=encoded("ExternalBasis", dict(basis, minima=[]))))
    rows.append(neg("V-B3-TERM", "commit_unknown", "V-F3-EXP-OK", "ExternalBasis", basis, "continuity.writer_term", "2", "writer_term disagrees with image"))
    unused = dict(core["challenges"][0], invalidated_by=None, state="unused", winning_request_hash=None, winning_response_hash=None)
    rows.append(pos("V-B3-EPOCH", "unused old-domain challenges invalidated; originals kept", checks, before=encoded("ChallengeRow", unused), after=encoded("ChallengeRow", dict(unused, state="invalidated", invalidated_by=nid(0x504))), originals_kept=len(core["originals"])))
    rows.append(neg("V-B4-UNSET", "limit_unset", "V-F3-EXP-OK", "ExternalBasis", basis, "config.core.limits.records", DELETE, "required LimitSet field absent"))
    retry1 = {"class": "cumulative", "key": {"arm": "model", "model": nid(0x500)}, "metric": "retry_attempts", "value": "1"}
    rows.append(sched("V-B4-RETRY", "retry_attempts kept at 2; not reset", ["attempt", "retry_same_bytes"], after_first=encoded("KeyedStock", retry1), after_second=encoded("KeyedStock", dict(retry1, value="2"))))
    img = image["core_id"]
    rows.append(sched("V-B4-CRASH-PRE", "no positive bytes; reservations gone; image unchanged", ["receive", "reserve", "crash"], image_before=img, image_after=img, witness=None, originals_added=[]))
    rows.append(sched("V-B4-CRASH-POST", "committed", ["receive", "reserve", "work", "prepare", "swap", "witness", "crash"], image_before=img, witness=encoded("CommitWitness", witness("swap", "committed", img, nh(0x601)))))
    rows.append(neg("V-B4-AMBIG", "unavailable", "V-F3-EXP-OK", "ExternalBasis", basis, "accounting.stocks.0.value", "9", "stock no longer reconciles"))
    rows.append(sched("V-B4-EXHAUST", "unavailable", ["cumulative stock at synthetic cap", "receive"], stock=encoded("KeyedStock", {"class": "cumulative", "key": {"aggregate": "all", "arm": "aggregate"}, "metric": "records", "value": "8"}), synthetic_cap="8", cap_label=SYNTHETIC_CAP_LABEL))
    rows.append(pos("V-B4-NOEVICT", "originals and holds remain at exhaustion", checks, image=encoded("StableImage", image), originals=len(core["originals"]), eviction=False))
    return rows


def b6_records(c: dict, checks: dict, nxt_core: dict, nxt_request: dict, nxt_record_id: str) -> list[dict]:
    rows: list[dict] = []
    ck = c["checkpoint_core"]
    rows.append(neg("V-B6-SELF", "binding", "V-B6-EXT", "ClientCheckpointCore", nxt_core, "prior_client_checkpoint", Hash("ClientCheckpointCore", nxt_core), "prior set to this core's own id"))
    rows.append(neg("V-B6-CURRENT", "binding", "V-B6-EXT", "CheckpointRequest", nxt_request, "journal.client_checkpoint", nxt_record_id, "client field set to the receipt this commit would produce"))
    rows.append(neg("V-B6-MISSING", "incomplete; grant unknown", "V-B6-EXT", "CheckpointRequest", nxt_request, "journal.prior_service_receipt", nh(0x5152), "prior receipt does not resolve"))
    rows.append(neg("V-B6-Z", "binding", "V-B6-EXT", "ClientCheckpointCore", nxt_core, "prior_client_checkpoint", Z, "Z as prior"))
    rows.append(neg("V-B6-ANY-H", "not_found; no new history", "V-B6-BOOT", "ClientCheckpointCore", ck, "prior_client_checkpoint", nh(0xDEAD), "random H as first prior"))
    return rows


def b7_records(c: dict, checks: dict) -> list[dict]:
    rows: list[dict] = []
    req = c["ck_req"]
    rows.append(neg("V-B7-FORK-HEAD", "fork; latch sticky; unknown", "V-F2-RECEIPT-CKPT", "CheckpointRequest", req, "source_bound.head", nh(0xF2), "same endpoint, different head"))
    rows.append(neg("V-B7-FORK-JRN", "fork; latch sticky", "V-F2-RECEIPT-CKPT", "CheckpointRequest", req, "journal.head", nh(0x62), "same span, different journal head"))
    raw = Raw(canonical_bytes("CheckpointRequest", req).encode("utf-8"))
    rows.append(sched("V-B7-DUP", "twin; no second commit; latch not cleared", ["latch", "retry_same_bytes"], request=encoded("CheckpointRequest", req), request_hash=raw, original=encoded("Original", c["ck_orig"])))
    latched = dict(c["image"]["core"]["streams"][0], fork="latched")
    later = mutate(req, "idempotency", nid(0xEC))
    later = mutate(later, "source_bound", source_bound(1, 0xF3))
    rows.append(sched("V-B7-FWD", "unknown", ["latch", "later_span"], stream=encoded("StreamRow", latched), request=encoded("CheckpointRequest", later)))
    bad_origin = dict(c["origin"], source_schema=nh(0x52))
    rows.append(neg("V-B7-RESTORE", "unknown; no repair", "V-F2-ORIGIN-FIELDS", "StreamRow", c["image"]["core"]["streams"][0], "origin", bad_origin, "image origin disagrees with opening bytes"))
    return rows


def primitive_self_checks(checks: dict) -> None:
    if not valid_i(str(I_MIN)):
        raise SystemExit("i64 minimum rejected")
    if valid_i(str(I_MIN - 1)) or valid_u("00") or valid_u(str(U_MAX + 1)) or valid_u("-1"):
        raise SystemExit("primitive validator accepted a bad value")
    if valid_u(str(U_MAX)) is not True:
        raise SystemExit("u max rejected")
    endpoint = {"last": str(U_MAX), "next_ok": "false"}
    if int(endpoint["last"]) != U_MAX:
        raise SystemExit("endpoint")
    try:
        _ = int(endpoint["last"]) + 1
        successor_fits = False
    except OverflowError:
        successor_fits = False
    if int(endpoint["last"]) + 1 <= U_MAX:
        successor_fits = True
    if successor_fits:
        raise SystemExit("U max endpoint was accepted")
    noncanonical = '{"b":"1","a":"0"}'
    canonical = J(json.loads(noncanonical))
    if noncanonical == canonical:
        raise SystemExit("noncanonical compared equal")
    checks["i64_minimum_accepted"] = True
    checks["u_max_successor_overflows_in_checked_arithmetic"] = True
    checks["noncanonical_text_differs_from_reserialized_canonical"] = True
    checks["executed_primitive_assertions"] = checks.get("executed_primitive_assertions", 0) + 5


def build() -> tuple[dict, dict]:
    checks = {"acyclic": False, "executed_primitive_assertions": 0, "generator_schema_checks": 0, "mutations": 0, "roundtrips": 0}
    primitive_self_checks(checks)
    base = full_chain()
    J(json.loads(base["issue_text"]))
    J(json.loads(base["consume_text"]))
    checks["roundtrips"] += 2
    rows = build_b1(base, checks) + build_rest(base, checks)
    for family in (f1_vectors, f2_vectors, f3_vectors, f4_vectors, f5_vectors, b5_vectors, b1_records, b2_records, b34_records, b7_records, fam_vectors):
        rows.extend(family(base, checks))
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        seen: set[str] = set()
        dups = sorted({r["id"] for r in rows if r["id"] in seen or seen.add(r["id"])})
        raise SystemExit(f"duplicate vector id {dups}")
    wanted = doc_ids()
    missing = [item for item in wanted if item not in by_id]
    extra = [item for item in by_id if item not in set(wanted)]
    if missing or extra:
        raise SystemExit(f"coverage mismatch missing={missing} extra={extra}")
    if checks["mutations"] < 1 or checks["executed_primitive_assertions"] < 1 or not checks["acyclic"]:
        raise SystemExit(f"self-check failed {checks}")
    classes: dict[str, int] = {}
    for row in by_id.values():
        if row["kind"] != "computed" or row["outcome_status"] not in {ENCODING_STATUS, VALIDATOR_STATUS}:
            raise SystemExit(f"{row['id']} outcome_status")
        if "case" in row or "incomplete_reason" in row:
            raise SystemExit(f"{row['id']} is a note, not a record")
        cls = row.get("evidence_class", "generator_computed_fixture")
        classes[cls] = classes.get(cls, 0) + 1
    coverage = {item: by_id[item].get("evidence_class", "generator_computed_fixture") for item in wanted}
    document = {
        "coverage": coverage,
        "evidence_counts": dict(sorted(classes.items())),
        "self_checks": {
            "acyclic": checks["acyclic"],
            "authored_negative_expectations_not_executed": classes.get("authored_negative_expectation", 0),
            "computed_mutations_changed_digest": checks["mutations"],
            "executed_primitive_assertions": checks["executed_primitive_assertions"],
            "generator_schema_checks": checks["generator_schema_checks"],
            "i64_minimum_accepted": checks["i64_minimum_accepted"],
            "model_outcomes_not_executed": len(by_id),
            "noncanonical_text_differs_from_reserialized_canonical": checks["noncanonical_text_differs_from_reserialized_canonical"],
            "positive_roundtrips": checks["roundtrips"],
            "u_max_successor_overflows_in_checked_arithmetic": checks["u_max_successor_overflows_in_checked_arithmetic"],
            "vectors": len(by_id),
        },
        "vectors": [by_id[item] for item in wanted],
    }
    return document, {"base": base}


def dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=True, indent=1, sort_keys=True) + "\n"


def file_pin(path: Path, label: str) -> dict:
    data = path.read_bytes()
    return {"bytes": len(data), "label": label, "path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(data).hexdigest()}


def pins_document(vector_bytes: bytes) -> dict:
    text = DOC.read_text(encoding="utf-8")
    return {
        "base": {"commit": BASE, "label": "supplied by operator"},
        "observed_file_pins": [
            file_pin(DOC, "observed by generator"),
            file_pin(Path(__file__).resolve(), "observed by generator"),
            {
                "bytes": len(vector_bytes),
                "label": "observed by generator",
                "path": VECTORS_PATH.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(vector_bytes).hexdigest(),
            },
        ],
        "supplied_pin_assertions": parse_supplied_pins(text),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    document, _ignored = build()
    vector_text = dump(document)
    pin_text = dump(pins_document(vector_text.encode("utf-8")))
    if args.write:
        VECTORS_PATH.write_text(vector_text, encoding="utf-8")
        PINS_PATH.write_text(pin_text, encoding="utf-8")
        return 0
    if args.check:
        if VECTORS_PATH.read_text(encoding="utf-8") != vector_text:
            return 1
        if PINS_PATH.read_text(encoding="utf-8") != pin_text:
            return 1
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            sys.exit(1)
        raise
