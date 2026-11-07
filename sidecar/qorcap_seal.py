"""Seal exclusion window. Fail closed.

The full end is the later of menu exit plus six frames, and OPTIONS release
plus measured lag plus six frames. If the menu exit is missing, or measured
lag is missing, the window stays unresolved and does not release later events
into counts. It does not shrink to the OPTIONS release.
"""
from __future__ import annotations

FRAME_HOLD = 6
SCHEMA = "qorcap-seal-window/v0"


def seal_exclusion(*, options_release_us, menu_exit_us, lag_us, frame_us) -> dict:
    missing = []
    if menu_exit_us is None:
        missing.append("menu_exit_missing")
    if lag_us is None:
        missing.append("lag_not_measured")
    if missing or not isinstance(frame_us, int) or frame_us <= 0 or not isinstance(options_release_us, int):
        reason = missing[0] if missing else "lag_not_measured"
        if not isinstance(options_release_us, int) and "menu_exit_missing" not in missing and "lag_not_measured" not in missing:
            reason = "options_release_missing"
        return {
            "schema": SCHEMA,
            "status": "unresolved",
            "reason": reason,
            "releases_counts": False,
            "end_us": None,
            "provisional": True,
            "note": "Unresolved. Later events stay out of counts. The window does not shrink.",
        }
    menu_end = menu_exit_us + FRAME_HOLD * frame_us
    lag_end = options_release_us + lag_us + FRAME_HOLD * frame_us
    return {
        "schema": SCHEMA,
        "status": "resolved",
        "reason": "later_of_menu_and_lag",
        "releases_counts": True,
        "end_us": max(menu_end, lag_end),
        "menu_end_us": menu_end,
        "lag_end_us": lag_end,
        "provisional": True,
        "note": "Resolved end is the later of the two holds. Events before it stay out of counts.",
    }


def event_released(exclusion, t_us) -> bool:
    """True only when the window is resolved and t_us is at or after its end."""
    if not isinstance(exclusion, dict) or not exclusion.get("releases_counts"):
        return False
    end = exclusion.get("end_us")
    return isinstance(end, int) and isinstance(t_us, int) and t_us >= end


def dual_bind_passes(session, pad, checkpoint_hash, anchor=None) -> bool:
    """Opening gate. A late hash fails. The seal hash is not an anchor."""
    if not isinstance(session, dict) or not isinstance(pad, dict):
        return False
    digest = session.get("seal_hash")
    if not isinstance(digest, str) or not digest:
        return False
    if pad.get("seal_hash") != digest or checkpoint_hash != digest:
        return False
    if not isinstance(anchor, str) or not anchor or anchor == digest:
        return False
    if session.get("anchor_ack") != anchor or pad.get("anchor_ack") != anchor:
        return False
    return session.get("opening_gate") is True and session.get("status") == "locked" and pad.get("opening_gate") is True
