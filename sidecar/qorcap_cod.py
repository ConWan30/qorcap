"""Call of Duty (MW4 / modern) match set.

The differentiator is multi-signal match verification for skill evidence
(controller + video + correlated game HUD), using real match recordings as the
calibration source. The product line is in ``docs/GAME_SETS.md``.

How a Call of Duty match is played, and what this set lines up:

- A shot is a trigger edge on the controller (R2 rising by more than the same
  60 counts ``src/haptics.rs`` already uses) and a hitmarker on the frame.
- The kill popup follows a hitmarker that already agreed. The killfeed follows
  that popup. A later hook whose prior step did not agree is flagged a mismatch.
- Pause is the OPTIONS button (bit 9, the same bit the Madden path uses) and
  the pause crop. This set does not run the Madden play-call detector.
- Recoil-style motion is a haptic row the existing detector already labeled
  high. A ``grip_jolt`` or ``hand_motion`` row stays low there, so it is not
  Counted here.
- Kills and assists against the end scoreboard are a separate hook. A missing
  number is not Counted. This hook does not post a final.
- The match final, when the end word agrees, uses Qortrola30's own scoreboard
  row (kills and deaths, assists when that column is on the board). That row
  is not home or away. Another player's row is not used. A missing row or an
  unreadable digit is not Counted.
- Stick turn against camera turn stays Counted. The tag does not add a triple
  by itself. Rumble against an on-screen hit stays INFO-ONLY. Grip is off for
  practice.
- The match ends when the end screen agrees, the way Madden's final screen
  ends a match. Stopping the recorder is not that read.

The shared clock is the opt-in frame clock (``src/frameclock.rs``). A stamp
must sit within ``FRAME_CLOCK_GAP_US`` (500 ms, ``MEASURED_MAX_GAP_US``) of
both the screen time and the input time. The manifest fit is not used. With
the frame clock off, a hook is not Counted.

Hitmarker, kill popup, killfeed presence, VICTORY and DEFEAT on the end
screen, and scoreboard K/A are calibrated from Con's private practice frames.
They set ``calibrated`` and ``ok`` only when that signal is on the frame.
Pause and the crosshair stay not read. An unreadable hook is not Counted,
posts no score, and posts no final. Those frames are not a public verification.

Words on the log and the receipt note: agreed, flagged a mismatch, Counted,
receipt. A local end-screen read is not a verification verdict.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PROFILE_ID = "cod_mw4_hud"
GAME_ID = "cod_mw4"

# Same window as src/frameclock.rs MEASURED_MAX_GAP_US.
FRAME_CLOCK_GAP_US = 500_000
# Same trigger step src/haptics.rs treats as a change.
TRIGGER_DELTA = 60
# DualSense OPTIONS, src/hid.rs bit 9. Same bit qorcap_reader.OPTIONS_BIT.
OPTIONS_BIT = 1 << 9

AGREED = "agreed"
MISMATCH = "flagged a mismatch"
NOT_COUNTED = "not Counted"

# (name, x, y, w, h) in 0..1 of a 16:9 frame.
# Measured on Con's private practice frames (match-20261105-173922,
# match-20261101-092413, and the scoreboard strips). Pause is still only a crop.
# end_screen is the search window. It stays wide so the letter sizes stay in
# the calibrated band. A word still has to land in its own box, measured on
# these frames: VICTORY splash x 0.29-0.71, y 0.40-0.62 (end_173922_017);
# VICTORY header x 0.05-0.21, y 0.07-0.16 (end_173922_019); DEFEAT header
# x 0.81-0.96, y 0.07-0.16. The older crop stopped at x 0.83 and cut DEFEAT,
# so this window runs out to that edge. The glitched splash is not enough
# on its own. A word outside its box is not read.
# hitmarker is the center aim window. kill_popup is the center XP plate.
# killfeed is the top-right, above the killstreak banner on these frames.
# scoreboard is the whole frame: the K/A strip is a short crop, and the
# header match is what decides it. KILLS sits near x 0.32-0.39 of that strip
# and ASSISTS near x 0.54-0.64.
REGIONS: tuple[tuple[str, tuple[float, float, float, float]], ...] = (
    ("crosshair", (0.47, 0.46, 0.06, 0.08)),
    ("hitmarker", (0.40, 0.42, 0.20, 0.22)),
    ("kill_popup", (0.30, 0.28, 0.40, 0.36)),
    ("killfeed", (0.64, 0.01, 0.34, 0.16)),
    ("pause", (0.20, 0.15, 0.60, 0.70)),
    ("end_screen", (0.02, 0.02, 0.96, 0.68)),
    ("scoreboard", (0.00, 0.00, 1.00, 1.00)),
)
HUD_NAMES = ("crosshair", "hitmarker", "kill_popup", "killfeed", "pause", "scoreboard")
END_NAME = "end_screen"
# Order a count follows. Crosshair is a crop, not a counted event.
COUNT_SIGNALS = ("hitmarker", "kill_popup", "killfeed", "pause", "recoil", "scoreboard", "end_screen")

# Stick stays Counted. The tag does not add a triple by itself.
COUNTED_SIGNALS = {
    "stick_turn_vs_camera": {
        "label": "Counted",
        "status": "Counted",
        "note": "Stick turn against camera turn stays Counted. The tag does not add a triple by itself.",
    },
}

# Optional later counts. They stay INFO-ONLY until a held-out match promotes them.
# Power is unknown: this set does not read battery versus plugged.
INFO_ONLY = {
    "rumble_vs_hit": {
        "label": "INFO-ONLY",
        "status": NOT_COUNTED,
        "note": "Rumble against an on-screen hit. Not Counted until a held-out match promotes it.",
    },
    "power_aware_timing": {
        "label": "INFO-ONLY",
        "power": "unknown",
        "status": NOT_COUNTED,
        "note": "Battery versus plugged is not read, so no timing band is applied.",
    },
}

END_AGREE_FRAMES = 3
END_AGREE_SPAN_MS = 3000
# A hole longer than the bar is not one showing. The candidate starts over.
# A 2000 ms step, which the three-frame check uses, is still inside this cap.
END_AGREE_GAP_MS = END_AGREE_SPAN_MS
# Offline video probe. The last few end frames, not a long end-window scan.
END_PROBE_FRAMES = 5
# A hitmarker counts only inside this window after an R2 edge. PROVISIONAL.
FIRE_WINDOW_US = 250_000
# An own-kill feed row needs a gated hitmarker this close. PROVISIONAL.
KILLFEED_NEAR_US = 2_000_000
SIGNAL_GENESIS = "0" * 64
SIGNAL_SCHEMA = "qorcap-cod-signals/v0"
READER_STATS_SCHEMA = "qorcap-cod-reader-stats/v0"
# Counts copied onto the stop file. Other runner counters stay off that file.
READER_STATS_KEYS = ("frames", "agreed", "flagged_mismatch", "not_counted", "hud_not_read", "errors")
# reader_tick is time.time()*1000 in the reader loop. End-word t_ms is the
# capture step's frame time (producer monotonic milliseconds). Not one clock.
READER_TICK_CLOCK = "wall_unix_ms"
END_WORD_CLOCK = "capture_monotonic_ms"
READER_STATS_ERROR_MAX = 180
# authorization: Bearer <token> is one secret. A standalone Bearer token is the
# same class. The token must contain a digit or a token mark so "bearer of"
# stays readable.
_STATS_BEARER_RE = re.compile(
    r"(?i)\bbearer\s+(?=[A-Za-z0-9\-._~+/]*[0-9\-._~+/=])[A-Za-z0-9\-._~+/=]+"
)
_STATS_SECRET_RE = re.compile(
    r"(?i)\b(api_key|token|secret|password|passwd|authorization)\b\s*[:=]\s*(?:bearer\s+)?\S+"
)
_STATS_QUOTED_PATH_RE = re.compile(
    r"""["'](?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/)[^"']*["']"""
)
# Drive, UNC (\\server\share and //server/share), tilde, or a Unix path of at
# least two segments. A single /word is left alone.
_STATS_PATH_START_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+|//[^/\s]+/[^/\s]+|~[\\/]|(?:/[\w.-]+){2,})"
)
_STATS_PATH_SEG_RE = re.compile(r"[\w.\\/-]+")
_STATS_PATH_MARK_RE = re.compile(r"(?:[\\/]|\.[A-Za-z0-9]{1,8}$)")
# At 4 Hz this is about eight frames. The probe keeps the last five.
END_PROBE_TAIL_S = 2

# Con's end-scoreboard row. Letters may differ in case. A different tag is not this row.
OWNER_TAG = "Qortrola30"


def same_owner_tag(text) -> bool:
    """True when ``text`` is Con's gamertag. Case does not matter. Spaces do not count.

    The letters have to agree with ``OWNER_TAG``. Another player's name does not.
    """
    if not isinstance(text, str):
        return False
    folded = "".join(text.split()).casefold()
    return bool(folded) and folded == OWNER_TAG.casefold()

STUB_NOTE = "hook only: this region is cropped and not read. It is not Counted."


@dataclass
class RegionRead:
    region: str
    status: str
    ok: bool
    reason: str
    calibrated: bool = False
    note: str = ""
    home: int | None = None
    away: int | None = None
    confirm: bool = False
    kills: int | None = None
    assists: int | None = None
    board_kills: int | None = None
    board_assists: int | None = None
    deaths: int | None = None
    confirms: int | None = None
    player_tag: str | None = None


def crop_box(img, box: tuple[float, float, float, float]):
    """Crop a normalized box. ``None`` when the frame cannot be cropped."""
    shape = getattr(img, "shape", None)
    if not shape or len(shape) < 2:
        return None
    try:
        h, w = int(shape[0]), int(shape[1])
    except (TypeError, ValueError):
        return None
    if h < 2 or w < 2:
        return None
    x, y, bw, bh = box
    x0 = max(0, min(w - 1, int(round(float(x) * w))))
    y0 = max(0, min(h - 1, int(round(float(y) * h))))
    x1 = max(x0 + 1, min(w, int(round(float(x + bw) * w))))
    y1 = max(y0 + 1, min(h, int(round(float(y + bh) * h))))
    try:
        crop = img[y0:y1, x0:x1]
    except Exception:
        return None
    if getattr(crop, "size", None) == 0:
        return None
    return crop


class CodHudReader:
    """Crosshair, hitmarker, kill popup, killfeed, pause, and scoreboard crops.

    Hitmarker, kill popup, killfeed, and scoreboard K/A are calibrated.
    Crosshair and pause stay not read. ``ok`` and ``calibrated`` are true only
    when that signal is on the frame.
    """

    profile_id = PROFILE_ID
    wants_full_res = True

    def read(self, img) -> list[RegionRead]:
        from qorcap_cod_hud import read_hud
        return list(read_hud(img))


class CodEndScreenReader:
    """The Call of Duty end screen. This is the end of the match.

    ``confirm`` is set only when the frame shows one sharp end word in its box:
    VICTORY on the splash or the match-complete header, or DEFEAT on the
    top-right header with a red field. Both words use the same letter gate.
    A word outside its box does not confirm. If both words agree, the frame
    is not read. A blurred word does not confirm. Splash and header reads stay
    presence-only: they do not fill home and away. The runner still requires
    3 strictly later timestamps across 3 seconds before it posts. That bar is
    reached when home and away are both read, or when this word agrees and
    Qortrola30's own scoreboard row has readable kills and deaths. Another
    player's row is not that row. Once home and away exist, confirm also
    requires the word to match the side chosen at the start. A gap, a non-end
    frame, or a different result or score starts that count over. ``confirm``
    is the visual read. ``final_confirmed`` also needs a durable seal, every
    supporting frame strictly after the seal end, a stable capture epoch, and
    an anchor that was validated for this session. A free-form string is not that.
    """

    def read(self, img) -> RegionRead:
        from qorcap_cod_hud import read_end
        return read_end(img)


def _as_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _score(value) -> int | None:
    n = _as_int(value)
    if n is None or n < 0:
        return None
    return n


def end_word_fits_side(reason, home, away, side) -> bool | None:
    """Whether the end word matches the side chosen at the start.

    ``None`` when the check does not apply: the word is not victory or defeat,
    the side is not home or away, or a score is missing. DEFEAT fits only when
    that side is strictly behind. VICTORY fits only when that side is strictly
    ahead. A tie fits neither word.
    """
    if reason not in ("victory", "defeat") or side not in ("home", "away"):
        return None
    ours_n, theirs_n = (home, away) if side == "home" else (away, home)
    ours, theirs = _score(ours_n), _score(theirs_n)
    if ours is None or theirs is None:
        return None
    if reason == "defeat":
        return ours < theirs
    return ours > theirs


def side_from_bundle(bundle) -> str | None:
    """The side chosen at the start, from ``side_choice.json``.

    Missing, unreadable, or any other role stays ``None``. This does not read
    a score.
    """
    if bundle is None:
        return None
    path = Path(bundle) / "side_choice.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    if role in ("home", "away"):
        return role
    return None


def _canon(obj) -> str:
    """ASCII-key JSON canonical form for the signal hash. No floats."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, list):
        return "[" + ",".join(_canon(x) for x in obj) + "]"
    if isinstance(obj, dict):
        return "{" + ",".join(_canon(k) + ":" + _canon(obj[k]) for k in sorted(obj)) + "}"
    raise TypeError(type(obj).__name__)


