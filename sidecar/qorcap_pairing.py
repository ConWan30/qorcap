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
        if not isinstance(value, str) or len(value) != n or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError(f"pairing token {name} is not {n} hex characters")
    issued = token.get("issued_ms")
    if isinstance(issued, bool) or not isinstance(issued, int) or issued < 0 or issued >= 2**64:
        raise ValueError("pairing token issued_ms is not a non-negative integer")
    return token


def token_code(token: dict) -> str:
    """One line the other player pastes. Compact JSON, so hex case survives the round trip."""
    raw = json.dumps(check_token(token), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token_from_code(code: str) -> dict:
    text = (code or "").strip()
    if text.startswith("{"):
        try:
            token = json.loads(text)
        except ValueError as e:
            raise ValueError("pairing code is not JSON") from e
        return check_token(token)
    if not text.startswith(CODE_PREFIX):
        raise ValueError("pairing code must start with qorpair1. The pairing id alone is not the token.")
    b64 = text[len(CODE_PREFIX):]
    pad = "=" * (-len(b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(b64 + pad)
    except Exception as e:
        raise ValueError("pairing code is not valid base64") from e
    try:
        token = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise ValueError("pairing code does not contain a token") from e
    return check_token(token)


def fetch_pairing(base: str, opener=None) -> dict:
    """POST /v1/pairings. The body is ``{}`` because the referee ignores it and chooses the id."""
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
        token = json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise PairingError("POST /v1/pairings did not return JSON") from e
    try:
        return check_token(token)
    except ValueError as e:
        raise PairingError(str(e)) from e


def write_pairing(bundle: Path, token: dict) -> Path:
    """Write pairing.json. receipt.json bytes are left as they were."""
    token = check_token(token)
    bundle.mkdir(parents=True, exist_ok=True)
    receipt = bundle / "receipt.json"
    before = receipt.read_bytes() if receipt.is_file() else None
    path = bundle / FILENAME
    path.write_text(json.dumps(token, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if before is not None and receipt.read_bytes() != before:
        raise PairingError("receipt.json changed while writing pairing.json")
    return path


def describe(token: dict) -> list[str]:
    code = token_code(token)
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
        token = fetch_pairing(base, opener=opener) if create else token_from_code(code or "")
        write_pairing(bundle, token)
    except (PairingError, ValueError, OSError) as e:
        say(f"PROBLEM: {e}")
        return 2
    for line in describe(token):
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
