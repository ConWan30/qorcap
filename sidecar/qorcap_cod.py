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
- Stick turn against camera turn, and rumble against an on-screen hit, are
  INFO-ONLY. They are not Counted.
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

import json
import sys
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

# Optional later counts. They stay INFO-ONLY until a held-out match promotes them.
# Power is unknown: this set does not read battery versus plugged.
INFO_ONLY = {
    "stick_turn_vs_camera": {
        "label": "INFO-ONLY",
        "status": NOT_COUNTED,
        "note": "Stick turn against camera turn. Not Counted until a held-out match promotes it.",
    },
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
    3 strictly later timestamps across 3 seconds before it posts, and it posts
    only when home and away are both read and the result is the same on each
    of those frames. Once those scores exist, confirm also requires the word
    to match the side chosen at the start. A gap, a non-end frame, or a
    different result or score starts that count over. ``confirm`` is the only
    way the receipt field ``final_confirmed`` becomes true. That field is the
    end-screen agreement. It is not a verification verdict.
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
    return {"region": rd.region, "status": rd.status, "reason": rd.reason, "calibrated": bool(rd.calibrated)}


class CodSetRunner:
    """Frame loop for the Call of Duty set.

    HUD hooks are logged and are not posted to ``/events``. The only event
    this runner posts is a final, after a calibrated end screen agrees for
    3 strictly later timestamps across 3 seconds, with the same result and the
    same home and away. A frame that is not that end screen, a missing score
    pair, a repeated or earlier timestamp, or a gap longer than 3 seconds
    starts the count over. Scoreboard kills and assists are not home and away
    and do not post a final. In-match signals stay on ``cod_signals.jsonl``.
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
        self.stats = {
            "frames": 0, "agreed": 0, "errors": 0,
            "hud_not_read": 0, "end_screen_not_read": 0, "posted_final": 0,
            "not_counted": 0, "flagged_mismatch": 0,
            "end_direction_mismatch": 0,
        }
        self._logged = False
        self._pair = None
        self._n = 0
        self._t0 = None
        self._last_t = None
        self.last_hitmarker = NOT_COUNTED
        self.last_hitmarker_us = None

    def set_signals(self, inputs, frame_stamps, haptics, wall_anchor_ms) -> None:
        """Replace the clocks used on the next frame. Empty lists count nothing."""
        self.inputs = list(inputs or [])
        self.frame_stamps = list(frame_stamps or [])
        self.haptics = list(haptics or [])
        self.wall_anchor_ms = wall_anchor_ms

    def on_frame(self, t_ms: int, img) -> None:
        self.stats["frames"] += 1
        try:
            reads = list(self.hud.read(img))
        except Exception as e:
            self.stats["errors"] += 1
            print(f"Call of Duty HUD hook error: {type(e).__name__}: {e}", file=sys.stderr)
            reads = []
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
        if not self._logged:
            self._logged = True
            self._write({
                "t_ms": int(t_ms), "kind": "set",
                "regions": [_public(rd) for rd in reads],
                "signals": signals,
                "frame_clock_gap_us": FRAME_CLOCK_GAP_US,
                "note": (
                    "A signal is Counted only when the on-screen hook and the controller "
                    "agree on the frame clock. Otherwise it is flagged a mismatch, or it is not Counted."
                ),
            })
        try:
            end = self.end.read(img)
        except Exception as e:
            self.stats["errors"] += 1
            print(f"Call of Duty end-screen hook error: {type(e).__name__}: {e}", file=sys.stderr)
            self._clear_end_candidate()
            return
        if self.final is not None:
            return
        # Home and away come only from this end-screen read. A scoreboard
        # kill, assist, or team total is not that pair and is not a final.
        if not (end.ok and end.calibrated):
            self.stats["end_screen_not_read"] += 1
            self._clear_end_candidate()
            return
        home, away = _score(end.home), _score(end.away)
        if home is None or away is None or not isinstance(end.reason, str) or not end.reason:
            self.stats["end_screen_not_read"] += 1
            self._clear_end_candidate()
            return
        when = t_ms if isinstance(t_ms, int) and not isinstance(t_ms, bool) else None
        if when is None:
            self._clear_end_candidate()
            return
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
            return
        confirmed = confirm
        notes = [
            "Call of Duty end screen agreed. This is the end of the match.",
            "The receipt quarter is 4 because this end screen has no game clock. It is not a Madden quarter.",
        ]
        if direction_note:
            notes.append(direction_note)
        if not confirmed:
            notes.append("The end-screen score is not Counted as confirmed.")
        event = {
            "type": "final",
            "basis": "end_menu",
            "method": "end_menu_read",
            "home": home,
            "away": away,
            "quarter": 4,
            "t_ms": agreed["t_ms"],
            "final_confirmed": confirmed,
            "notes": notes,
            "game_set": "cod",
            "end_screen": {
                "frames": agreed["frames"],
                "region": END_NAME,
                "until_ms": agreed["until_ms"],
            },
        }
        self.final = event
        self.stats["posted_final"] += 1
        self._write({"t_ms": int(t_ms), "kind": "end_screen", "status": AGREED,
                     "final_confirmed": confirmed, "home": home, "away": away,
                     "frames": agreed["frames"]})
        self.post(event)

    def _signals(self, t_ms: int, by_name: dict) -> list[dict]:
        screen_us = screen_mono_us(t_ms, self.wall_anchor_ms)
        stamps = self.frame_stamps
        edges = trigger_edges(self.inputs)
        trigger = nearest_us(edges, screen_us)
        hit = by_name.get("hitmarker")
        hit_status = correlate_pair(
            calibrated=bool(hit and hit.ok and hit.calibrated),
            screen_us=screen_us, input_us=trigger, frame_stamps=stamps)
        self.last_hitmarker = hit_status
        self.last_hitmarker_us = screen_us if hit_status == AGREED else None
        popup = by_name.get("kill_popup")
        popup_status = follows_status(
            calibrated=bool(popup and popup.ok and popup.calibrated),
            prior_status=hit_status, this_us=screen_us, prior_us=self.last_hitmarker_us)
        popup_us = screen_us if popup_status == AGREED else None
        feed = by_name.get("killfeed")
        feed_status = follows_status(
            calibrated=bool(feed and feed.ok and feed.calibrated),
            prior_status=popup_status, this_us=screen_us, prior_us=popup_us)
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
        motion = _nearest_haptic(self.haptics, trigger if trigger is not None else screen_us)
        motion_status = recoil_status(motion, trigger, stamps)
        return [
            {"signal": "hitmarker", "status": hit_status},
            {"signal": "kill_popup", "status": popup_status},
            {"signal": "killfeed", "status": feed_status},
            {"signal": "pause", "status": pause_status},
            {"signal": "recoil", "status": motion_status},
            {"signal": "scoreboard", "status": board_status},
            {"signal": "end_screen", "status": NOT_COUNTED},
        ]

    def _clear_end_candidate(self) -> None:
        self._pair = None
        self._n = 0
        self._t0 = None
        self._last_t = None

    def _begin_end_candidate(self, t_ms: int, key: tuple) -> None:
        self._pair = key
        self._n = 1
        self._t0 = t_ms
        self._last_t = t_ms

    def _offer(self, t_ms: int, reason: str, confirm: bool, home: int, away: int) -> dict | None:
        """One more end-screen sample. None until the bar is met.

        The sample counts only when its timestamp is strictly later than the
        last accepted one, the gap is within the cap, and the result and the
        score pair are unchanged. Anything else starts a new candidate at this
        timestamp and does not post.
        """
        key = (reason, bool(confirm), home, away)
        if key != self._pair or not isinstance(self._last_t, int):
            self._begin_end_candidate(t_ms, key)
            return None
        if t_ms <= self._last_t or t_ms - self._last_t > END_AGREE_GAP_MS:
            self._begin_end_candidate(t_ms, key)
            return None
        self._n += 1
        self._last_t = t_ms
        t0 = self._t0 if isinstance(self._t0, int) else t_ms
        if self._n >= END_AGREE_FRAMES and t_ms - t0 >= END_AGREE_SPAN_MS:
            return {"home": home, "away": away, "frames": self._n, "t_ms": t0, "until_ms": t_ms}
        return None

    def _write(self, row: dict) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as e:
            print(f"Call of Duty signal log failed: {type(e).__name__}: {e}", file=sys.stderr)


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


def session_document(*, frame_stamps=None, inputs=None, wall_anchor_ms=None) -> dict:
    """Power and controller path beside the match. Not a receipt field.

    ``controller_path`` is ``usb`` only when the input log header already says
    ``usb-hid`` (the path QorCap read). Power stays ``unknown`` until a held-out
    report map says battery versus plugged. No timing band is applied.
    """
    header = None
    for row in inputs or []:
        if isinstance(row, dict) and row.get("type") == "header":
            header = row
            break
    path = "usb" if isinstance(header, dict) and header.get("transport") == "usb-hid" else "unknown"
    stamps = list(frame_stamps or [])
    return {
        "schema": "qorcap-cod-session/v0",
        "controller_path": path,
        "power": "unknown",
        "frame_clock": "present" if stamps else "absent",
        "clock_sync": clock_sync_status(stamps, inputs or [], wall_anchor_ms),
        "info_only": INFO_ONLY,
        "note": (
            "Session tags sit beside the match. Clock sync uses the frame clock's 500 ms window. "
            "They are not a receipt field and not a verification verdict."
        ),
    }


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


def write_session(bundle: Path) -> dict:
    loaded = load_signal_files(bundle)
    doc = session_document(
        frame_stamps=loaded["frame_stamps"], inputs=loaded["inputs"],
        wall_anchor_ms=loaded["wall_anchor_ms"])
    (bundle / "cod_session.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def cod_core_factory(post, log_path: Path | None = None) -> CodSetRunner:
    return CodSetRunner(post, log_path=log_path)