def verify_signal_chain(text: str) -> list[str]:
    """Check prev/hash links and a terminal commitment.

    A matching prefix without a terminal row is incomplete. Local hash links
    are not an anchor and are not completeness.
    """
    prev = SIGNAL_GENESIS
    errs = []
    seq = 0
    rows = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            errs.append(f"line {i} is not json")
            continue
        if not isinstance(row, dict):
            errs.append(f"line {i} is not an object")
            continue
        body = {k: v for k, v in row.items() if k not in ("prev", "hash")}
        if row.get("prev") != prev:
            errs.append(f"line {i} prev does not match")
        try:
            expect = hashlib.sha256(_canon({"prev": prev, "seq": seq, "row": body}).encode("utf-8")).hexdigest()
        except TypeError:
            errs.append(f"line {i} is not canonical")
            expect = None
        if expect is not None and row.get("hash") != expect:
            errs.append(f"line {i} hash does not match")
        claimed = row.get("chain_seq")
        if isinstance(claimed, bool) or claimed != seq:
            errs.append(f"line {i} chain_seq does not match")
        prev = row.get("hash") or prev
        seq += 1
        rows.append(row)
    if not rows or rows[-1].get("kind") != "terminal":
        errs.append("no terminal commitment")
        return errs
    terminals = [n for n, row in enumerate(rows) if row.get("kind") == "terminal"]
    if terminals != [len(rows) - 1]:
        errs.append("terminal is not unique")
    prior = rows[:-1]
    last = rows[-1]
    count = last.get("count")
    entry_count = last.get("entry_count", count)
    if isinstance(count, bool) or isinstance(entry_count, bool) or count != len(prior) or entry_count != len(prior):
        errs.append("terminal count does not match")
    if "entry_count" in last and last.get("entry_count") != count:
        errs.append("terminal entry_count does not match")
    root = SIGNAL_GENESIS if not prior else prior[-1].get("hash")
    if last.get("root") != root:
        errs.append("terminal root does not match")
    if "chain_root_hash" in last and last.get("chain_root_hash") != root:
        errs.append("terminal chain_root does not match")
    if not isinstance(last.get("truncated"), bool):
        errs.append("terminal truncated flag missing")
    if any(key not in last for key in ("count", "entry_count", "first_seq", "last_seq")):
        errs.append("terminal coverage missing")
    if len(prior) == 0:
        if last.get("first_seq") is not None or last.get("last_seq") is not None:
            errs.append("terminal seq range does not match")
    else:
        if last.get("first_seq") != 0 or last.get("last_seq") != len(prior) - 1:
            errs.append("terminal seq range does not match")
    if last.get("complete") is not False:
        errs.append("local hash links are not completeness")
    if last.get("anchored") is not False:
        errs.append("local hash links are not an anchor")
    # truncated:true is a self-consistent declaration. It is not a hash failure.
    # Acceptance treats it as an unknown chain rather than a complete record.
    return errs


def _in_fire_window(edges, screen_us) -> bool:
    """True when an R2 edge is at or before the screen and inside the fire window."""
    if not isinstance(screen_us, int):
        return False
    for t in edges or []:
        n = _as_int(t)
        if n is None:
            continue
        dt = screen_us - n
        if 0 <= dt <= FIRE_WINDOW_US:
            return True
    return False


def _independent_anchor(token, seal_hash) -> bool:
    """A formatted token is not authority evidence.

    ``independent-v1:`` plus 64 hex digits, a boolean, and the seal hash do
    not validate an anchor. This process does not run an authority.
    """
    del token, seal_hash
    return False


ENGINE_CLOCK_SOURCE = "engine_monotonic_ns"


def producer_screen_us(source, input_origin_ns=None) -> int | None:
    """Screen time on the controller's ``t_us`` clock, or None.

    The engine stamps snapshot ``mono_us`` and HID ``t_us`` from one Instant
    and writes that Instant's origin on both. The sample is used only when
    the snapshot origin is nonzero and equals the ``inputs.jsonl`` header
    origin. Media PTS, a child-relative clock, a zero or legacy origin, and
    wall-anchor subtraction stay unknown.
    """
    if not isinstance(source, dict):
        return None
    if source.get("clock_source", source.get("clock")) != ENGINE_CLOCK_SOURCE:
        return None
    origin = source.get("mono_origin_ns")
    if isinstance(origin, bool) or not isinstance(origin, int) or origin <= 0:
        return None
    if isinstance(input_origin_ns, bool) or not isinstance(input_origin_ns, int) or input_origin_ns != origin:
        return None
    sample = source.get("mono_us")
    if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
        return None
    return sample


def _http_status_from_gap(reason: str):
    """A status only when the server answered.

    ``/snapshot.jpg`` answers 200 or 503, so an ``HTTPError`` gap with no
    code is that 503. A timeout, a refused connection, or a URLError got no
    response and has no status.
    """
    if "httperror" in str(reason).lower():
        return 503
    return None


def screen_mono_us(screen_wall_ms, wall_anchor_ms) -> int | None:
    """Place a wall-clock frame on the controller's monotonic clock.

    ``inputs.jsonl`` header ``wall_anchor_ms`` is wall time at ``t_us == 0``.
    The frame clock uses that same ``t_us``. Missing either time means the
    screen cannot be placed, and the caller does not Count it.
    """
    wall = _as_int(screen_wall_ms)
    anchor = _as_int(wall_anchor_ms)
    if wall is None or anchor is None:
        return None
    return (wall - anchor) * 1000


def _stamp_covers(a_us: int, b_us: int, stamps) -> bool:
    for stamp in stamps or []:
        s = _as_int(stamp)
        if s is None:
            continue
        if abs(a_us - s) <= FRAME_CLOCK_GAP_US and abs(b_us - s) <= FRAME_CLOCK_GAP_US:
            return True
    return False


def correlate_pair(*, calibrated: bool, screen_us, input_us, frame_stamps) -> str:
    """One screen time against one controller time, on the frame clock.

    Not Counted when the hook is not calibrated, the screen has no monotonic
    time, or the frame clock has no stamp. The manifest fit is not a stamp.
    A calibrated pair with no input, or no stamp within 500 ms of both, is
    flagged a mismatch.
    """
    if not calibrated or screen_us is None:
        return NOT_COUNTED
    if not frame_stamps:
        return NOT_COUNTED
    if input_us is None:
        return MISMATCH
    screen = _as_int(screen_us)
    inp = _as_int(input_us)
    if screen is None or inp is None:
        return NOT_COUNTED
    if abs(screen - inp) > FRAME_CLOCK_GAP_US or not _stamp_covers(screen, inp, frame_stamps):
        return MISMATCH
    return AGREED


def trigger_edges(rows) -> list[int]:
    """R2 rising edges. The step is the haptic detector's trigger change."""
    prev = None
    edges = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("type") != "in":
            continue
        r2 = _as_int(row.get("r2"))
        t = _as_int(row.get("t_us"))
        if r2 is None or t is None:
            continue
        if prev is not None and r2 - prev > TRIGGER_DELTA:
            edges.append(t)
        prev = r2
    return edges


def options_presses(rows) -> list[int]:
    prev = 0
    presses = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("type") != "in":
            continue
        b = _as_int(row.get("b"))
        t = _as_int(row.get("t_us"))
        if b is None or t is None:
            continue
        if b & OPTIONS_BIT and not prev & OPTIONS_BIT:
            presses.append(t)
        prev = b
    return presses


def nearest_us(times, target) -> int | None:
    target = _as_int(target)
    if target is None:
        return None
    best, best_d = None, None
    for t in times or []:
        n = _as_int(t)
        if n is None:
            continue
        d = abs(n - target)
        if best_d is None or d < best_d:
            best, best_d = n, d
    return best


def recoil_status(haptic, trigger_us, frame_stamps) -> str:
    """Motion next to a trigger, using the haptic row the detector already wrote.

    ``grip_jolt`` and ``hand_motion`` are low in that detector. They are not
    Counted. A high row still needs the frame clock and a trigger within 500 ms.
    """
    if not isinstance(haptic, dict):
        return NOT_COUNTED
    flags = haptic.get("flags") or []
    if not isinstance(flags, list):
        flags = []
    if "grip_jolt" in flags or "hand_motion" in flags:
        return NOT_COUNTED
    if haptic.get("confidence_label") != "high":
        return NOT_COUNTED
    t = _as_int(haptic.get("t_us"))
    if t is None or trigger_us is None or not frame_stamps:
        return NOT_COUNTED
    trig = _as_int(trigger_us)
    if trig is None:
        return NOT_COUNTED
    if abs(t - trig) > FRAME_CLOCK_GAP_US or not _stamp_covers(t, trig, frame_stamps):
        return MISMATCH
    return AGREED


