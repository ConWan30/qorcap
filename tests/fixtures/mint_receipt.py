"""Mint a minimal qor-receipt/v0 for QorCap clip-credential tests.

Uses QorTime's own build/sign/verify path (PYTHONPATH must be a qortime checkout).
Prints one JSON line: {"status": "...", "pubkey": "...", "path": "..."}.
Pass "break" as the third argument to flip the referee signature after signing.
"""
from __future__ import annotations

import json
import sys

from qor_receipt.checkpoints import Chain, countersign
from qor_receipt.keys import SoftwareKey
from qor_receipt.receipt import SELF_ATTESTED, build, sign_player, sign_referee, verify_receipt


def main() -> int:
    match_id, out = sys.argv[1], sys.argv[2]
    broken = len(sys.argv) > 3 and sys.argv[3] == "break"
    player, ref = SoftwareKey(), SoftwareKey()
    start, end = 1_000_000, 1_005_000
    cp = Chain().next(1_002_000, "ab" * 32, "cd" * 32)
    cp = countersign(ref, match_id, "home", cp, 1_002_000)
    rec = build(
        match={
            "match_id": match_id,
            "game": "madden27",
            "platform": "ps5",
            "scorebug_profile": "madden27_standard",
            "mode": "single",
            "started_ms": start,
            "ended_ms": end,
        },
        result={
            "home_score": 7,
            "away_score": 3,
            "quarter": 4,
            "final_t_ms": end,
            "winner": "home",
            "method": "agreed_scorebug_final",
        },
        players=[{
            "role": "home",
            "player_id": "local:player",
            "pubkey": player.pubkey_hex,
            "key_kind": "software",
        }],
        referee={"pubkey": ref.pubkey_hex, "key_id": ref.key_id, "key_kind": "local-self-attested"},
        challenges=[],
        checkpoints={"home": [cp]},
        agreement={"timelines_agree": None, "mismatches": []},
        signals={"motion_permille": {"home": None}},
    )
    rec = sign_referee(sign_player(rec, "home", player), ref)
    if broken:
        rec["signatures"]["referee"]["sig"] = "00" * 64
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    with open(out + ".pub", "w", encoding="ascii") as fh:
        fh.write(ref.pubkey_hex + "\n")
    verdict = verify_receipt(rec, referee_pub=ref.pubkey_hex)
    print(json.dumps({
        "status": verdict["status"],
        "expected_if_intact": SELF_ATTESTED,
        "errors": verdict.get("errors"),
        "pubkey": ref.pubkey_hex,
        "path": out,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
