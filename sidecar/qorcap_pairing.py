"""Pair two QorCap recordings of the same match.

``POST /v1/pairings`` returns a referee-signed ``qortime-pairing/v1`` token. The
referee chooses ``pairing_id`` and ``nonce``; a client-supplied id is ignored.
Both players store that token, unchanged, as ``pairing.json`` next to
``receipt.json``. The receipt schema rejects extra fields, so the token is never
written into the receipt.

The other player joins by pasting the one-line code this command prints. The
code is the token, not the pairing id alone: the signature has to travel with
the id. Corroboration is a separate status. It does not change VERIFIED,
PARTIAL, SELF_ATTESTED, or FAIL.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PAIRING_TYPE = "qortime-pairing/v1"
CODE_PREFIX = "qorpair1."
FILENAME = "pairing.json"
# pairing_id and nonce are 16 bytes (32 hex). key_id is 8 bytes. sig is 64 bytes.
_HEX_LEN = {"pairing_id": 32, "nonce": 32, "key_id": 16, "sig": 128}


class PairingError(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def check_token(token: dict) -> dict:
    """Require the documented token shape. String values are kept exactly (the signature covers them)."""
    if not isinstance(token, dict):
        raise ValueError("pairing token is not an object")
    if token.get("type") != PAIRING_TYPE:
        raise ValueError(f"pairing token type is not {PAIRING_TYPE}")
    for name, n in _HEX_LEN.items():
        value = token.get(name)
        alphabet = "0123456789abcdef" if name in ("pairing_id", "nonce") else "0123456789abcdefABCDEF"
        if not isinstance(value, str) or len(value) != n or any(c not in alphabet for c in value):
            kind = "lowercase hex" if name in ("pairing_id", "nonce") else "hex"
            raise ValueError(f"pairing token {name} is not {n} {kind} characters")
    issued = token.get("issued_ms")
    if isinstance(issued, bool) or not isinstance(issued, int) or issued < 0 or issued >= 2**64:
        raise ValueError("pairing token issued_ms is not a non-negative integer")
    return token


def code_for(raw: bytes) -> str:
    """One line the other player pastes. base64url of the pairing.json bytes. The referee has no lookup."""
    parse_raw(raw)
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def raw_from_code(code: str) -> bytes:
    """The identical token bytes. A bare pairing id is not enough: the signature has to travel with it."""
    text = (code or "").strip()
    if text.startswith("{"):
        raw = text.encode("utf-8")
    elif text.startswith(CODE_PREFIX):
        b64 = text[len(CODE_PREFIX):]
        pad = "=" * (-len(b64) % 4)
        try:
            raw = base64.urlsafe_b64decode(b64 + pad)
        except Exception as e:
            raise ValueError("pairing code is not valid base64") from e
    else:
        raise ValueError("pairing code must start with qorpair1. or be the token JSON. The pairing id alone is not the token.")
    parse_raw(raw)
    return raw


def parse_raw(raw: bytes) -> dict:
    try:
        token = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as e:
        raise ValueError("pairing token is not JSON") from e
    return check_token(token)


def fetch_pairing(base: str, opener=None) -> bytes:
    """POST /v1/pairings. The body is ``{}`` because the referee ignores it and chooses the id.

    Returns the response bytes unchanged.
    """
    url = base.rstrip("/") + "/v1/pairings"
    req = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "qorcap-pair/0.3.30"},
    )
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise PairingError(f"POST /v1/pairings returned HTTP {e.code}") from e
    except OSError as e:
        raise PairingError(f"POST /v1/pairings failed: {e}") from e
    try:
        parse_raw(body)
    except ValueError as e:
        raise PairingError(str(e)) from e
    return body


def write_pairing(bundle: Path, raw: bytes) -> Path:
    """Write pairing.json as the exact token bytes. receipt.json is not touched."""
    parse_raw(raw)
    bundle.mkdir(parents=True, exist_ok=True)
    receipt = bundle / "receipt.json"
    before = receipt.read_bytes() if receipt.is_file() else None
    path = bundle / FILENAME
    path.write_bytes(raw)
    if before is not None and receipt.read_bytes() != before:
        raise PairingError("receipt.json changed while writing pairing.json")
    return path


def describe(raw: bytes) -> list[str]:
    token = parse_raw(raw)
    code = code_for(raw)
    return [
        "Pairing code (send this whole line to the other player):",
        code,
        f"pairing_id: {token['pairing_id']}",
        "They join with:  qorcap pair join \"<code>\" --bundle \"<their match folder>\"",
        "or Start Match --pair-code \"<code>\".",
        "Wrote pairing.json next to the receipt. It is not inside receipt.json.",
        "Corroboration does not change VERIFIED, PARTIAL, SELF_ATTESTED, or FAIL.",
    ]


def apply_pairing(bundle: Path, *, create: bool, code: str | None, base: str, opener=None) -> int:
    """Create or join, write pairing.json, print the code. 0 on success, 2 on failure. Does not start a recording."""
    try:
        if create and code:
            raise PairingError("pass either --pair create or --pair-code, not both")
        raw = fetch_pairing(base, opener=opener) if create else raw_from_code(code or "")
        write_pairing(bundle, raw)
    except (PairingError, ValueError, OSError) as e:
        say(f"PROBLEM: {e}")
        return 2
    for line in describe(raw):
        say(line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qorcap pair", description="Create or join a referee pairing token")
    ap.add_argument("action", choices=["create", "join"])
    ap.add_argument("code", nargs="?", help="the other player's qorpair1 code (join only)")
    ap.add_argument("--bundle", required=True, help="match folder; pairing.json is written beside receipt.json")
    ap.add_argument("--referee", default=None, help="referee service URL (default: the official QorTime referee)")
    ap.add_argument("--qorcap", help=argparse.SUPPRESS)
    ap.add_argument("--ffmpeg", help=argparse.SUPPRESS)
    ap.add_argument("--qortime", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.action == "join" and not a.code:
        say("PROBLEM: join needs the other player's pairing code.")
        return 2
    if a.action == "create" and a.code:
        say("PROBLEM: create takes no code. The referee chooses the pairing id.")
        return 2
    from qorcap_submit import OFFICIAL_URL

    return apply_pairing(Path(a.bundle), create=a.action == "create", code=a.code,
                         base=a.referee or OFFICIAL_URL)


if __name__ == "__main__":
    sys.exit(main())
