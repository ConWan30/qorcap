"""Free, per-game labels that never come from Glimpse.

Sources, in trust order:
  A  score lock from a hash-chain-verified QorCap checkpoint, inside a legal segment whose last lock equals an
     end-menu-confirmed final (final_basis 'end_menu' / final_confirmed True).
  B  same, but final not confirmed (or confirmed only by the operator), or locks from an unsigned reader re-run.
Frames inside a transition margin around any lock change, before the first lock, or across an illegal step
get NO label. Region (scorebug present / absent) is labelled only from the reader's own timeline
observations (score rows / scorebug_absent), never from Glimpse.
"""
import bisect

PRE_MARGIN = 45_000          # ms before the next lock: the screen may already show the new score (reader lag;
                             # measured 22-32 s on 021003/162055)
PRE_MARGIN_MERGED = 60_000   # before a +7/+8 lock (TD and try merged: the +6 showed first)
POST_MARGIN = 4_000   # ms after a lock: animation / agreement window settling
GLIMPSE_SOURCES = ("glimpse", "advisory", "elorian")

class CircularLabel(ValueError):
    pass

def assert_independent(source: str):
    s = (source or "").lower()
    if any(g in s for g in GLIMPSE_SOURCES):
        raise CircularLabel(f"label source {source!r} is derived from Glimpse; refusing (circularity)")

def label_frames(frames, segs, *, source, chain_verified, final=None, region_obs=None, negatives=False):
    """frames: [{'id', 't'}]; segs: legal lock segments [(t, a, h, ref)].
    final: {'away','home','confirmed': 'end_menu'|'operator'|None}. Returns {frame_id: label}.

    An interval [lock i, lock i+1) is trusted only if the step i -> i+1 is legal (inside one segment). The last
    interval of a segment that ends in an illegal step, and the open interval after the last lock without a
    confirmed final, are NOT labelled (the reader may have missed changes there)."""
    assert_independent(source)
    out = {}
    last_seg = segs[-1] if segs else []
    final_ok = bool(final and last_seg and (final["away"], final["home"]) == (last_seg[-1][1], last_seg[-1][2]))
    for f in frames:
        t = f["t"]
        lab = {"label": None, "why": "before_first_lock"}
        for si, seg in enumerate(segs):
            times = [L[0] for L in seg]
            i = bisect.bisect_right(times, t) - 1
            if i < 0:
                continue
            closing = i + 1 < len(seg)
            nxt = times[i + 1] if closing else None
            if not closing and si < len(segs) - 1 and t >= segs[si + 1][0][0]:
                continue                                  # belongs to a later segment
            if not closing:
                lab = ({"label": None, "why": "gap_before_illegal_step"} if si < len(segs) - 1 else
                       None if final_ok else {"label": None, "why": "after_last_lock_unconfirmed_final"})
                if lab is not None:
                    break
            pre = 0
            if closing:
                d = (seg[i + 1][1] - seg[i][1]) + (seg[i + 1][2] - seg[i][2])
                pre = PRE_MARGIN_MERGED if d in (7, 8) else PRE_MARGIN
            if t < times[i] + POST_MARGIN or (nxt is not None and t > nxt - pre):
                lab = {"label": None, "why": "transition_margin"}
                break
            grade = "A" if (chain_verified and final_ok and final.get("confirmed") == "end_menu"
                            and len(segs) == 1) else "B"
            lab = {"label": {"away": seg[i][1], "home": seg[i][2]}, "grade": grade, "basis": seg[i][3],
                   "source": source, "independent_of_glimpse": True}
            break
        if region_obs is not None:
            lab["scorebug_present"] = region_obs(t)
        out[f["id"]] = lab
    return out