def follows_status(*, calibrated: bool, prior_status: str, this_us, prior_us) -> str:
    """A later HUD event follows one that already agreed, on the frame clock.

    The play chain is hitmarker, then the kill popup, then the killfeed.
    A hook that is not calibrated is not Counted. A calibrated hook whose
    prior step did not agree is flagged a mismatch.
    """
    if not calibrated:
        return NOT_COUNTED
    if prior_status != AGREED:
        return MISMATCH
    a, b = _as_int(this_us), _as_int(prior_us)
    if a is None or b is None or abs(a - b) > FRAME_CLOCK_GAP_US:
        return MISMATCH
    return AGREED


def killfeed_status(*, calibrated: bool, hitmarker_status: str, killfeed_us, hitmarker_us) -> str:
    """A killfeed entry follows a hitmarker that already agreed."""
    return follows_status(calibrated=calibrated, prior_status=hitmarker_status,
                          this_us=killfeed_us, prior_us=hitmarker_us)


def cod_final_agreement(*, feed, scoreboard, end_screen, frame_stamps) -> dict:
    """Intersect independent legs on the frame clock; presence alone is advisory.

    The feed contributes an ownership-qualified covered tally, not generic icons.
    The board supplies K/D and the calibrated end screen supplies the outcome.
    Existing multi-frame visual and seal/anchor gates remain additional requirements.
    """
    def abstain(reason):
        return {"counted": False, "status": NOT_COUNTED, "reason": reason}

    legs = {"killfeed": feed, "scoreboard": scoreboard, "end_screen": end_screen}
    for name, leg in legs.items():
        if not isinstance(leg, dict):
            return abstain(f"missing_{name}")
    if (feed.get("coverage") != "covered" or feed.get("ownership_qualified") is not True
            or feed.get("deduped") is not True or _score(feed.get("kills")) is None):
        return abstain("killfeed_coverage_unknown")
    if (scoreboard.get("calibrated") is not True or _score(scoreboard.get("kills")) is None):
        return abstain("scoreboard_not_read")
    if _score(scoreboard.get("deaths")) is None:
        # No DEATHS column and no owner card deaths: deaths are UNKNOWN, so the three legs
        # cannot agree on K/D. This says so instead of using a guessed number.
        if scoreboard.get("deaths_status") == "UNKNOWN_NO_COLUMN":
            return abstain("scoreboard_deaths_unknown")
        return abstain("scoreboard_not_read")
    if end_screen.get("calibrated") is not True or end_screen.get("outcome") not in ("victory", "defeat"):
        return abstain("end_screen_not_read")
    times = [leg.get("t_us") for leg in legs.values()]
    if any(isinstance(t, bool) or not isinstance(t, int) or t < 0 for t in times):
        return abstain("frame_clock_unknown")
    if max(times) - min(times) > FRAME_CLOCK_GAP_US:
        return abstain("agreement_window_expired")
    if not all(_stamp_covers(t, t, frame_stamps) for t in times):
        return abstain("frame_clock_unmatched")
    if feed["kills"] != scoreboard["kills"]:
        return abstain("killfeed_scoreboard_mismatch")
    # Extra structured values, if actually read by another leg, must also agree.
    for key in ("kills", "deaths", "assists", "outcome"):
        values = [leg[key] for leg in legs.values() if key in leg and leg[key] is not None]
        if any(v != values[0] for v in values[1:]):
            return abstain(f"{key}_mismatch")
    return {"counted": True, "status": AGREED, "reason": "three_leg_agreement"}


def scoreboard_status(*, calibrated: bool, kills, assists, board_kills, board_assists) -> str:
    """Offline kills and assists against the end scoreboard.

    Both sides need both numbers. A missing number is not Counted. Different
    numbers are flagged a mismatch. This hook does not post a final and does
    not change the receipt by itself.
    """
    if not calibrated:
        return NOT_COUNTED
    ks = (_score(kills), _score(assists), _score(board_kills), _score(board_assists))
    if any(n is None for n in ks):
        return NOT_COUNTED
    if ks[0] == ks[2] and ks[1] == ks[3]:
        return AGREED
    return MISMATCH


def _public(rd: RegionRead) -> dict:
    out = {"region": rd.region, "status": rd.status, "reason": rd.reason, "calibrated": bool(rd.calibrated)}
    digits = {}
    for key in ("home", "away", "kills", "assists", "board_kills", "board_assists", "deaths", "confirms"):
        val = getattr(rd, key, None)
        if isinstance(val, int) and not isinstance(val, bool):
            out[key] = val
            digits[key] = val
        else:
            out[key] = None
    tag = getattr(rd, "player_tag", None)
    out["player_tag"] = tag if isinstance(tag, str) else None
    candidates = getattr(rd, "candidates", None)
    out["digits"] = digits
    out["candidates"] = list(candidates) if isinstance(candidates, list) else []
    from qorcap_qordata import evidence_hash
    out["evidence_hash"] = evidence_hash(out)
    return out


