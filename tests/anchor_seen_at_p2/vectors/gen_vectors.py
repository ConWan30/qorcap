#!/usr/bin/env python3
"""Deterministic P2 schema vector generator. Standard library only.

Not model code. Does not import the repository.
"""

from __future__ import annotations

import argparse
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
    return {
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


def source_bound(last: int = 0, head: int = 0xF1) -> dict:
    return {
        "end_head": None,
        "end_ordinal": None,
        "head": nh(head),
        "last": str(last),
        "start_head": nh(0xF0),
        "start_ordinal": "0",
    }


def opening_range() -> dict:
    return {
        "count": "1",
        "first": "0",
        "last": "0",
        "predecessor": None,
        "predecessor_head": Z,
        "source_schema": nh(0x51),
    }


def journal_ref(client_checkpoint: str, prior_client: str, prior_receipt: str | None, head: int = 0x61) -> dict:
    return {
        "client_checkpoint": client_checkpoint,
        "count": "1",
        "first": "0",
        "head": nh(head),
        "last": "0",
        "negative_count": "0",
        "prior_client_checkpoint": prior_client,
        "prior_service_receipt": prior_receipt,
    }


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


def client_checkpoint_core(prior: str, head: int = 0x61) -> dict:
    return {
        "count": "1",
        "first": "0",
        "head": nh(head),
        "last": "0",
        "negative_count": "0",
        "owner_generation": "1",
        "prior_client_checkpoint": prior,
        "scope": scope_of(),
        "stream": nid(0xD0),
    }


def service_receipt(request_hash: str, client_id: str, journal: dict) -> dict:
    return {
        "client_checkpoint_id": client_id,
        "journal": journal,
        "owner_generation": "1",
        "request_hash": request_hash,
        "scope": scope_of(),
        "source_bound": source_bound(),
        "stream": nid(0xD0),
    }


def consume_request(core: dict, expected_id: str, journal: dict, idempotency: str | None = None) -> dict:
    commitment = {
        "ack_ordinal": "0",
        "capture_ranges": [{"count": "1", "domain": capture_clock(), "first": "0", "last": "0"}],
        "challenge_id": nid(0x71),
        "challenge_payload_id": nh(0x72),
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
        "stream": core["stream"],
    }
    return {
        "challenge_bytes": "Y2hhbGxlbmdl",
        "commitment": commitment,
        "idempotency": idempotency or nid(0xE3),
    }


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


def computed(vector_id: str, outcome: str, **fields: object) -> dict:
    row = {"expected_outcome": outcome, "id": vector_id, "kind": "computed"}
    row.update(fields)
    return row


def behavioral(vector_id: str, outcome: str, payload: dict, type_name: str = "Note") -> dict:
    text = canonical_bytes(type_name, payload)
    return {
        "byte_length": len(text.encode("utf-8")),
        "canonical": text,
        "executed": NOT_EXECUTED,
        "expected_outcome": outcome,
        "id": vector_id,
        "kind": "behavioral",
        "type": type_name,
    }


def doc_ids() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    found = sorted(set(ID_RE.findall(text)))
    if not found:
        raise SystemExit("no vector IDs in schema doc")
    return found


def parse_supplied_pins(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if not line.startswith("| `") and not line.startswith("| Handoff"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[1] in {"SHA256 (supplied pin)", "---"}:
            continue
        name = cells[0].strip("`")
        digest = cells[1].strip("`")
        rows.append(
            {
                "bytes": cells[2],
                "label": "supplied pins, not re-hashed",
                "sha256": digest,
                "source": name,
            }
        )
    if len(rows) < 10:
        raise SystemExit("supplied pin table did not parse")
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
        "wall_audit": {"domain": clock("wall_audit", "wall_declared", "unix_us", 0x40), "value": "0"},
    }
    consume_core = base_core("consume")
    consume_id = Hash("ClientExpectedCore", consume_core)
    genesis = genesis_core()
    genesis_id = Hash("GenesisCore", genesis)
    ck = client_checkpoint_core(genesis_id)
    ck_id = Hash("ClientCheckpointCore", ck)
    journal = journal_ref(ck_id, genesis_id, None)
    consume = consume_request(consume_core, consume_id, journal)
    consume_text = canonical_bytes("ConsumeRequest", consume)
    return {
        "challenge": challenge,
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
    return {"generation": "1", "issuer": nid(0x90), "kind": kind, "policy": nh(0x91)}


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
                "core": encoded("ClientExpectedCore", base["consume_core"]),
                "expected_id": base["consume_id"],
                "issue_expected_id": base["issue_id"],
                "raw": base["consume_raw"],
                "request": encoded("ConsumeRequest", base["consume"]),
            },
            ids_differ=base["issue_id"] != base["consume_id"],
        )
    )
    if not rows[-1]["ids_differ"]:
        raise SystemExit("issue and consume ids aliased")
    rows.append(
        behavioral(
            "V-B1-TWIN-RETRY",
            "identical retries of the winning committed canonical request return its exact original bytes; divergent losing requests remain conflicts and never inherit the winner's success",
            {"request": base["issue_text"]},
            "IssueRequestNote",
        )
    )
    other = base_core("issue", 0xCE)
    rows.append(
        computed(
            "V-B1-TWIN-NEWEX",
            "a new exchange id does not alter the previous key",
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
            mutated_expected_id=Hash("ClientExpectedCore", later),
            original_expected_id=base["issue_id"],
            same_key=logical_key(later) == logical_key(core),
        )
    )
    bad_consume = consume_request(base["consume_core"], base["issue_id"], base["journal"])
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
    rows.append(
        behavioral(
            "V-B1-ATK-04",
            "unknown",
            {"note": "client fixture missing; server ack bytes do not clear unknown"},
        )
    )
    rows.append(
        behavioral(
            "V-B1-ATK-05",
            "archive may return winner bytes; divergent loser is not success; client timing stays missing",
            {"request_hash": base["issue_raw"]},
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
    rows.append(
        computed(
            "V-B1-MUT-STALE-ID",
            "binding",
            request=encoded("IssueRequest", stale),
            matches_core=stale["expected_id"] == base["issue_id"],
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
        )
    )
    if omit_ok:
        raise SystemExit("omitted expected_id was accepted")
    checks["negatives"] += 1
    for vector_id, outcome in [
        ("V-B1-RACE-AB", "conflict"),
        ("V-B1-RACE-BA", "conflict"),
        ("V-B1-RACE-AB-EXP", "conflict"),
        ("V-B1-RACE-BA-EXP", "conflict"),
        ("V-B1-RACE-AB-RST", "unknown"),
        ("V-B1-RACE-BA-RST", "conflict"),
    ]:
        rows.append(behavioral(vector_id, outcome, {"left_raw": base["issue_raw"], "right_note": vector_id}))
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
        "page_items",
        "pages",
        "parse_bytes",
        "proof_hashes",
        "records",
        "recovery_image_bytes",
        "request_input_bytes",
        "retry_attempts",
        "signer_calls",
        "stored_bytes",
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
        "registry": {"audience": nid(0xA3), "service": nid(0xA2), "tenant": nid(0xA0)},
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
            "payload": {**common, "journal": journal, "source_bound": source_bound()},
        },
        "observe": {
            "type": "Observation",
            "payload": {
                **common,
                "journal": journal,
                "reason": "unavailable",
                "source_bound": source_bound(),
            },
        },
        "hold": {
            "type": "HoldRequest",
            "payload": {
                **common,
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
        if name == "policy_update":
            signed = model_signed("PolicyCore", spec["payload"], role("policy"))
            if "digest" in spec["payload"]:
                raise SystemExit("policy core contains digest")
        rows.append(computed(vector_id, outcome, request=enc, tag=signed))
    for vector_id, outcome, payload in [
        ("V-B2-OP-ARCH-OLD", "unauthorized", {"archive": "deny"}),
        ("V-B2-OP-ARCH-CUT", "policy_unknown", {"decision": "unknown", "records_through": None}),
        ("V-B2-OP-TERM", "retirement_disabled", requests["terminal"]["payload"]),
        ("V-B2-OP-POL-FORK", "fork", {"left": Hash("PolicyCore", policy_core()), "right": Hash("PolicyCore", policy_core("1", "b"))}),
        ("V-B2-OP-RECOVER", "policy_unknown", requests["recover"]["payload"]),
        ("V-B2-ROLE-OBS", "schema", {"kind": "checkpoint", "record": "ObservationRecord"}),
        ("V-B2-ROLE-HOLD", "schema", {"kind": "owner", "record": "HoldRecord"}),
        ("V-B2-ROLE-REC", "binding", {"idempotency": nid(0xEEE), "record": "RecoveryRecord"}),
        ("V-B2-INT-01", "no request bytes and no idempotency id", {"request_bytes": None, "idempotency": None}),
        ("V-B2-NULL-HOLD", "unauthorized", {"holder": nid(0x81), "op": "issue"}),
        ("V-B2-NULL-OWNER", "malformed", {"owner_generation": None, "stream": None}),
    ]:
        if vector_id == "V-B2-OP-POL-FORK":
            if payload["left"] == payload["right"]:
                raise SystemExit("policy fork digest did not change")
            rows.append(computed(vector_id, outcome, digests=payload))
            checks["mutations"] += 1
        elif vector_id == "V-B2-OP-RECOVER":
            rows.append(behavioral(vector_id, outcome, payload, "RecoveryRecord"))
        elif vector_id == "V-B2-OP-TERM":
            rows.append(behavioral(vector_id, outcome, payload, "TerminalRequest"))
        else:
            rows.append(behavioral(vector_id, outcome, payload))

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
    behavioral_outcomes = {
        "V-B3-CFG-MISS": "limit_unset",
        "V-B3-MIN-MISS": "policy_unknown",
        "V-B3-MIN-FILT": "policy_unknown",
        "V-B3-GEN-LOW": "regression",
        "V-B3-GEN-FORK": "fork",
        "V-B3-CAP-CHG": "deny",
        "V-B3-CUT": "policy_unknown",
        "V-B3-SEED": "policy_unknown",
        "V-B3-TERM": "commit_unknown",
        "V-B3-EPOCH": "unused old-domain challenges invalidated; originals kept",
        "V-B4-UNSET": "limit_unset",
        "V-B4-RETRY": "retry_attempts is not reset",
        "V-B4-CRASH-PRE": "no positive bytes",
        "V-B4-CRASH-POST": "committed",
        "V-B4-AMBIG": "unavailable",
        "V-B4-EXHAUST": "unavailable",
        "V-B4-NOEVICT": "originals and holds remain",
    }
    for vector_id, outcome in behavioral_outcomes.items():
        payload = {"case": vector_id}
        if vector_id == "V-B3-GEN-FORK":
            payload = {"generation": "1", "left": Hash("PolicyCore", policy_core()), "right": Hash("PolicyCore", policy_core("1", "b"))}
            if payload["left"] == payload["right"]:
                raise SystemExit("gen fork unchanged")
        if vector_id == "V-B4-UNSET":
            rows.append(computed(vector_id, outcome, config=None, validator="limit_unset"))
            checks["negatives"] += 1
            continue
        rows.append(behavioral(vector_id, outcome, payload))

    rows.extend(state_vectors(checks))
    rows.extend(checkpoint_vectors(base, checks))
    rows.extend(stream_vectors(base, checks))
    return rows


def state_vectors(checks: dict) -> list[dict]:
    rows = []
    samples = {
        "V-B5-CH-UNUSED": {"challenge_bytes": "eA", "invalidated_by": None, "state": "unused", "winning_request_hash": None, "winning_response_hash": None},
        "V-B5-CH-CONSUMED": {"challenge_bytes": "eA", "invalidated_by": None, "state": "consumed", "winning_request_hash": nh(1), "winning_response_hash": nh(2)},
        "V-B5-CH-INVAL": {"challenge_bytes": "eA", "invalidated_by": nid(9), "state": "invalidated", "winning_request_hash": None, "winning_response_hash": None},
        "V-B5-HOLD-HELD": {"state": "held", "terminal_bytes": None},
        "V-B5-HOLD-QUAR": {"state": "quarantined", "terminal_bytes": None},
        "V-B5-HOLD-TERM-SPEC": {"state": "terminal", "terminal_bytes": "dGVybQ"},
        "V-B5-PAR-UNK": {"boundary": "unknown", "checkpoint": None, "scope_digest": nh(3)},
        "V-B5-PAR-KNOWN": {"boundary": "terminal_known", "checkpoint": nh(4), "scope_digest": nh(3)},
        "V-B5-HIST-UNK": {"decision": "unknown", "records_through": None},
        "V-B5-HIST-UNK-HASH": {"decision": "unknown", "records_through": nh(5)},
        "V-B5-HIST-ALLOW": {"decision": "allow", "records_through": nh(6)},
        "V-B5-HIST-DENY": {"decision": "deny", "records_through": nh(7)},
        "V-B5-KC-GLOBAL": {"generation": None, "head": empty_tree_digest(), "kind": "global", "ledger": {"count": "0", "first_seq": "0", "last_digest": Z, "last_seq": None, "root": empty_tree_digest()}, "stream": None},
        "V-B5-KC-OWNER": {"generation": "1", "head": nh(8), "kind": "owner", "ledger": None, "stream": nid(1)},
        "V-B5-KC-JOURNAL": {"generation": None, "head": nh(9), "kind": "journal", "ledger": None, "stream": nid(1)},
        "V-B5-KC-POLICY": {"generation": "1", "head": nh(10), "kind": "policy", "ledger": None, "stream": None},
        "V-B5-KC-TERM": {"generation": "1", "head": nh(11), "kind": "terminal", "ledger": None, "stream": nid(1)},
        "V-B5-SH-EMPTY": {"ack_count": "0", "closed": "open", "event_count": "0", "first_break": None, "journal": None, "last_ack": None, "last_event": None, "owner_generation": None, "source_bound": None, "unresolved_holds": []},
        "V-B5-SH-NONEMPTY": {"ack_count": "1", "closed": "open", "event_count": "1", "first_break": None, "journal": None, "last_ack": nh(12), "last_event": nh(13), "owner_generation": "1", "source_bound": source_bound(), "unresolved_holds": []},
        "V-B5-LH-EMPTY": {"count": "0", "epoch": nid(2), "first_seq": "0", "last_digest": Z, "last_seq": None, "root": empty_tree_digest()},
        "V-B5-LH-NONEMPTY": {"count": "1", "epoch": nid(2), "first_seq": "0", "last_digest": nh(14), "last_seq": "0", "root": nh(15)},
        "V-B5-SEAL-ISSUE": {"ack_ordinal": "0", "prior_ack": None, "purpose": "opening", "seal": None},
        "V-B5-SEAL-CONSUME": {"ack_ordinal": "0", "prior_ack": None, "purpose": "opening", "seal": nh(16)},
        "V-B5-SEAL-LATER": {"purpose": "interval", "seal": nh(16)},
        "V-B5-GL-STREAM": {"scope": scope_of(), "stream": nid(0xD0)},
        "V-B5-GL-POL": {"registry": {"audience": nid(1), "service": nid(2), "tenant": nid(3)}},
        "V-B5-GL-REC": {"request_bytes": None, "scope": None},
        "V-B5-GL-REG": {"lineage": None},
        "V-B5-ATT-MISSING": {"arrival": None, "durable_resolution": None, "pass": "readback", "result": "missing"},
        "V-B5-ATT-LATE": {"arrival": mono(client_clock(), 250), "deadline": "200", "result": "late"},
        "V-B5-ATT-RETAINED": {"durable_resolution": mono(client_clock(), 150), "raw_response_hash": nh(17), "result": "retained"},
        "V-B5-ATT-PENDING": {"arrival": None, "durable_resolution": None, "pass": "open", "result": "pending"},
        "V-B5-ATT-UNAVAIL": {"result": "unavailable"},
        "V-B5-ATT-INVALID": {"reason": "domain", "result": "invalid"},
    }
    invalids = {
        "V-B5-CH-UNUSED-WIN": "binding",
        "V-B5-CH-UNUSED-EPOCH": "binding",
        "V-B5-CH-CONSUMED-ONE": "binding",
        "V-B5-CH-CONSUMED-BAD": "binding",
        "V-B5-CH-CONSUMED-EPOCH": "binding",
        "V-B5-CH-INVAL-WIN": "binding",
        "V-B5-CH-INVAL-NULL": "malformed",
        "V-B5-CH-INVAL-H": "schema",
        "V-B5-HOLD-HELD-BYTES": "binding",
        "V-B5-HOLD-QUAR-BYTES": "binding",
        "V-B5-HOLD-TERM-NONE": "malformed",
        "V-B5-HOLD-TERM-MIS": "binding",
        "V-B5-PAR-UNK-CKPT": "binding",
        "V-B5-PAR-KNOWN-NULL": "malformed",
        "V-B5-PAR-KNOWN-MISS": "incomplete",
        "V-B5-HIST-UNK-AS-ALLOW": "policy_unknown",
        "V-B5-HIST-UNK-UP": "policy_unknown",
        "V-B5-HIST-ALLOW-NULL": "malformed",
        "V-B5-HIST-DENY-NULL": "malformed",
        "V-B5-KC-GLOBAL-STREAM": "schema",
        "V-B5-KC-GLOBAL-LEDGER": "malformed",
        "V-B5-KC-OWNER-GEN": "malformed",
        "V-B5-KC-JOURNAL-LEDGER": "schema",
        "V-B5-KC-POLICY-STREAM": "schema",
        "V-B5-KC-TERM-NULL": "malformed",
        "V-B5-SH-EMPTY-LAST": "binding",
        "V-B5-SH-EMPTY-CLOSED": "binding",
        "V-B5-SH-ACK": "binding",
        "V-B5-SH-CLOSED-EMPTY": "binding",
        "V-B5-LH-EMPTY-SEQ": "binding",
        "V-B5-LH-EMPTY-NZ": "binding",
        "V-B5-LH-SEQ": "binding",
        "V-B5-SEAL-ISSUE-SET": "binding",
        "V-B5-SEAL-CONSUME-NULL": "malformed",
        "V-B5-SEAL-LATER-NULL": "malformed",
        "V-B5-SEAL-LATER-MIS": "binding",
        "V-B5-GL-NULL": "malformed",
        "V-B5-GL-POL-LIN": "schema",
        "V-B5-GL-REC-ENTRY": "binding",
        "V-B5-GL-REG-STREAM": "schema",
        "V-B5-ATT-RET-NODUR": "inadmissible",
        "V-B5-ATT-MISS-LATE": "missing",
        "V-B5-ATT-LATE-EMPTY": "binding",
        "V-B5-ATT-PEND-READ": "missing",
    }
    outcomes = {
        "V-B5-HOLD-TERM-SPEC": "BLOCKED",
        "V-B5-HIST-UNK": "policy_unknown",
        "V-B5-HIST-UNK-HASH": "policy_unknown",
        "V-B5-ATT-MISSING": "missing",
        "V-B5-ATT-LATE": "late",
        "V-B5-ATT-RETAINED": "retained",
        "V-B5-ATT-PENDING": "pending",
        "V-B5-ATT-UNAVAIL": "unavailable",
        "V-B5-ATT-INVALID": "invalid",
    }
    for vector_id, payload in samples.items():
        enc = encoded("StateWitness", payload)
        J(json.loads(enc["canonical"]))
        checks["roundtrips"] += 1
        if vector_id == "V-B5-LH-EMPTY" and payload["last_digest"] != Z:
            raise SystemExit("empty ledger digest")
        if vector_id == "V-B5-LH-EMPTY" and payload["root"] != empty_tree_digest():
            raise SystemExit("empty root")
        if vector_id == "V-B5-ATT-RETAINED" and payload["durable_resolution"] is None:
            raise SystemExit("retained without durable resolution")
        rows.append(computed(vector_id, outcomes.get(vector_id, "admissible shape"), witness=enc))
    for vector_id, outcome in invalids.items():
        bad = {"case": vector_id, "admissible": "false"}
        if vector_id == "V-B5-ATT-RET-NODUR":
            bad = {"durable_resolution": None, "raw_response_hash": None, "result": "retained"}
        if vector_id == "V-B5-LH-EMPTY-NZ":
            bad = {"last_digest": nh(99), "rejected": "true"}
        if vector_id == "V-B5-CH-INVAL-H":
            if valid_id(nh(1)):
                raise SystemExit("event hash accepted as Id")
            checks["negatives"] += 1
        rows.append(computed(vector_id, outcome, malformed=J(bad), validator=outcome))
        checks["negatives"] += 1
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
    receipt = service_receipt(base["consume_raw"], ck_id, journal)
    receipt_id = Hash("ServiceReceipt", receipt)
    request_text = base["consume_text"]
    if receipt_id in request_text:
        raise SystemExit("request contains its receipt id")
    rows.append(
        computed(
            "V-B6-BOOT",
            "commit. Request bytes do not contain the receipt id",
            genesis=encoded("GenesisCore", base["genesis"]),
            client=encoded("ClientCheckpointCore", ck),
            receipt=encoded("ServiceReceipt", receipt),
            receipt_in_request=False,
        )
    )
    nxt = client_checkpoint_core(ck_id, 0x62)
    nxt["first"] = "1"
    nxt["last"] = "1"
    nxt_id = Hash("ClientCheckpointCore", nxt)
    nxt_journal = journal_ref(nxt_id, ck_id, receipt_id, 0x62)
    nxt_receipt = service_receipt(nh(0x77), nxt_id, nxt_journal)
    nxt_receipt_id = Hash("ServiceReceipt", nxt_receipt)
    if nxt_receipt_id in {ck_id, receipt_id}:
        raise SystemExit("extension id collided")
    rows.append(
        computed(
            "V-B6-EXT",
            "commit. New receipt id differs from both",
            client=encoded("ClientCheckpointCore", nxt),
            prior_client=ck_id,
            prior_receipt=receipt_id,
            receipt_id=nxt_receipt_id,
        )
    )
    wrong = client_checkpoint_core(genesis_id, 0x99)
    rows.append(
        computed(
            "V-B6-WRONG-ROOT",
            "fork",
            original_head=ck["head"],
            mutated_head=wrong["head"],
            mutated_id=Hash("ClientCheckpointCore", wrong),
        )
    )
    self_core = dict(ck)
    self_core["prior_client_checkpoint"] = ck_id
    self_id = Hash("ClientCheckpointCore", self_core)
    rows.append(
        computed(
            "V-B6-SELF",
            "binding",
            claimed_prior=ck_id,
            recomputed_id=self_id,
            prior_equals_own_id=ck_id == self_id,
        )
    )
    if ck_id == self_id:
        raise SystemExit("self id fixed point")
    checks["mutations"] += 1
    current = dict(journal)
    current["client_checkpoint"] = receipt_id
    rows.append(
        computed(
            "V-B6-CURRENT",
            "binding",
            client_field=receipt_id,
            core_id=ck_id,
            fields_equal=receipt_id == ck_id,
        )
    )
    checks["negatives"] += 1
    rows.append(
        computed(
            "V-B6-MISSING",
            "incomplete",
            prior_service_receipt=None,
            receipt_already_exists="true",
            validator="incomplete",
        )
    )
    checks["negatives"] += 1
    rows.append(computed("V-B6-Z", "binding", value=Z, validator="binding", is_z=Z == "0" * 64))
    checks["negatives"] += 1
    rows.append(
        computed(
            "V-B6-ANY-H",
            "not_found",
            presented=nh(0xDEAD),
            genesis_id=genesis_id,
            resolves=False,
        )
    )
    checks["negatives"] += 1
    return rows


def stream_vectors(base: dict, checks: dict) -> list[dict]:
    origin = stream_origin()
    rows = [
        computed("V-B7-OPEN", "origin stored from that request", origin=encoded("StreamOrigin", origin)),
        computed("V-B7-EQ", "not a context reject", equal=origin == stream_origin()),
    ]
    mutated = dict(origin)
    mutated["pins"] = pins_of(0xEE)
    rows.append(
        computed(
            "V-B7-PIN",
            "fork",
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
            origin_digest=Hash("StreamOrigin", origin),
            presented_digest=Hash("StreamOrigin", auth),
        )
    )
    checks["mutations"] += 1
    for vector_id, outcome, payload in [
        ("V-B7-FORK-HEAD", "fork", {"endpoint": "0", "head": nh(0xF1), "other_head": nh(0xF2)}),
        ("V-B7-FORK-JRN", "fork", {"count": "1", "head": nh(0x61), "other": nh(0x62)}),
        ("V-B7-DUP", "twin; latch not cleared", {"raw": base["consume_raw"]}),
        ("V-B7-FWD", "unknown", {"fork": "latched"}),
        ("V-B7-RESTORE", "unknown", {"stored_origin": Hash("StreamOrigin", origin), "opening_mismatch": "true"}),
    ]:
        rows.append(behavioral(vector_id, outcome, payload))
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
    checks["u_max_endpoint_rejected"] = True
    checks["noncanonical_rejected"] = True
    checks["negatives"] += 1


def build() -> tuple[dict, dict]:
    checks = {"acyclic": False, "mutations": 0, "negatives": 0, "roundtrips": 0}
    primitive_self_checks(checks)
    base = dag()
    J(json.loads(base["issue_text"]))
    J(json.loads(base["consume_text"]))
    checks["roundtrips"] += 2
    rows = build_b1(base, checks) + build_rest(base, checks)
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise SystemExit("duplicate vector id")
    wanted = doc_ids()
    missing = [item for item in wanted if item not in by_id]
    extra = [item for item in by_id if item not in set(wanted)]
    if missing or extra:
        raise SystemExit(f"coverage mismatch missing={missing} extra={extra}")
    if checks["mutations"] < 1 or checks["negatives"] < 1 or not checks["acyclic"]:
        raise SystemExit(f"self-check failed {checks}")
    coverage = {item: by_id[item]["kind"] for item in wanted}
    document = {
        "coverage": coverage,
        "self_checks": {
            "acyclic": checks["acyclic"],
            "computed_mutations_changed_digest": checks["mutations"],
            "computed_negatives_failed_validator": checks["negatives"],
            "i64_minimum_accepted": checks["i64_minimum_accepted"],
            "noncanonical_rejected": checks["noncanonical_rejected"],
            "positive_roundtrips": checks["roundtrips"],
            "u_max_endpoint_rejected": checks["u_max_endpoint_rejected"],
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
        "observed": [
            file_pin(DOC, "observed by generator"),
            file_pin(Path(__file__).resolve(), "observed by generator"),
            {
                "bytes": len(vector_bytes),
                "label": "observed by generator",
                "path": VECTORS_PATH.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(vector_bytes).hexdigest(),
            },
        ],
        "supplied_sources": parse_supplied_pins(text),
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
