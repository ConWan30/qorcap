"""Hash-linked, append-only ledgers (lesson ledger and spend ledger share the format).

entry = {seq, prev, kind, ts, body}; hash = SHA-256(JCS(entry)); stored as {**entry, hash}.
Anyone holding the file can re-verify the chain; body.receipt_refs ties lessons to the QorCap
evidence (checkpoint hashes / receipt sha256) the labels came from. The ledger only POINTS at receipts;
it never writes into them.
"""
import json, os, datetime
from .jcs import sha

GENESIS = "0" * 64

class HashLedger:
    def __init__(self, path):
        self.path = path
        self.rows = [json.loads(l) for l in open(path)] if os.path.exists(path) else []

    @property
    def head(self):
        return self.rows[-1]["hash"] if self.rows else GENESIS

    def append(self, kind, body, ts=None):
        e = {"seq": len(self.rows), "prev": self.head, "kind": kind,
             "ts": ts or datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "body": body}
        e["hash"] = sha({k: v for k, v in e.items() if k != "hash"})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
        self.rows.append(e)
        return e

def verify(path):
    prev, n = GENESIS, 0
    for line in open(path, encoding="utf-8"):
        e = json.loads(line)
        h = e.pop("hash")
        if e["prev"] != prev or sha(e) != h or e["seq"] != n:
            return False, n
        prev, n = h, n + 1
    return True, n