class CodSetRunner:
    """Frame loop for the Call of Duty set.

    HUD hooks are logged and are not posted to ``/events``. The only event
    this runner posts is a final, after a calibrated end screen agrees for
    3 strictly later timestamps across 3 seconds. The scored path keeps the
    same result and the same home and away. The owner-row path posts when the
    end word agrees and Qortrola30's row has readable kills and deaths, and
    it does not fill home or away from that row. A frame that is not that end
    screen, a missing pair, a repeated or earlier timestamp, or a gap longer
    than 3 seconds starts the count over. The K/A scoreboard hook is not that
    row and does not post a final. In-match signals stay on ``cod_signals.jsonl``.
    """

    def __init__(self, post, hud=None, end=None, log_path: Path | None = None,
                 side: str | None = None) -> None:
        self.post = post
        self.hud = hud or CodHudReader()
        self.end = end or CodEndScreenReader()
        self.log_path = log_path
        # Side chosen at the start. Used only once home and away are both read.
        self.side = side if side in ("home", "away") else None
        self.final: dict | None = None
        self.reader = None  # no Madden scorebug grammar
        self.inputs: list = []
        self.haptics: list = []
        self.frame_stamps: list = []
        self.wall_anchor_ms = None
        self.input_origin_ns = None
        self.stats = {
            "frames": 0, "agreed": 0, "errors": 0,
            "hud_not_read": 0, "end_screen_not_read": 0, "posted_final": 0,
            "not_counted": 0, "flagged_mismatch": 0,
            "end_direction_mismatch": 0,
            "signal_log_failures": 0,
            "capture_gaps": 0,
        }
        self.capture_epoch = None
        self.epoch_fault = False
        self.independent_anchor = None
        self.seal_hash = None
        self.seal_durable = False
        self.anchor_validated = False
        self.session_id = None
        self.capture_generation = None
        self._bound_clock = None
        self._support = []
        self._attempt_seq = 0
        self._frame_end = {"status": "not_evaluated", "reason": None}
        self._frame_emitted = False
        self._pending_reads = []
        self._pending_signals = []
        self._logged = False
        self._chain_prev = SIGNAL_GENESIS
        self._chain_seq = 0
        self._last_snap = None
        self._real_frames: list = []
        self._own_kills: list = []
        self._feed_present = False
        self._source = None
        self._signals_closed = False
        # One lock serializes signal append, durable-length recovery, and close.
        # It is re-entrant because close commits its terminal through `_write`.
        self._signal_lock = threading.RLock()
        self._log_fault = False
        # Signal log length before a failed append. The next write cuts back to it.
        self._durable_len = None
        self.menu_exit_us = None
        self.lag_us_override = None
        self._last_gated_hit_us = None
        self._last_feed_us = None
        # Own kills read by gamertag from the killfeed (qorcap_cod_killfeed).
        self._kill_tally = None
        self._kill_read_us = None
        self._lag = None
        self._pair = None
        self._n = 0
        self._t0 = None
        self._last_t = None
        self.last_hitmarker = NOT_COUNTED
        self.last_hitmarker_us = None
        # Stop-time health only. These do not change what a frame posts or logs.
        self.end_word_first_ms = None
        self.end_word_last_ms = None
        # Owner-row stop health (info only; none of these changes a decision):
        # - owner_row_ever_read: at least one frame passed the per-frame owner-row gate
        #   (tag, kills, deaths). Kept for compatibility. It does not mean the row was accepted.
        # - owner_row_good_frames: how many frames passed that per-frame gate.
        # - owner_row_accepted: the row met the 3-frame / 3000 ms agreement and the owner-row
        #   consensus and was committed with the final (the cod_session.json owner_row).
        # cod_end_stop.json owner_row_seen is a different, EndStop field: the row was the read
        # that confirmed the end screen (splash+owner_row or a sustained run with the row).
        self.owner_row_ever_read = False
        self.owner_row_good_frames = 0
        self.owner_row_accepted = False
        self._observe_lock = threading.Lock()
        self._published = None
        # Optional practice auto-stop watcher (qorcap_cod_endstop). Sees the reads this loop already
        # made and the frame; it cannot change what a frame posts or logs.
        self.end_watch = None

    def set_signals(self, inputs, frame_stamps, haptics, wall_anchor_ms) -> None:
        """Replace the clocks used on the next frame. Empty lists count nothing."""
        self.inputs = list(inputs or [])
        self.frame_stamps = list(frame_stamps or [])
        self.haptics = list(haptics or [])
        self.wall_anchor_ms = wall_anchor_ms
        origin = None
        for row in self.inputs:
            if isinstance(row, dict) and row.get("type") == "header":
                raw = row.get("mono_origin_ns")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    origin = raw
                break
        self.input_origin_ns = origin

    def _screen_us(self):
        return producer_screen_us(self._source, self.input_origin_ns)

    def _note_end_word(self, t_ms: int) -> None:
        """Remember the first and last frame time a victory or defeat word was read."""
        if self.end_word_first_ms is None:
            self.end_word_first_ms = t_ms
        self.end_word_last_ms = t_ms

    def publish_reader_observation(self, *, reader_error=None, cod_signal_error=None, reader_tick=None) -> None:
        """Store one coherent copy of the stop fields. The copy is replaced, not merged.

        The lock covers only this copy. A later frame can replace it. A stop
        that did not join the reader keeps the copy it already took.
        """
        snap = {
            "stats": {key: _stat_count(self.stats, key) for key in READER_STATS_KEYS},
            "reader_error": reader_error if isinstance(reader_error, str) else None,
            "cod_signal_error": cod_signal_error if isinstance(cod_signal_error, str) else None,
            "reader_tick": _optional_ms(reader_tick),
            "end_word_first_ms": _optional_ms(self.end_word_first_ms),
            "end_word_last_ms": _optional_ms(self.end_word_last_ms),
            "owner_row_ever_read": bool(self.owner_row_ever_read),
            "owner_row_good_frames": _stat_count({"n": self.owner_row_good_frames}, "n"),
            "owner_row_accepted": bool(self.owner_row_accepted),
            "published": True,
        }
        with self._observe_lock:
            self._published = snap

    def published_reader_observation(self) -> dict | None:
        """Return the last published copy, or None when the reader has not published."""
        with self._observe_lock:
            snap = self._published
            if not isinstance(snap, dict):
                return None
            stats = snap.get("stats")
            return {
                "stats": dict(stats) if isinstance(stats, dict) else None,
                "reader_error": snap.get("reader_error"),
                "cod_signal_error": snap.get("cod_signal_error"),
                "reader_tick": snap.get("reader_tick"),
                "end_word_first_ms": snap.get("end_word_first_ms"),
                "end_word_last_ms": snap.get("end_word_last_ms"),
                "owner_row_ever_read": snap.get("owner_row_ever_read"),
                "owner_row_good_frames": snap.get("owner_row_good_frames"),
                "owner_row_accepted": snap.get("owner_row_accepted"),
                "published": True,
            }

    def on_frame(self, t_ms: int, img, source=None) -> None:
        self._source = source if isinstance(source, dict) else None
        if isinstance(self._source, dict) and isinstance(self._source.get("clock_id"), str) and self._source.get("clock_id"):
            if self._bound_clock is None:
                self._bound_clock = self._source["clock_id"]
            elif self._source["clock_id"] != self._bound_clock:
                self.epoch_fault = True
                self._clear_end_candidate()
        self.stats["frames"] += 1
        self._frame_emitted = False
        self._frame_end = {"status": "not_evaluated", "reason": None}
        self._pending_reads = []
        self._pending_signals = []
        self._note_capture_epoch()
        end_rd = owner_rd = hud_rd = None
        try:
            try:
                reads = list(self.hud.read(img))
                hud_rd = reads
            except Exception as e:
                self.stats["errors"] += 1
                print(f"Call of Duty HUD hook error: {type(e).__name__}: {e}", file=sys.stderr)
                reads = []
                self._write({
                    "t_ms": int(t_ms),
                    "kind": "hook_fault",
                    "hook": "hud",
                    "reason_code": "hud_hook_error",
                    "error": type(e).__name__,
                    "evaluated": True,
                })
            by_name = {rd.region: rd for rd in reads}
            signals = self._signals(int(t_ms), by_name)
            for rd in reads:
                if not (rd.ok and rd.calibrated):
                    self.stats["hud_not_read"] += 1
            for row in signals:
                if row["status"] == AGREED:
                    self.stats["agreed"] += 1
                elif row["status"] == MISMATCH:
                    self.stats["flagged_mismatch"] += 1
                else:
                    self.stats["not_counted"] += 1
            self._pending_reads = reads
            self._pending_signals = signals
            self._keep_real(img)
            self._note_lag(int(t_ms), img)
            try:
                end = self.end.read(img)
                end_rd = end
            except Exception as e:
                self.stats["errors"] += 1
                print(f"Call of Duty end-screen hook error: {type(e).__name__}: {e}", file=sys.stderr)
                self._frame_end = {"status": "fault", "reason": "end_hook_error"}
                self._clear_end_candidate()
                self._kill_feed_fault()
                return
            when = t_ms if isinstance(t_ms, int) and not isinstance(t_ms, bool) else None
            if (
                when is not None
                and end.ok and end.calibrated
                and isinstance(end.reason, str) and end.reason in ("victory", "defeat")
            ):
                self._note_end_word(when)
            if self.final is None:
                self._read_kill_feed(img, bool(end.ok and end.calibrated))
            if self.final is not None:
                self._frame_end = {"status": "already_final", "reason": None}
                return
            # Home and away come only from this end-screen read. A scoreboard
            # kill, assist, or team total is not that pair. The owner row is a
            # separate read and is not copied into home or away.
            if not (end.ok and end.calibrated):
                self.stats["end_screen_not_read"] += 1
                self._frame_end = {"status": "abstain", "reason": "end_not_read"}
                self._clear_end_candidate()
                return
            if when is None:
                self._frame_end = {"status": "abstain", "reason": "frame_time_missing"}
                self._clear_end_candidate()
                return
            home, away = _score(end.home), _score(end.away)
            if home is not None and away is not None and isinstance(end.reason, str) and end.reason:
                self._post_scored_end(int(t_ms), when, end, home, away)
                return
            try:
                from qorcap_cod_hud import read_owner_row
                owner = read_owner_row(img)
                owner_rd = owner
            except Exception as e:
                self.stats["errors"] += 1
                print(f"Call of Duty owner-row hook error: {type(e).__name__}: {e}", file=sys.stderr)
                self.stats["end_screen_not_read"] += 1
                self._frame_end = {"status": "fault", "reason": "owner_hook_error"}
                self._clear_end_candidate()
                return
            self._post_owner_end(int(t_ms), when, end, owner)
        finally:
            self._emit_frame(int(t_ms))
            watch = self.end_watch
            if watch is not None:
                try:
                    watch(t_ms, end_rd, owner_rd, self._source, hud_rd, img)
                except Exception:
                    pass

    def _signals(self, t_ms: int, by_name: dict) -> list[dict]:
        screen_us = self._screen_us()
        stamps = self.frame_stamps
        edges = trigger_edges(self.inputs)
        trigger = nearest_us(edges, screen_us)
        hit = by_name.get("hitmarker")
        visual = bool(hit and hit.ok and hit.calibrated)
        fired = _in_fire_window(edges, screen_us)
        if visual and not fired:
            hit_status, hit_reason = NOT_COUNTED, "outside_fire_window"
            gated = False
        else:
            hit_status = correlate_pair(
                calibrated=visual, screen_us=screen_us, input_us=trigger, frame_stamps=stamps)
            hit_status, hit_reason = self._hold_counts(hit_status, None, screen_us)
            gated = visual and fired
        if gated and isinstance(screen_us, int):
            self._last_gated_hit_us = screen_us
        self.last_hitmarker = hit_status
        self.last_hitmarker_us = screen_us if hit_status == AGREED else None
        popup = by_name.get("kill_popup")
        popup_status = follows_status(
            calibrated=bool(popup and popup.ok and popup.calibrated),
            prior_status=hit_status, this_us=screen_us, prior_us=self.last_hitmarker_us)
        popup_status, popup_reason = self._hold_counts(popup_status, None, screen_us)
        popup_us = screen_us if popup_status == AGREED else None
        feed = by_name.get("killfeed")
        feed_visual = bool(feed and feed.ok and feed.calibrated)
        near = (
            feed_visual and isinstance(screen_us, int) and isinstance(self._last_gated_hit_us, int)
            and 0 <= screen_us - self._last_gated_hit_us <= KILLFEED_NEAR_US
        )
        if feed_visual and not near:
            feed_status, feed_reason = NOT_COUNTED, "no_nearby_hitmarker"
        else:
            feed_status = follows_status(
                calibrated=feed_visual, prior_status=popup_status,
                this_us=screen_us, prior_us=popup_us)
            feed_status, feed_reason = self._hold_counts(feed_status, None, screen_us)
        if feed_visual and near and isinstance(screen_us, int) and not self._feed_present:
            # A generic icon is one candidate. It is not ownership-qualified.
            self._own_kills.append({
                "t_us": screen_us,
                "source_id": self._source_id(),
                "ownership_qualified": False,
            })
            self._last_feed_us = screen_us
        self._feed_present = bool(feed_visual and near)
        board = by_name.get("scoreboard")
        board_status = scoreboard_status(
            calibrated=bool(board and board.ok and board.calibrated),
            kills=getattr(board, "kills", None) if board else None,
            assists=getattr(board, "assists", None) if board else None,
            board_kills=getattr(board, "board_kills", None) if board else None,
            board_assists=getattr(board, "board_assists", None) if board else None)
        pause = by_name.get("pause")
        options = nearest_us(options_presses(self.inputs), screen_us)
        pause_status = correlate_pair(
            calibrated=bool(pause and pause.ok and pause.calibrated),
            screen_us=screen_us, input_us=options, frame_stamps=stamps)
        pause_status, pause_reason = self._hold_counts(pause_status, None, screen_us)
        motion = _nearest_haptic(self.haptics, trigger if trigger is not None else screen_us)
        motion_status = recoil_status(motion, trigger, stamps)
        motion_status, motion_reason = self._hold_counts(motion_status, None, screen_us)
        board_status, board_reason = self._hold_counts(board_status, None, screen_us)
        return [
            {"signal": "hitmarker", "status": hit_status, "reason": hit_reason, "gated": gated, "counted": False, "advisory": True},
            {"signal": "kill_popup", "status": popup_status, "reason": popup_reason, "counted": False, "advisory": True},
            {"signal": "killfeed", "status": feed_status, "reason": feed_reason, "counted": False, "advisory": True},
            {"signal": "pause", "status": pause_status, "reason": pause_reason},
            {"signal": "recoil", "status": motion_status, "reason": motion_reason},
            {"signal": "scoreboard", "status": board_status, "reason": board_reason, "counted": False, "advisory": True},
            {"signal": "end_screen", "status": NOT_COUNTED, "reason": None},
        ]

    def _clear_end_candidate(self) -> None:
        self._pair = None
        self._n = 0
        self._t0 = None
        self._last_t = None
        self._support = []

    def _capture_ref(self) -> dict:
        src = self._source if isinstance(self._source, dict) else {}
        mono = self._screen_us()
        capture_id = src.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id:
            capture_id = src.get("frame_id") if isinstance(src.get("frame_id"), str) else None
        generation = src.get("capture_generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = self.capture_generation
        return {
            "capture_id": capture_id,
            "mono_us": mono,
            "epoch": src.get("epoch"),
            "capture_generation": generation,
            "session_id": src.get("session_id") or self.session_id,
        }

    def _begin_end_candidate(self, t_ms: int, key: tuple) -> None:
        self._pair = key
        self._n = 1
        self._t0 = t_ms
        self._last_t = t_ms
        self._support = [self._capture_ref()]

    def _post_scored_end(self, t_ms: int, when: int, end: RegionRead, home: int, away: int) -> None:
        """The path that already has home and away. The word has to fit the side."""
        confirm = bool(end.confirm)
        fits = end_word_fits_side(end.reason, home, away, self.side)
        direction_note = None
        # Victory and defeat confirm only when the word matches the side.
        # A missing side fails closed. Other reasons are unchanged.
        if end.reason in ("victory", "defeat") and fits is not True:
            confirm = False
            self.stats["end_direction_mismatch"] += 1
            if fits is False:
                direction_note = (
                    "The end-screen word does not match the side chosen at the start, "
                    "so it is not confirmed."
                )
            else:
                direction_note = (
                    "The side chosen at the start is not on this read, "
                    "so the end-screen word is not confirmed."
                )
        agreed = self._offer(when, end.reason, confirm, home, away)
        if agreed is None:
            self._frame_end = {"status": "not_ready", "reason": end.reason, "visual_ok": confirm}
            return
        gate = self.final_eligibility(visual_ok=confirm, evidence_us=self._evidence_us())
        agreement = self._final_agreement(end, None)
        confirmed = gate["confirmed"] and agreement["counted"]
        self._frame_end = {
            "status": "scored", "reason": end.reason, "visual_ok": confirm, "eligible": confirmed,
        }
        notes = [
            "Call of Duty end screen agreed. This is the end of the match.",
            "The receipt quarter is 4 because this end screen has no game clock. It is not a Madden quarter.",
        ]
        if direction_note:
            notes.append(direction_note)
        self._frame_end["agreement"] = agreement
        if not agreement["counted"]:
            notes.append("CoD agreement: " + agreement["reason"] + ".")
        if not confirmed:
            notes.append("The end-screen score is not Counted as confirmed.")
        self._emit_frame(t_ms)
        self._commit_final(t_ms, agreed, end.reason, confirmed, home, away, notes, owner=None, visual=confirm, agreement=agreement)

    def _post_owner_end(self, t_ms: int, when: int, end: RegionRead, owner: RegionRead) -> None:
        """End word plus Qortrola30's row. Home and away stay unread.

        The word names victory or defeat. The row says whose match it is.
        Kills and deaths have to be read. Assists count only when that column
        is on the board. A missing row, a different tag, or an unreadable
        digit is not Counted.
        """
        from qorcap_cod_hud import owner_deaths_ok, owner_deaths_status
        kills = _score(getattr(owner, "kills", None))
        deaths = _score(getattr(owner, "deaths", None))
        assists = _score(getattr(owner, "assists", None))
        confirms = _score(getattr(owner, "confirms", None))
        tag = getattr(owner, "player_tag", None)
        # Deaths may be None only with deaths_status UNKNOWN_NO_COLUMN (the board has no
        # DEATHS column and no owner card deaths were read). A DEATHS column that was not
        # read still stops the row. The 3-frame/3000 ms bar is unchanged.
        deaths_status = owner_deaths_status(owner)
        ready = (
            isinstance(end.reason, str)
            and end.reason in ("victory", "defeat")
            and bool(end.confirm)
            and owner.ok
            and owner.calibrated
            and same_owner_tag(tag or "")
            and kills is not None
            and (deaths is not None or owner_deaths_ok(owner))
        )
        if not ready:
            self.stats["end_screen_not_read"] += 1
            self._frame_end = {"status": "abstain", "reason": "owner_row_not_read"}
            stage = getattr(owner, "stage", None)
            if isinstance(stage, str) and stage:
                # Which owner-row step stopped this frame. Diagnostic only: not a score and not Counted.
                self._frame_end["owner_stage"] = stage
            detail = getattr(owner, "stage_detail", None)
            if isinstance(detail, dict) and detail:
                # The exact check that stopped the kills or deaths read, with its scores.
                # Diagnostic only: the decision above is unchanged and nothing here is Counted.
                self._frame_end["owner_stage_detail"] = dict(detail)
            self._clear_end_candidate()
            return
        self.owner_row_ever_read = True
        self.owner_row_good_frames += 1
        extra = (OWNER_TAG.casefold(), kills, deaths, assists, confirms)
        if deaths_status != "column":
            # Agreement across frames is on kills plus the deaths status. Column boards keep the old key.
            extra = extra + (deaths_status,)
        agreed = self._offer(when, end.reason, True, None, None, extra=extra)
        if agreed is None:
            self._frame_end = {"status": "not_ready", "reason": end.reason, "visual_ok": False}
            return
        from qorcap_cod_hud import consensus_owner_row
        verdict = consensus_owner_row(self._real_frames, self.own_kill_coverage())
        if verdict["status"] != "accepted":
            self._frame_end = {
                "status": "abstain", "reason": verdict["reason"], "consensus": "abstain", "visual_ok": False,
            }
            if verdict["reason"] != "short_real_window":
                self._clear_end_candidate()
            return
        notes = [
            "Call of Duty end screen agreed. This is the end of the match.",
            "The receipt quarter is 4 because this end screen has no game clock. It is not a Madden quarter.",
            (
                f"The end word is {end.reason}. {OWNER_TAG}'s scoreboard row was read: "
                f"kills {kills}, deaths {deaths}."
                if deaths is not None else
                f"The end word is {end.reason}. {OWNER_TAG}'s scoreboard row was read: "
                f"kills {kills}. Deaths are UNKNOWN: the board has no DEATHS column and no "
                "owner card deaths were read. They are not zero."
            ),
            "Home and away were not read. Those row numbers are not a home or away score.",
        ]
        if assists is None:
            notes.append("Assists were not on this board.")
        else:
            notes.append(f"Assists on that row: {assists}.")
        if confirms is not None:
            notes.append(f"Confirms on that row: {confirms}.")
        owner_doc = {
            "tag": OWNER_TAG,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "confirms": confirms,
            "result": end.reason,
        }
        if deaths_status != "column":
            # Shape change only off DEATHS-column boards: deaths_status and the card name check.
            owner_doc["deaths_status"] = deaths_status
            owner_doc["card_name_match"] = getattr(owner, "card_name_match", None)
        gate = self.final_eligibility(visual_ok=True, evidence_us=self._evidence_us())
        agreement = self._final_agreement(end, owner)
        confirmed = gate["confirmed"] and agreement["counted"]
        self.owner_row_accepted = True
        self._frame_end = {
            "status": "accepted", "reason": end.reason, "visual_ok": True, "eligible": confirmed,
        }
        self._frame_end["agreement"] = agreement
        if not agreement["counted"]:
            notes.append("CoD agreement: " + agreement["reason"] + ".")
        if not confirmed:
            notes.append("The end-screen score is not Counted as confirmed.")
        self._emit_frame(t_ms)
        self._commit_final(t_ms, agreed, end.reason, confirmed, None, None, notes, owner=owner_doc, visual=True, agreement=agreement)

    def _final_agreement(self, end, owner) -> dict:
        feed = self.own_kill_coverage()
        if not (self._kill_tally is not None and self._kill_tally.frames):
            feed["t_us"] = self._last_feed_us
        board = None if owner is None else {
            "kills": _score(owner.kills), "deaths": _score(owner.deaths),
            "assists": _score(owner.assists), "calibrated": bool(owner.ok and owner.calibrated),
            "t_us": self._screen_us(),
            "deaths_status": getattr(owner, "deaths_status", None),
        }
        return cod_final_agreement(
            feed=feed, scoreboard=board,
            end_screen={"calibrated": bool(end.ok and end.calibrated and end.confirm),
                        "outcome": end.reason, "t_us": self._screen_us()},
            frame_stamps=self.frame_stamps)

    def _commit_final(self, t_ms, agreed, reason, confirmed, home, away, notes, owner, visual=False, agreement=None) -> None:
        confirmed = bool(confirmed and agreement and agreement.get("counted") is True)
        event = {
            "type": "final",
            "basis": "end_menu",
            "method": "end_menu_read",
            "home": home,
            "away": away,
            "quarter": 4,
            "t_ms": agreed["t_ms"],
            "agreement": agreement,
            "counted": bool(confirmed and agreement and agreement.get("counted")),
            "visual_confirmed": bool(visual),
            "final_confirmed": bool(confirmed),
            "notes": notes,
            "game_set": "cod",
            "end_screen": {
                "frames": agreed["frames"],
                "region": END_NAME,
                "until_ms": agreed["until_ms"],
            },
        }
        if owner is not None:
            event["owner_row"] = owner
        self.final = event
        self.stats["posted_final"] += 1
        logged = {
            "t_ms": int(t_ms), "kind": "end_screen", "status": AGREED,
            "final_confirmed": confirmed, "counted": bool(confirmed), "agreement": agreement, "home": home, "away": away,
            "result": reason, "frames": agreed["frames"],
        }
        if owner is not None:
            logged["owner_row"] = owner
        self._write(logged)
        self.post(event)

    def _offer(self, t_ms: int, reason: str, confirm: bool, home, away, extra=None) -> dict | None:
        """One more end-screen sample. None until the bar is met.

        The sample counts only when its timestamp is strictly later than the
        last accepted one, the gap is within the cap, and the result and the
        pair are unchanged. ``extra`` is the owner row when home and away are
        unread. Anything else starts a new candidate at this timestamp and
        does not post.
        """
        key = (reason, bool(confirm), home, away, extra)
        if key != self._pair or not isinstance(self._last_t, int):
            self._begin_end_candidate(t_ms, key)
            return None
        if t_ms <= self._last_t or t_ms - self._last_t > END_AGREE_GAP_MS:
            self._begin_end_candidate(t_ms, key)
            return None
        self._n += 1
        self._last_t = t_ms
        self._support.append(self._capture_ref())
        t0 = self._t0 if isinstance(self._t0, int) else t_ms
        if self._n >= END_AGREE_FRAMES and t_ms - t0 >= END_AGREE_SPAN_MS:
            return {"home": home, "away": away, "frames": self._n, "t_ms": t0, "until_ms": t_ms}
        return None

    def _keep_real(self, img) -> None:
        try:
            from qorcap_cod_hud import frame_is_real, _pixel_id
            if not frame_is_real(img):
                return
        except Exception:
            return
        stored = img.copy() if hasattr(img, "copy") else img
        src = self._source if isinstance(self._source, dict) else {}
        captured = src.get("captured_us")
        if isinstance(captured, bool) or not isinstance(captured, int):
            captured = None
        ref = self._capture_ref()
        self._real_frames.append({
            "img": stored,
            "source_id": src.get("frame_id") if isinstance(src.get("frame_id"), str) else None,
            "capture_id": ref["capture_id"],
            "seq": src.get("seq") if isinstance(src.get("seq"), int) and not isinstance(src.get("seq"), bool) else None,
            "captured_us": captured,
            "mono_us": ref["mono_us"],
            "epoch": ref["epoch"],
            "capture_generation": ref["capture_generation"],
            "session_id": ref["session_id"],
            "pixel_id": _pixel_id(stored),
        })
        if len(self._real_frames) > 5:
            del self._real_frames[:-5]

    def _source_id(self):
        src = self._source if isinstance(self._source, dict) else {}
        frame_id = src.get("frame_id")
        return frame_id if isinstance(frame_id, str) and frame_id else None

    def _kill_feed_fault(self) -> None:
        if self._kill_tally is not None:
            self._kill_tally.fault()

    def _read_kill_feed(self, img, end_screen: bool) -> None:
        """Own kills by gamertag from the killfeed, on the frame clock.

        The end board shows no feed, so it only moves the tally's time on. Feed
        frames are read about 4 times a second, the rate it was checked at.
        """
        try:
            from qorcap_cod_killfeed import KillfeedTally, read_killfeed_lines, READ_EVERY_US
        except Exception:
            return
        if self._kill_tally is None:
            self._kill_tally = KillfeedTally()
        tally = self._kill_tally
        screen_us = self._screen_us()
        if end_screen:
            if tally.frames and (not isinstance(screen_us, int) or tally.last_us is None or screen_us > tally.last_us):
                tally.hold(screen_us)
            return
        if isinstance(screen_us, int) and not isinstance(screen_us, bool) and isinstance(self._kill_read_us, int):
            if 0 <= screen_us - self._kill_read_us < READ_EVERY_US:
                return
        try:
            read = read_killfeed_lines(img)
        except Exception as e:
            print(f"Call of Duty killfeed hook error: {type(e).__name__}: {e}", file=sys.stderr)
            tally.fault()
            return
        if not (isinstance(read, dict) and read.get("ok") is True) and tally.frames == 0:
            # Nothing placed yet (no image or no layout): the legacy path stands.
            return
        self._kill_read_us = screen_us if isinstance(screen_us, int) else None
        tally.update(screen_us, read)

    def own_kill_coverage(self) -> dict:
        """Unknown unless every kill is ownership-qualified, deduplicated, and covered.

        Once the killfeed has been read by gamertag, its tally is the feed leg.
        """
        if self._kill_tally is not None and self._kill_tally.frames:
            return self._kill_tally.coverage()
        events = list(self._own_kills)
        if not events:
            return {"coverage": "unknown", "kills": None, "ownership_qualified": False, "deduped": False}
        qualified = all(event.get("ownership_qualified") is True for event in events)
        ids = [event.get("source_id") for event in events]
        deduped = all(isinstance(item, str) and item for item in ids) and len(set(ids)) == len(ids)
        covered = qualified and deduped and all(isinstance(event.get("t_us"), int) for event in events)
        return {
            "coverage": "covered" if covered else "unknown",
            "kills": len(events) if covered else None,
            "ownership_qualified": qualified,
            "deduped": deduped,
        }

    def _note_lag(self, t_ms: int, img) -> None:
        try:
            from qorcap_lag import LagProbe
        except Exception:
            return
        if self._lag is None:
            self._lag = LagProbe()
        try:
            frame_id = self._source.get("frame_id") if isinstance(self._source, dict) else None
            self._lag.observe(
                img, self.inputs, self._screen_us(),
                source_frame=frame_id if isinstance(frame_id, str) else None)
        except Exception:
            return
        if self.log_path is None or not self._lag.samples:
            return
        try:
            self._lag.write(Path(self.log_path).with_name("lag_band.v0.json"))
        except OSError:
            pass

    def _options_release_us(self):
        """Last OPTIONS release on the input log. None when OPTIONS is still down or absent."""
        prev = 0
        release = None
        for row in self.inputs or []:
            if not isinstance(row, dict) or row.get("type") != "in":
                continue
            b = _as_int(row.get("b"))
            t = _as_int(row.get("t_us"))
            if b is None or t is None:
                continue
            if prev & OPTIONS_BIT and not b & OPTIONS_BIT:
                release = t
            prev = b
        if prev & OPTIONS_BIT:
            return None
        return release

    def _seal_window(self) -> dict:
        from qorcap_seal import seal_exclusion
        lag = self.lag_us_override
        if lag is None and self._lag is not None:
            lag = self._lag.measured_us()
        # Pause-menu exit is not read. Missing it keeps the window unresolved.
        return seal_exclusion(
            options_release_us=self._options_release_us(), menu_exit_us=self.menu_exit_us,
            lag_us=lag, frame_us=16_667)

    def _hold_counts(self, status, reason, t_us):
        """An unresolved seal window does not release an agreed row into counts."""
        if status != AGREED:
            return status, reason
        from qorcap_seal import event_released
        window = self._seal_window()
        if event_released(window, t_us if isinstance(t_us, int) else None):
            return status, reason
        return NOT_COUNTED, window.get("reason") or "seal_window_unresolved"

    def _note_capture_epoch(self) -> None:
        src = self._source if isinstance(self._source, dict) else {}
        epoch = src.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool):
            if self.capture_epoch is None:
                self.capture_epoch = epoch
            elif self.capture_epoch != epoch:
                self.epoch_fault = True
        session = src.get("session_id")
        if isinstance(session, str) and session:
            if not isinstance(self.session_id, str) or not self.session_id:
                self.session_id = session
            elif self.session_id != session:
                self.epoch_fault = True
        generation = src.get("capture_generation")
        if isinstance(generation, int) and not isinstance(generation, bool):
            if self.capture_generation is None:
                self.capture_generation = generation
            elif self.capture_generation != generation:
                self.epoch_fault = True

    def _evidence_us(self):
        """Evidence time on the controller clock. A wall clock or an unmatched origin is not evidence."""
        return self._screen_us()

    def final_eligibility(self, *, visual_ok: bool, evidence_us) -> dict:
        """One gate for the scored path and the owner path.

        A visual read can still post. ``confirmed`` needs every supporting
        frame strictly after ``end_us``, unique capture ids, a durable seal,
        a stable epoch, and a validated anchor. Missing any of those keeps
        the final unconfirmed. This does not invent a lag or an anchor.
        """
        from qorcap_qordata import eligibility_decision

        window = self._seal_window()
        end_us = window.get("end_us") if window.get("releases_counts") is True else None
        frames = list(self._support)
        support_ids = [frame.get("capture_id") for frame in frames]
        for item in self._real_frames:
            capture_id = item.get("capture_id") or item.get("source_id")
            if capture_id in support_ids:
                continue
            frames.append({
                "capture_id": capture_id,
                "mono_us": item.get("mono_us"),
                "epoch": item.get("epoch"),
                "capture_generation": item.get("capture_generation"),
                "session_id": item.get("session_id") or self.session_id,
            })
        epoch_ok = (
            isinstance(self.capture_epoch, int)
            and not isinstance(self.capture_epoch, bool)
            and self.epoch_fault is not True
        )
        # A boolean and a formatted token are not authority evidence.
        anchor_ok = _independent_anchor(self.independent_anchor, self.seal_hash)
        decision = eligibility_decision(
            path="shared",
            end_us=end_us,
            frames=frames,
            seal_ready=self.seal_durable is True and window.get("releases_counts") is True,
            anchor_ready=anchor_ok,
            epoch_valid=epoch_ok,
            lag_present=window.get("releases_counts") is True,
            session_id=self.session_id,
            capture_generation=self.capture_generation,
            binding_epoch=self.capture_epoch,
        )
        confirmed = bool(visual_ok) and decision["eligible"]
        after = bool(frames) and "pre_exclusion_frame" not in decision["reason_codes"] and "boundary_not_after_end" not in decision["reason_codes"]
        return {
            "confirmed": confirmed,
            "visual_ok": bool(visual_ok),
            "after_end_us": after and confirmed,
            "epoch_ok": epoch_ok,
            "anchor_ok": anchor_ok,
            "end_us": end_us,
            "reason": decision["reason"],
            "reason_codes": decision["reason_codes"],
            "evidence_us": evidence_us,
        }

    def note_capture_failure(self, *, reason_code: str, attempt_seq=None, http_status=None,
                             observed_mono_us=None, detail=None) -> bool:
        """Log one capture failure and drop the current final candidate.

        The row does not inherit the previous frame's capture id or mono time.
        """
        self._clear_end_candidate()
        if isinstance(attempt_seq, int) and not isinstance(attempt_seq, bool):
            self._attempt_seq = attempt_seq
        else:
            self._attempt_seq += 1
            attempt_seq = self._attempt_seq
        return self._write({
            "t_ms": 0,
            "kind": "capture_failure",
            "reason_code": reason_code,
            "reason": detail if detail is not None else reason_code,
            "attempt_seq": attempt_seq,
            "http_status": http_status,
            "response": "http" if http_status is not None else "none",
            "capture_id": None,
            "capture_mono_us": None,
            "mono_us": None,
            "observed_mono_us": observed_mono_us,
            "anchored": False,
            "evaluated": True,
            "_no_source": True,
        })

    def note_capture_gap(self, reason: str, http_status=None, observed_mono_us=None) -> bool:
        """Record an HTTP, decode, provenance, or API failure on the signal chain."""
        from qorcap_qordata import reason_code_for

        self.stats["capture_gaps"] = self.stats.get("capture_gaps", 0) + 1
        status = http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else _http_status_from_gap(reason)
        if isinstance(observed_mono_us, bool) or not isinstance(observed_mono_us, int):
            observed_mono_us = time.monotonic_ns() // 1000
        return self.note_capture_failure(
            reason_code=reason_code_for(reason),
            detail=str(reason),
            http_status=status,
            observed_mono_us=observed_mono_us,
        )

    def _emit_frame(self, t_ms: int) -> None:
        if self._frame_emitted:
            return
        self._frame_emitted = True
        self._stream_hud(int(t_ms), self._pending_reads, self._pending_signals, self._frame_end)

    def _source_row(self) -> dict:
        src = self._source if isinstance(self._source, dict) else {}
        seq = src.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            seq = None
        captured = src.get("captured_us")
        if isinstance(captured, bool) or not isinstance(captured, int):
            captured = None
        frame_id = src.get("frame_id") if isinstance(src.get("frame_id"), str) else None
        clock = src.get("clock") if isinstance(src.get("clock"), str) else None
        row = {"frame_id": frame_id, "seq": seq, "captured_us": captured, "clock": clock, "anchored": False}
        session = src.get("session_id") if isinstance(src.get("session_id"), str) and src.get("session_id") else self.session_id
        if isinstance(session, str) and session:
            row["session_id"] = session
        epoch = src.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool):
            row["epoch"] = epoch
        origin = src.get("mono_origin_ns")
        if isinstance(origin, int) and not isinstance(origin, bool):
            row["mono_origin_ns"] = origin
        generation = src.get("capture_generation")
        if isinstance(generation, int) and not isinstance(generation, bool):
            row["capture_generation"] = generation
        return row

    def _stream_hud(self, t_ms: int, reads, signals, end_eval) -> None:
        regions = [_public(rd) for rd in reads]
        source = self._source_row()
        row = {
            "t_ms": int(t_ms),
            "kind": "set" if not self._logged else "hud",
            "regions": regions,
            "signals": signals,
            "source_frame": source,
            "frame_index": source["seq"],
            "anchored": False,
            "poll_index": self.stats["frames"] - 1,
            "eval_n": self.stats["frames"],
            "evaluated": True,
            "end_eval": end_eval if isinstance(end_eval, dict) else {"status": "not_evaluated", "reason": None},
        }
        if not self._logged:
            self._logged = True
            row["frame_clock_gap_us"] = FRAME_CLOCK_GAP_US
            row["note"] = (
                "A signal is Counted only when the on-screen hook and the controller "
                "agree on the frame clock. Otherwise it is flagged a mismatch, or it is not Counted."
            )
        self._write(row)

    def close_signals(self) -> bool:
        """Commit the terminal root. A failed write stays retryable."""
        with self._signal_lock:
            return self._close_signals_locked()

    def _close_signals_locked(self) -> bool:
        if self._signals_closed:
            return False
        count = self._chain_seq
        ok = self._write({
            "t_ms": 0,
            "kind": "terminal",
            "count": count,
            "entry_count": count,
            "root": self._chain_prev,
            "chain_root_hash": self._chain_prev,
            "first_seq": 0 if count else None,
            "last_seq": (count - 1) if count else None,
            "truncated": self._log_fault is True,
            "complete": False,
            "anchored": False,
            "session_id": self.session_id,
            "note": "Local hash links are not an anchor and are not completeness.",
        })
        if ok:
            self._signals_closed = True
        return ok

    def _write(self, row: dict) -> bool:
        # Serialized with close and with durable-length recovery (which runs
        # inside this method). After close, every further write is rejected:
        # nothing may be appended after the terminal row.
        with self._signal_lock:
            if self._signals_closed:
                self.stats["signal_log_failures"] = self.stats.get("signal_log_failures", 0) + 1
                return False
            return self._write_locked(row)

    def _write_locked(self, row: dict) -> bool:
        if self.log_path is None:
            return False
        body = {k: v for k, v in row.items() if k not in ("prev", "hash")}
        no_source = bool(body.pop("_no_source", False))
        body.setdefault("schema", SIGNAL_SCHEMA)
        prev = self._chain_prev
        seq = self._chain_seq
        body["chain_seq"] = seq
        if no_source:
            body["mono_us"] = None
            body["capture_id"] = None
            body["capture_mono_us"] = None
            body["frame_index"] = None
            body["source_frame"] = None
            body["anchored"] = False
            body["clock"] = "unbound"
        else:
            source = self._source_row()
            body.setdefault("frame_index", source["seq"])
            body.setdefault("anchored", False)
            body.setdefault("source_frame", source)
            for key in ("session_id", "epoch", "mono_origin_ns", "capture_generation"):
                if key in source and key not in body:
                    body[key] = source[key]
            if "mono_us" not in body:
                src = self._source if isinstance(self._source, dict) else {}
                screen_us = self._screen_us()
                if screen_us is not None:
                    body["mono_us"] = screen_us
                    body["clock"] = src.get("clock")
                    if isinstance(src.get("clock_id"), str):
                        body["clock_id"] = src["clock_id"]
                    if isinstance(src.get("capture_id"), str):
                        body["capture_id"] = src["capture_id"]
                    if isinstance(src.get("mono_origin_ns"), int) and not isinstance(src.get("mono_origin_ns"), bool):
                        body["mono_origin_ns"] = src["mono_origin_ns"]
                elif src.get("clock") == "wall":
                    body["mono_us"] = None
                    body["clock"] = "wall"
                    wall = src.get("wall_ms")
                    if isinstance(wall, int) and not isinstance(wall, bool):
                        body["wall_ms"] = wall
                else:
                    # No producer binding. Do not subtract a wall anchor into mono_us.
                    body["mono_us"] = None
                    body["clock"] = "unbound"
        try:
            digest = hashlib.sha256(_canon({"prev": prev, "seq": seq, "row": body}).encode("utf-8")).hexdigest()
        except TypeError as e:
            self._log_fault = True
            self.stats["signal_log_failures"] = self.stats.get("signal_log_failures", 0) + 1
            print(f"Call of Duty signal log skipped a non-integer row: {e}", file=sys.stderr)
            return False
        stored = dict(body)
        stored["prev"] = prev
        stored["hash"] = digest
        terminal = body.get("kind") == "terminal"
        if self._durable_len is not None and not self._restore_durable_len():
            if not terminal:
                self._log_fault = True
            self.stats["signal_log_failures"] = self.stats.get("signal_log_failures", 0) + 1
            print("Call of Duty signal log cannot restore its durable length", file=sys.stderr)
            return False
        try:
            fh = open(self.log_path, "ab")
        except OSError as e:
            if not terminal:
                self._log_fault = True
            self.stats["signal_log_failures"] = self.stats.get("signal_log_failures", 0) + 1
            print(f"Call of Duty signal log failed: {type(e).__name__}: {e}", file=sys.stderr)
            return False
        start = None
        try:
            with fh:
                start = os.fstat(fh.fileno()).st_size
                fh.write((json.dumps(stored) + "\n").encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            # The line may be in the file without being durable. Cut back to
            # the length before it now, or before the next write, so a retry
            # at this seq leaves one row.
            if start is not None:
                self._durable_len = start
                self._restore_durable_len()
            if not terminal:
                self._log_fault = True
            self.stats["signal_log_failures"] = self.stats.get("signal_log_failures", 0) + 1
            print(f"Call of Duty signal log failed: {type(e).__name__}: {e}", file=sys.stderr)
            return False
        self._chain_prev = digest
        self._chain_seq += 1
        return True

    def _restore_durable_len(self) -> bool:
        length = self._durable_len
        if length is None:
            return True
        try:
            if not os.path.exists(self.log_path):
                if length != 0:
                    return False
                self._durable_len = None
                return True
            with open(self.log_path, "r+b") as fh:
                size = fh.seek(0, os.SEEK_END)
                if size < length:
                    return False
                if size != length:
                    fh.truncate(length)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            return False
        self._durable_len = None
        return True


def _nearest_haptic(rows, target_us):
    target = _as_int(target_us)
    best, best_d = None, None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("type") not in (None, "haptic"):
            continue
        if row.get("type") not in (None, "haptic") and "t_us" not in row:
            continue
        t = _as_int(row.get("t_us"))
        if t is None or target is None:
            continue
        d = abs(t - target)
        if best_d is None or d < best_d:
            best, best_d = row, d
    return best


def load_signal_files(bundle: Path) -> dict:
    """Read the clocks the recorder already writes. Missing files stay empty.

    The tail is capped so a long ``inputs.jsonl`` cannot stall the frame loop.
    A short read fails closed: nothing new is Counted from bytes we did not finish.
    """
    inputs = _tail_rows(bundle / "inputs.jsonl")
    header = _first_header(bundle / "inputs.jsonl")
    if header is not None and not any(isinstance(r, dict) and r.get("type") == "header" for r in inputs):
        inputs = [header, *inputs]
    anchor = None
    for row in inputs:
        if isinstance(row, dict) and row.get("type") == "header":
            anchor = _as_int(row.get("wall_anchor_ms"))
            break
    stamps = []
    for row in _tail_rows(bundle / "frame_clock.jsonl"):
        if isinstance(row, dict) and row.get("type") == "frame":
            t = _as_int(row.get("t_mono_us"))
            if t is not None:
                stamps.append(t)
    haptics = [r for r in _tail_rows(bundle / "haptics.jsonl")
               if isinstance(r, dict) and r.get("type") == "haptic"]
    return {"inputs": inputs, "frame_stamps": stamps, "haptics": haptics, "wall_anchor_ms": anchor}


def _first_header(path: Path) -> dict | None:
    """The input header is at the start of the file. A tail read can miss it.

    Missing or unreadable means the wall anchor is unknown and nothing is Counted.
    """
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            raw = fh.readline(8192)
    except OSError:
        return None
    try:
        row = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeError):
        return None
    if isinstance(row, dict) and row.get("type") == "header":
        return row
    return None


