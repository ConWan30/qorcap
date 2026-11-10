"""Read QorCap lock evidence and verify it before it may become a label (read-only)."""
import json
from .jcs import sha

FOOTBALL = frozenset({1, 2, 3, 6, 7, 8})
GENESIS = "0" * 64

def legal_step(a0, h0, a1, h1):
    da, dh = a1 - a0, h1 - h0
    return (da, dh) == (0, 0) or (da == 0 and dh in FOOTBALL) or (dh == 0 and da in FOOTBALL)

def verify_checkpoints(path):
    """QorTime checkpoint rule: hash = SHA-256(JCS({seq,t_ms,frames_hash,input_hash,prev})), chained from 64x'0',
    frames_hash = SHA-256(JCS(qorcap)). Returns (ok, head, checkpoints). Any break -> ok False."""
    prev, cps, ok = GENESIS, [], True
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            c = json.loads(line)
            body = {k: c[k] for k in ("seq", "t_ms", "frames_hash", "input_hash", "prev")}
            if c["prev"] != prev or sha(body) != c["hash"]:
                ok = False
            q = c.get("qorcap")
            if q is not None and sha(q) != c["frames_hash"]:
                ok = False
            prev = c["hash"]
            cps.append(c)
    return ok, prev, cps

def locks_from_checkpoints(cps, clock="t_ms"):
    """Score-change locks [(t, away, home, checkpoint_hash)], consecutive duplicates (quarter flicker) dropped."""
    out = []
    for c in cps:
        q = c.get("qorcap") or {}
        if q.get("trigger") not in ("score_change", "final") or not q.get("score"):
            continue
        s = q["score"]
        t = q.get("video_ms") if clock == "video_ms" else c["t_ms"]
        if out and (out[-1][1], out[-1][2]) == (s["away"], s["home"]):
            continue
        out.append((t, s["away"], s["home"], c["hash"]))
    return out

def segments(locks):
    """Split a lock chain at illegal steps (decrease, both sides, non-football delta). Each segment is a
    run of legal steps; an illegal step means a misread, a reset or a second game and is never bridged."""
    segs, cur = [], []
    for L in locks:
        if cur and not legal_step(cur[-1][1], cur[-1][2], L[1], L[2]):
            segs.append(cur); cur = []
        cur.append(L)
    if cur:
        segs.append(cur)
    return segs
