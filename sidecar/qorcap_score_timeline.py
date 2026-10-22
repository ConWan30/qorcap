"""Signed score timeline sidecar.

The referee cross-checks ``signals.score_timeline.events``: each event is
``{t_ms, home, away, quarter}``. ``receipt.v0.json`` does not define that object,
and this module does not write it into ``receipt.json``. The events live in
``score_timeline.json``, hash-chained like a checkpoint chain and signed by the
same device key that signs the receipt.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "qorcap-score-timeline/v0"
FILENAME = "score_timeline.json"
GENESIS = "0" * 64


def _nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def events_from_timeline(rows) -> list[dict]:
    """Score reads only. Observation spans are skipped. Consecutive identical
    ``(home, away, quarter)`` states count as one change, keeping the first timestamp.
    """
    out: list[dict] = []
    last = None
    for item in rows:
        if not isinstance(item, dict) or "obs" in item:
            continue
        try:
            t, h, a, q = item["t_ms"], item["home"], item["away"], item["quarter"]
        except KeyError:
            continue
        if not all(_nonneg_int(x) for x in (t, h, a, q)) or q < 1:
            continue
        key = (h, a, q)
        if key == last:
            continue
        last = key
        out.append({"t_ms": int(t), "home": int(h), "away": int(a), "quarter": int(q)})
    return out


def chain_events(events: list[dict]) -> tuple[list[dict], str]:
    """Each link is SHA-256 of JCS({t_ms, home, away, quarter, prev})."""
    from qor_receipt.jcs import sha256_hex

    prev = GENESIS
    rows = []
    for e in events:
        body = {"t_ms": e["t_ms"], "home": e["home"], "away": e["away"], "quarter": e["quarter"], "prev": prev}
        digest = sha256_hex(body)
        rows.append(dict(body, hash=digest))
        prev = digest
    return rows, prev


def checkpoint_head(receipt: dict) -> str:
    """``receipt.checkpoints[role][-1].hash``, else the only non-empty role, else genesis."""
    players = receipt.get("players") if isinstance(receipt.get("players"), list) else []
    role = players[0].get("role") if players and isinstance(players[0], dict) else None
    cps = receipt.get("checkpoints") if isinstance(receipt.get("checkpoints"), dict) else {}
    chain = cps.get(role) if role in ("home", "away") else None
    if not isinstance(chain, list) or not chain:
        found = [cps.get(r) for r in ("home", "away") if isinstance(cps.get(r), list) and cps.get(r)]
        chain = found[0] if len(found) == 1 else None
    if not isinstance(chain, list) or not chain or not isinstance(chain[-1], dict):
        return GENESIS
    head = chain[-1].get("hash")
    if isinstance(head, str) and len(head) == 64 and all(c in "0123456789abcdef" for c in head):
        return head
    return GENESIS


def build(events: list[dict], *, checkpoint_head: str) -> dict:
    chained, head = chain_events(events)
    return {
        "schema": SCHEMA,
        "signals": {"score_timeline": {"events": events}},
        "chain": chained,
        "chain_head": head,
        "checkpoint_head": checkpoint_head,
    }


def signing_message(doc: dict) -> bytes:
    """JCS of the events, the chain, and the checkpoint head. The signature object is not included."""
    from qor_receipt.jcs import canonicalize

    return canonicalize({
        "type": SCHEMA,
        "signals": doc["signals"],
        "chain": doc["chain"],
        "chain_head": doc["chain_head"],
        "checkpoint_head": doc["checkpoint_head"],
    })


def sign_doc(doc: dict, device) -> dict:
    out = dict(doc)
    out["signature"] = {"pub": device.pubkey_hex, "sig": device.sign(signing_message(doc))}
    return out


def verify_doc(doc: dict) -> list[str]:
    """Empty when the chain matches the events and the device signature covers that chain."""
    from qor_receipt.keys import verify

    errs = []
    block = doc.get("signals") if isinstance(doc.get("signals"), dict) else {}
    timeline = block.get("score_timeline") if isinstance(block.get("score_timeline"), dict) else {}
    events = timeline.get("events")
    if not isinstance(events, list):
        return ["events"]
    plain = []
    for e in events:
        if not isinstance(e, dict):
            return ["events"]
        try:
            plain.append({"t_ms": e["t_ms"], "home": e["home"], "away": e["away"], "quarter": e["quarter"]})
        except KeyError:
            return ["events"]
    chained, head = chain_events(plain)
    if doc.get("chain") != chained:
        errs.append("chain")
    if doc.get("chain_head") != head:
        errs.append("chain_head")
    sig = doc.get("signature") if isinstance(doc.get("signature"), dict) else {}
    pub, hexsig = sig.get("pub"), sig.get("sig")
    if not isinstance(pub, str) or not isinstance(hexsig, str) or not verify(pub, hexsig, signing_message(doc)):
        errs.append("signature")
    return errs


def read_timeline(bundle: Path) -> list[dict]:
    path = bundle / "timeline.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def write_score_timeline(bundle: Path, device, receipt: dict) -> Path:
    """Write ``score_timeline.json``. Does not read or write ``receipt.json``."""
    events = events_from_timeline(read_timeline(bundle))
    doc = sign_doc(build(events, checkpoint_head=checkpoint_head(receipt)), device)
    path = bundle / FILENAME
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path