def _tail_rows(path: Path, max_bytes: int = 262144) -> list:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial line the seek landed in
            raw = fh.read(max_bytes)
    except OSError:
        return []
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def clock_sync_status(frame_stamps, inputs, wall_anchor_ms) -> str:
    """Controller times against frame-clock stamps.

    Agreed when the wall anchor is known and some input sits within 500 ms of
    a stamp. Not Counted when the frame clock or the anchor is absent. Both
    present, and none of the inputs near a stamp, is flagged a mismatch.
    This is not a verification verdict.
    """
    if not frame_stamps or _as_int(wall_anchor_ms) is None:
        return NOT_COUNTED
    times = []
    for row in inputs or []:
        if isinstance(row, dict) and row.get("type") == "in":
            t = _as_int(row.get("t_us"))
            if t is not None:
                times.append(t)
    if not times:
        return NOT_COUNTED
    stamps = [_as_int(s) for s in frame_stamps]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return NOT_COUNTED
    for t in times:
        for s in stamps:
            if abs(t - s) <= FRAME_CLOCK_GAP_US:
                return AGREED
    return MISMATCH


def owner_row_from_signals(bundle: Path) -> dict | None:
    """The owner row on a posted end screen, from ``cod_signals.jsonl``.

    Missing or unreadable stays ``None``. This does not read another player's row.
    """
    path = Path(bundle) / "cod_signals.jsonl"
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("kind") != "end_screen":
            continue
        owner = row.get("owner_row")
        if isinstance(owner, dict) and same_owner_tag(str(owner.get("tag") or "")):
            found = owner
    return found


def _public_owner_row(owner: dict) -> dict:
    """The fields that sit beside the match. No other name is copied."""
    kills, deaths = _score(owner.get("kills")), _score(owner.get("deaths"))
    unknown = deaths is None and owner.get("deaths_status") == "UNKNOWN_NO_COLUMN"
    if kills is None or (deaths is None and not unknown):
        return {}
    assists = _score(owner.get("assists"))
    confirms = _score(owner.get("confirms"))
    result = owner.get("result") if owner.get("result") in ("victory", "defeat") else None
    out = {
        "tag": OWNER_TAG,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "confirms": confirms,
        "result": result,
        "note": (
            "Read from this player's end scoreboard row. "
            "Not a home or away score. Not a verification verdict."
        ),
    }
    status = owner.get("deaths_status")
    if status in ("personal_card", "UNKNOWN_NO_COLUMN"):
        # Only rows without a DEATHS column carry these keys, so column boards keep the old shape.
        out["deaths_status"] = status
        match = owner.get("card_name_match")
        out["card_name_match"] = match if isinstance(match, bool) else None
        if unknown:
            out["note"] += " Deaths are UNKNOWN: no DEATHS column and no owner card deaths. Not zero."
    return out


def session_document(*, frame_stamps=None, inputs=None, wall_anchor_ms=None, owner_row=None) -> dict:
    """Power and controller path beside the match. Not a receipt field.

    ``controller_path`` is ``usb`` only when the input log header says
    ``usb-hid`` and the log shows the pad was read on that path: at least one
    ``in`` report row or a ``connected`` status. A header alone is the path
    QorCap looked on, not a path it read, so it stays ``unknown``. Power stays
    ``unknown`` until a held-out report map says battery versus plugged. No
    timing band is applied.
    """
    header = None
    read = False
    for row in inputs or []:
        if not isinstance(row, dict):
            continue
        if header is None and row.get("type") == "header":
            header = row
        elif row.get("type") == "in" or (row.get("type") == "status" and row.get("state") == "connected"):
            read = True
    usb = isinstance(header, dict) and header.get("transport") == "usb-hid"
    path = "usb" if usb and read else "unknown"
    stamps = list(frame_stamps or [])
    doc = {
        "schema": "qorcap-cod-session/v0",
        "controller_path": path,
        "power": "unknown",
        "frame_clock": "present" if stamps else "absent",
        "clock_sync": clock_sync_status(stamps, inputs or [], wall_anchor_ms),
        "counted": COUNTED_SIGNALS,
        "grip": "off",
        "info_only": INFO_ONLY,
        "note": (
            "Session tags sit beside the match. Clock sync uses the frame clock's 500 ms window. "
            "They are not a receipt field and not a verification verdict."
        ),
    }
    if isinstance(owner_row, dict):
        public = _public_owner_row(owner_row)
        if public:
            doc["owner_row"] = public
    return doc


def attach_session(bundle: Path) -> dict:
    """Write ``cod_session.json`` and copy sync tags onto ``bundle.json``.

    Receipt files are not edited. ``bundle.json`` already allows extra fields.
    """
    doc = write_session(bundle)
    p = bundle / "bundle.json"
    if not p.is_file():
        return doc
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return doc
    if not isinstance(meta, dict):
        return doc
    meta["clock_sync"] = doc["clock_sync"]
    meta["controller_path"] = doc["controller_path"]
    meta["power"] = doc["power"]
    meta["game"] = GAME_ID
    meta["scorebug_profile"] = PROFILE_ID
    p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return doc


def _stat_count(stats: dict, key: str) -> int:
    raw = stats.get(key, 0) if isinstance(stats, dict) else 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _optional_ms(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _path_segment(text: str, at: int) -> tuple[str, int] | None:
    matched = _STATS_PATH_SEG_RE.match(text, at)
    if not matched:
        return None
    return matched.group(0), matched.end()


def _path_continuation_end(text: str, at: int) -> int | None:
    """Extend a path across spaces when the next piece still looks like a path.

    ``My Documents\\file.cfg`` stays one path. The word ``missing`` before another
    path does not, so two paths keep the words between them.
    """
    n = len(text)
    if at >= n or not text[at].isspace():
        return None
    k = at
    while k < n and text[k].isspace():
        k += 1
    if k >= n or _STATS_PATH_START_RE.match(text, k):
        return None
    first = _path_segment(text, k)
    if first is None:
        return None
    body, end = first
    if not _STATS_PATH_MARK_RE.search(body):
        p = end
        while p < n and text[p].isspace():
            p += 1
        if p >= n or _STATS_PATH_START_RE.match(text, p):
            return None
        nxt = _path_segment(text, p)
        if nxt is None or not _STATS_PATH_MARK_RE.search(nxt[0]):
            return None
        end = nxt[1]
    while True:
        p = end
        while p < n and text[p].isspace():
            p += 1
        if p >= n or _STATS_PATH_START_RE.match(text, p):
            break
        nxt = _path_segment(text, p)
        if nxt is None or not _STATS_PATH_MARK_RE.search(nxt[0]):
            break
        end = nxt[1]
    return end


def _scrub_unquoted_paths(text: str) -> str:
    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        matched = _STATS_PATH_START_RE.search(text, i)
        if not matched:
            parts.append(text[i:])
            break
        parts.append(text[i:matched.start()])
        j = matched.end()
        while j < n and text[j] not in " \t\r\n\"'":
            if text[j] in ",;:)]}>":
                break
            j += 1
        while True:
            nxt = _path_continuation_end(text, j)
            if nxt is None:
                break
            j = nxt
        parts.append("[path]")
        i = j
    return "".join(parts)


def scrub_reader_error(value) -> str | None:
    """Shorten a stored reader string and drop path-like and secret-like pieces.

    Empty becomes null. Bearer tokens, UNC paths, tilde paths, and paths that
    contain spaces are removed along with key=value secrets. This does not
    decide that the reader is healthy.
    """
    if not isinstance(value, str):
        return None
    text = value.replace("\r", " ").replace("\n", " ")
    # Paths first so a spaced tail after a secret value is not left behind.
    text = _STATS_QUOTED_PATH_RE.sub("[path]", text)
    text = _scrub_unquoted_paths(text)
    text = _STATS_SECRET_RE.sub(r"\1=[redacted]", text)
    text = _STATS_BEARER_RE.sub("Bearer [redacted]", text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > READER_STATS_ERROR_MAX:
        text = text[: READER_STATS_ERROR_MAX - 6].rstrip() + " [cut]"
    return text


def _blank_observation() -> dict:
    return {
        "stats": None,
        "reader_error": None,
        "cod_signal_error": None,
        "reader_tick": None,
        "end_word_first_ms": None,
        "end_word_last_ms": None,
        "owner_row_ever_read": None,
        "owner_row_good_frames": None,
        "owner_row_accepted": None,
        "published": False,
    }


def observation_from_runner(runner, *, reader_error=None, cod_signal_error=None, reader_tick=None) -> dict:
    """Read the runner after its thread has stopped. Counts come from ``runner.stats``."""
    stats = getattr(runner, "stats", None)
    if not isinstance(stats, dict):
        return _blank_observation()
    return {
        "stats": {key: _stat_count(stats, key) for key in READER_STATS_KEYS},
        "reader_error": reader_error if isinstance(reader_error, str) else None,
        "cod_signal_error": cod_signal_error if isinstance(cod_signal_error, str) else None,
        "reader_tick": _optional_ms(reader_tick),
        "end_word_first_ms": _optional_ms(getattr(runner, "end_word_first_ms", None)),
        "end_word_last_ms": _optional_ms(getattr(runner, "end_word_last_ms", None)),
        "owner_row_ever_read": bool(getattr(runner, "owner_row_ever_read", False)),
        "owner_row_good_frames": _stat_count({"n": getattr(runner, "owner_row_good_frames", 0)}, "n"),
        "owner_row_accepted": bool(getattr(runner, "owner_row_accepted", False)),
        "published": True,
    }


def reader_stats_document(observation: dict, *, reader_join: str) -> dict:
    """Counts at stop. No images. Not a receipt field.

    ``reader_join`` is ``joined`` when the reader thread had exited,
    ``timed_out`` when the join wait ended first, or ``not_joined`` when stop
    did not wait. A missing publication keeps counts null instead of writing
    zeros. ``reader_tick`` is wall Unix milliseconds. The end-word fields are
    capture monotonic milliseconds. Those clocks are not subtracted here.
    """
    obs = observation if isinstance(observation, dict) else _blank_observation()
    stats = obs.get("stats")
    if isinstance(stats, dict):
        counts = {key: _stat_count(stats, key) for key in READER_STATS_KEYS}
    else:
        counts = None
    owner = obs.get("owner_row_ever_read")
    if not isinstance(owner, bool):
        owner = None
    good = obs.get("owner_row_good_frames")
    if isinstance(good, bool) or not isinstance(good, int) or good < 0:
        good = None
    accepted = obs.get("owner_row_accepted")
    if not isinstance(accepted, bool):
        accepted = None
    if reader_join not in ("joined", "timed_out", "not_joined"):
        reader_join = "not_joined"
    return {
        "schema": READER_STATS_SCHEMA,
        "reader_join": reader_join,
        "stats": counts,
        "reader_error": scrub_reader_error(obs.get("reader_error")),
        "cod_signal_error": scrub_reader_error(obs.get("cod_signal_error")),
        "reader_tick": _optional_ms(obs.get("reader_tick")),
        "reader_tick_clock": READER_TICK_CLOCK,
        "end_word_first_ms": _optional_ms(obs.get("end_word_first_ms")),
        "end_word_last_ms": _optional_ms(obs.get("end_word_last_ms")),
        "end_word_clock": END_WORD_CLOCK,
        "owner_row_ever_read": owner,
        "owner_row_good_frames": good,
        "owner_row_accepted": accepted,
        "note": (
            "Reader counts from this sidecar when stop passes practice and the game is Call of Duty. "
            "This file has no switch that reads another installed build. Not a receipt field. No images. "
            "reader_tick is wall Unix milliseconds. end_word_first_ms and end_word_last_ms are capture "
            "monotonic milliseconds. Do not subtract those clocks. A timed-out join keeps the last "
            "published copy and is not a finished reader. owner_row_ever_read: at least one frame "
            "passed the per-frame owner-row read; it does not mean accepted. owner_row_good_frames: "
            "frames that passed it. owner_row_accepted: the row met the 3-frame agreement and was "
            "committed with the final."
        ),
    }


def write_reader_stats(bundle: Path, observation: dict, *, reader_join: str) -> dict:
    """Write ``cod_reader_stats.json``. Does not append ``cod_signals.jsonl`` or edit a receipt."""
    doc = reader_stats_document(observation, reader_join=reader_join)
    (Path(bundle) / "cod_reader_stats.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def write_session(bundle: Path) -> dict:
    loaded = load_signal_files(bundle)
    doc = session_document(
        frame_stamps=loaded["frame_stamps"], inputs=loaded["inputs"],
        wall_anchor_ms=loaded["wall_anchor_ms"],
        owner_row=owner_row_from_signals(bundle))
    (bundle / "cod_session.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def probe_end_frames(frames, own_kills=None) -> dict:
    """Read the last ``END_PROBE_FRAMES`` end frames. Does not scan a longer window.

    ``frames`` is ``(t_ms, image)`` from the end of the match, oldest first.
    A final is posted only when the same end word and the same owner row agree
    on 3 strictly later timestamps across 3 seconds inside those frames.
    Five frames closer together than that do not post.
    """
    tail = list(frames or [])[-END_PROBE_FRAMES:]
    posted: list = []
    runner = CodSetRunner(posted.append)
    summaries = []
    times = []
    for item in tail:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        t_ms, img = item[0], item[1]
        source = item[2] if len(item) > 2 and isinstance(item[2], dict) else None
        if isinstance(t_ms, bool) or not isinstance(t_ms, int):
            continue
        times.append(t_ms)
        runner.on_frame(t_ms, img, source=source)
        summaries.append(_summarize_end_frame(img))
    span = (times[-1] - times[0]) if len(times) >= 2 else 0
    final = posted[-1] if posted else None
    if final is not None:
        reason = "the end word and the owner row agreed across the 3-frame bar"
    elif not any(row.get("result") for row in summaries):
        reason = "the end word was not read on the last frames"
    elif not any(row.get("owner_row") for row in summaries):
        reason = "the owner row was not read on the last frames"
    elif span >= END_AGREE_SPAN_MS and len(times) >= END_AGREE_FRAMES:
        reason = "the last frames are not five distinct captures"
    else:
        reason = "the last frames do not meet 3 timestamps across 3 seconds"
    return {
        "schema": "qorcap-cod-end-probe/v0",
        "frames": len(summaries),
        "span_ms": span,
        "posted": final is not None,
        "reason": reason,
        "reads": summaries,
        "note": (
            "Last 5 end frames only. Not a long end-window scan. "
            "Not a home or away score. Not a verification verdict."
        ),
    }


def _summarize_end_frame(img) -> dict:
    """The end word and the owner row on one frame. Other names are not copied."""
    from qorcap_cod_hud import read_owner_row
    end = CodEndScreenReader().read(img)
    result = end.reason if end.ok and end.reason in ("victory", "defeat") else None
    owner = read_owner_row(img)
    row = None
    if owner.ok and owner.calibrated and same_owner_tag(owner.player_tag or ""):
        from qorcap_cod_hud import owner_deaths_ok, owner_deaths_status
        kills, deaths = _score(owner.kills), _score(owner.deaths)
        if kills is not None and (deaths is not None or owner_deaths_ok(owner)):
            row = {
                "tag": OWNER_TAG,
                "kills": kills,
                "deaths": deaths,
                "assists": _score(owner.assists),
                "confirms": _score(owner.confirms),
            }
            if owner_deaths_status(owner) != "column":
                row["deaths_status"] = owner_deaths_status(owner)
    return {"result": result, "owner_row": row}


def record_end_probe(bundle: Path, frames, own_kills=None) -> dict:
    """Write ``cod_end_probe.json``. Does not edit a receipt."""
    doc = probe_end_frames(frames, own_kills=own_kills)
    (Path(bundle) / "cod_end_probe.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def cod_core_factory(post, log_path: Path | None = None) -> CodSetRunner:
    return CodSetRunner(post, log_path=log_path)
