"""Branded Start Match console. Presentation only.

The capture agent, the HID reader, the referee protocol, and the receipt are untouched.
This module paints the interactive console. It never writes a controller report, never
writes a receipt, and never decides a referee result.

Gold is drawn only when a caller sets ``referee_verified`` on a card. The live match
path never sets it. Local button observation is labeled as a pad read. The words
attested, attestation, and hardware-verified are not printed. A device row is not
shown; if one is added later its strongest wording is tpm_claimed.

Plain output (the historical one-line status) is used when ``--plain`` is set,
``QORCAP_PLAIN`` is 1/true/yes/on, stdout is not a terminal, ``TERM`` is dumb, or
Windows virtual-terminal mode cannot be enabled. ``NO_COLOR`` keeps the layout and
drops color. ``QORCAP_ASCII=1`` uses ``+`` frames because classic conhost fonts
always have those. ``QORCAP_REDUCE_MOTION=1`` holds the pulse still.

Try it on Windows, from the unzipped QorCap folder, before a match:

    py sidecar\\qorcap_console.py

That paints sample frames in the open console and records nothing.
``Start Match.cmd --plain`` or ``set QORCAP_PLAIN=1`` keeps the old status line.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field, replace

# Condensed 4x5 letters, in the spirit of the sites' display face (a terminal has no
# Big Shoulders). Every glyph is exactly 4 columns.
_FONT: dict[str, tuple[str, str, str, str, str]] = {
    "A": (" ## ", "#  #", "####", "#  #", "#  #"),
    "B": ("### ", "#  #", "### ", "#  #", "### "),
    "C": (" ###", "#   ", "#   ", "#   ", " ###"),
    "D": ("### ", "#  #", "#  #", "#  #", "### "),
    "E": ("####", "#   ", "### ", "#   ", "####"),
    "F": ("####", "#   ", "### ", "#   ", "#   "),
    "G": (" ###", "#   ", "# ##", "#  #", " ###"),
    "H": ("#  #", "#  #", "####", "#  #", "#  #"),
    "I": ("### ", " #  ", " #  ", " #  ", "### "),
    "J": (" ###", "   #", "   #", "#  #", " ## "),
    "K": ("#  #", "# # ", "##  ", "# # ", "#  #"),
    "L": ("#   ", "#   ", "#   ", "#   ", "####"),
    "M": ("#  #", "## #", "# ##", "#  #", "#  #"),
    "N": ("#  #", "## #", "# ##", "#  #", "#  #"),
    "O": (" ## ", "#  #", "#  #", "#  #", " ## "),
    "P": ("### ", "#  #", "### ", "#   ", "#   "),
    "Q": (" ## ", "#  #", "#  #", "# ##", " ## "),
    "R": ("### ", "#  #", "### ", "# # ", "#  #"),
    "S": (" ###", "#   ", " ## ", "   #", "### "),
    "T": ("####", " #  ", " #  ", " #  ", " #  "),
    "U": ("#  #", "#  #", "#  #", "#  #", " ## "),
    "V": ("#  #", "#  #", "#  #", "#  #", " ## "),
    "W": ("#  #", "#  #", "# ##", "## #", "#  #"),
    "X": ("#  #", " ## ", " ## ", "#  #", "#  #"),
    "Y": ("#  #", "#  #", " ## ", " #  ", " #  "),
    "Z": ("####", "  # ", " #  ", "#   ", "####"),
    "0": (" ## ", "#  #", "# ##", "## #", " ## "),
    "1": (" #  ", "##  ", " #  ", " #  ", "### "),
    "2": ("### ", "   #", " ## ", "#   ", "####"),
    "3": ("### ", "   #", " ## ", "   #", "### "),
    "4": ("#  #", "#  #", "####", "   #", "   #"),
    "5": ("####", "#   ", "### ", "   #", "### "),
    "6": (" ## ", "#   ", "### ", "#  #", " ## "),
    "7": ("####", "   #", "  # ", " #  ", "#   "),
    "8": (" ## ", "#  #", " ## ", "#  #", " ## "),
    "9": (" ## ", "#  #", " ###", "   #", " ## "),
    ":": ("    ", " ## ", "    ", " ## ", "    "),
    " ": ("    ", "    ", "    ", "    ", "    "),
}

_STATE = {
    "capturing": "REC",
    "waiting_for_device": "NO SIGNAL",
    "reopening": "reconnecting",
    "disk_full": "DISK FULL",
}

_HID = {
    "connected": "connected",
    "searching": "searching",
    "missing": "missing",
    "unplugged": "unplugged",
    "stalled": "stalled",
    "off": "off",
}

# Face colors match the pad, not a verdict. Gold is not a button color.
_BUTTON_COLOR = {
    "CROSS": "cyan",
    "CIRCLE": "red",
    "SQUARE": "pink",
    "TRIANGLE": "ok",
    "OPTIONS": "cyan",
    "CREATE": "ink",
    "UP": "ink",
    "DOWN": "ink",
    "LEFT": "ink",
    "RIGHT": "ink",
}

_SPINNER = "|/-\\"
_HOLD_S = 8.0
# The pattern is pressed in the order on the card. CIRCLE closes Madden's pause
# menu, so the line says to stay there until the pattern is finished. Shown only
# when CIRCLE or CROSS is in the pattern and one row fits. Pattern order is unchanged.
PAUSE_MENU_HINT = "In the order shown. CIRCLE closes menu; stay until done."
_BANNED = ("attested", "attestation", "hardware-verified", "hardware verified")

_TRUE = {
    "bg": (4, 5, 10),
    "ink": (238, 243, 255),
    "mute": (151, 160, 186),
    "dim": (93, 103, 136),
    "cyan": (46, 242, 255),
    "cyan2": (15, 184, 216),
    "pink": (255, 46, 136),
    "red": (255, 77, 94),
    "gold": (255, 194, 61),
    "ok": (61, 255, 168),
    "line": (188, 198, 220),
    "hot": (255, 255, 255),
}
_C256 = {
    "bg": "16", "ink": "231", "mute": "146", "dim": "60", "cyan": "51", "cyan2": "45",
    "pink": "198", "red": "203", "gold": "220", "ok": "48", "line": "250", "hot": "231",
}
_C16 = {
    "bg": "30", "ink": "97", "mute": "37", "dim": "90", "cyan": "96", "cyan2": "36",
    "pink": "95", "red": "91", "gold": "93", "ok": "92", "line": "37", "hot": "97",
}

Cell = tuple[str, str]
Grid = list[list[Cell]]

_SAY_HOOK = None


def set_say_hook(fn) -> None:
    global _SAY_HOOK
    _SAY_HOOK = fn


def emit(msg: str = "") -> None:
    """Print to the interactive console. A live dashboard may hold the line and reprint it later."""
    hook = _SAY_HOOK
    if hook is not None:
        try:
            if hook(msg):
                return
        except Exception:
            pass
    try:
        msg = tint_verified_banner(msg)
    except Exception:
        pass
    print(msg, flush=True)


def tint_verified_banner(msg: str, env: dict | None = None, stream=None) -> str:
    """Gold on a referee VERIFIED banner only. Other results stay uncolored."""
    env = os.environ if env is None else env
    stream = sys.stdout if stream is None else stream
    if _color_level(env) == "none":
        return msg
    if not _isatty(stream):
        return msg
    stripped = msg.strip()
    if stripped.startswith("RESULT: VERIFIED") or stripped.startswith("OFFICIAL RESULT: VERIFIED"):
        return paint_inline(msg, "gold", _color_level(env))
    return msg


def paint_inline(msg: str, name: str, level: str) -> str:
    pal = _Palette(level)
    return pal.code(name) + msg + pal.reset()


def big(text: str) -> list[str]:
    glyphs = []
    for ch in text.upper():
        glyphs.append(_FONT.get(ch, _FONT[" "]))
    if not glyphs:
        return [""] * 5
    return [" ".join(g[i] for g in glyphs) for i in range(5)]


def emphasize(text: str, width: int) -> list[str]:
    lines = big(text)
    doubled = ["".join(ch * 2 for ch in line) for line in lines]
    if doubled and len(doubled[0]) <= max(0, width - 4):
        return doubled
    return lines


def _box(a: str, b: str, c: str) -> list[str]:
    def inner(s: str) -> str:
        if len(s) != 7:
            raise RuntimeError(f"glyph cell {s!r} is {len(s)}")
        return f"|{s}|"

    return ["+-------+", inner(a), inner(b), inner(c), "+-------+"]


def _named(name: str) -> list[str]:
    return _box("       ", name[:7].center(7), "       ")


def button_glyph(name: str) -> list[str]:
    """Large pad glyph. Interiors are ASCII so conhost's default font can draw them."""
    art = {
        "CROSS": _box(" \\   / ", "   X   ", " /   \\ "),
        "CIRCLE": _box("  .-.  ", " (   ) ", "  '-'  "),
        "SQUARE": _box(" .---. ", " |   | ", " '---' "),
        "TRIANGLE": _box("  /\\   ", " /  \\  ", " /____\\"),
        "UP": _box("   ^   ", "  / \\  ", "   |   "),
        "DOWN": _box("   |   ", "  \\ /  ", "   v   "),
        "LEFT": _box("   <   ", "  <--  ", "   <   "),
        "RIGHT": _box("   >   ", "  -->  ", "   >   "),
    }
    if name in art:
        rows = art[name]
    else:
        rows = _named(name)
    for row in rows:
        if len(row) != 9:
            raise RuntimeError(f"glyph {name} row {row!r} is {len(row)}")
    return rows


@dataclass
class ChallengeCard:
    phase: str
    label: str
    pattern: list[str]
    issued_ms: int
    deadline_ms: int
    matched: int = 0
    presses: list[str] = field(default_factory=list)
    prev: list[str] = field(default_factory=list)
    result: str | None = None
    note: str = ""
    referee_verified: bool = False
    settled_at: float | None = None
    unhealthy: bool = False
    saw_trusted: bool = False
    # Per-press feedback (0.3.37). ``last`` is the newest press, ``wrong`` the button that broke the current try.
    last: str = ""
    last_ok: bool = False
    wrong: str = ""
    rolled: bool = False
    tries_broken: int = 0
    edge_seq: int | None = None
    edges: bool = False
    _last_t_ms: int | None = None
    _last_dpad: str = ""

    def text(self) -> str:
        parts = [self.label, self.result or "pending", self.note, " ".join(self.pattern)]
        return " ".join(parts)


# The buttons the referee reads from inputs.jsonl (qor_receipt.review.bundle.BUTTON_BITS). Edge-only names such
# as MUTE or the back paddles are not presses to the referee, so they do not break a try on this card either.
REFEREE_BUTTONS = frozenset({
    "CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "L1", "R1", "L2", "R2", "CREATE", "OPTIONS", "L3", "R3",
    "UP", "DOWN", "LEFT", "RIGHT", "PS", "TOUCHPAD",
})
_DPAD = frozenset({"UP", "DOWN", "LEFT", "RIGHT"})
# Two d-pad edges this close together are one rolled thumb, not two deliberate presses.
_ROLL_MS = 60
_PAUSE = "OPTIONS"


def note_press(card: ChallengeCard, button: str, t_ms: int | None = None) -> None:
    """One rising edge, judged the way the referee judges the window.

    The referee answer is the first OPTIONS press followed by exactly the pattern as the very next presses
    (``qor_receipt.challenge.response_start``). Any other press in between breaks that try. The pattern body
    never contains OPTIONS, so a wrong press resets the count to 0, or to 1 when it is OPTIONS itself. Once the
    pattern is complete the referee has its answer and later presses cannot change it, so the card stops
    counting. This only mirrors the rule for display; nothing here is recorded or sent.
    """
    b = str(button).upper()
    if card.result or b not in REFEREE_BUTTONS:
        return
    if t_ms is not None and not (int(card.issued_ms) <= int(t_ms) <= int(card.deadline_ms)):
        return
    if card.matched >= len(card.pattern) > 0:
        return
    rolled = (b in _DPAD and card._last_dpad and card._last_dpad != b and t_ms is not None
              and card._last_t_ms is not None and int(t_ms) - card._last_t_ms <= _ROLL_MS)
    card.presses.append(b)
    card.last = b
    if card.matched < len(card.pattern) and b == card.pattern[card.matched]:
        card.matched += 1
        card.last_ok = True
        card.wrong = ""
        card.rolled = False
    else:
        if card.matched > 0:
            card.tries_broken += 1
            card.wrong = b
            card.rolled = bool(rolled)
        card.last_ok = False
        card.matched = 1 if (b == _PAUSE and card.pattern and card.pattern[0] == _PAUSE) else 0
        if card.matched:
            card.last_ok = True
    card._last_t_ms = int(t_ms) if t_ms is not None else None
    card._last_dpad = b if b in _DPAD else ""


def note_held(card: ChallengeCard, held: list[str], *, trusted: bool) -> None:
    """Fallback for a QorCap without ``recent_presses``: rising edges of the held set at each poll.

    A tap shorter than the poll can fall between two samples and never show. ``note_edges`` does not have
    that gap; this path stays for older binaries and marks the card ``unhealthy`` the same way as before.
    """
    if card.result:
        return
    prev = set(card.prev)
    fresh = [b for b in held if b not in prev]
    card.prev = list(held)
    if not card.edges:
        for b in fresh:
            note_press(card, b)
    if trusted:
        card.saw_trusted = True


def note_edges(card: ChallengeCard, recent: list, *, trusted: bool) -> bool:
    """Rising edges from ``/status`` ``input.recent_presses`` (QorCap 0.3.37+). Returns False when absent.

    Each edge has a ``seq``; only edges newer than the last one seen are applied, so a tap is counted once
    whatever the poll rate. A gap in ``seq`` (more edges than the ring holds between two polls) marks the
    card unhealthy, the same honest fallback as a slow poll on the held path.
    """
    if not isinstance(recent, list):
        return False
    card.edges = True
    if card.result:
        return True
    rows = []
    for e in recent:
        if not isinstance(e, dict):
            continue
        seq, t_ms, button = e.get("seq"), e.get("t_ms"), e.get("button")
        if isinstance(seq, bool) or not isinstance(seq, int) or not isinstance(button, str):
            continue
        if isinstance(t_ms, bool) or not isinstance(t_ms, int):
            t_ms = None
        rows.append((seq, t_ms, button))
    rows.sort(key=lambda r: r[0])
    if card.edge_seq is not None and rows and rows[0][0] > card.edge_seq + 1:
        card.unhealthy = True
    for seq, t_ms, button in rows:
        if card.edge_seq is not None and seq <= card.edge_seq:
            continue
        card.edge_seq = seq
        if t_ms is None:
            continue
        note_press(card, button, t_ms)
    if card.edge_seq is None:
        card.edge_seq = 0
    if trusted:
        card.saw_trusted = True
    return True


def settle(card: ChallengeCard, now_ms: int, *, trusted: bool, now_s: float) -> None:
    """Local pad observation. Never a referee result, and never gold by itself."""
    if card.result:
        return
    complete = bool(card.pattern) and card.matched >= len(card.pattern)
    open_window = now_ms < card.deadline_ms
    if complete:
        card.result = "pass"
        card.note = "Got it. Buttons matched on this pad. Not a referee result."
        card.settled_at = now_s
        return
    if open_window:
        return
    if card.presses and trusted and not card.unhealthy:
        card.result = "fail"
        card.note = "Buttons in this window did not match the pattern."
    else:
        card.result = "expired"
        if card.unhealthy or (card.presses and not trusted):
            card.note = "Time window closed. This screen may have missed presses. The controller log is what the referee reads."
        else:
            card.note = "Time window closed before the pattern was entered."
    card.settled_at = now_s


def console_snapshot(st: dict, info: dict, signed: int, live) -> dict:
    inp = st.get("input") or {}
    anchors = getattr(live, "anchors", 0) if live is not None else 0
    issued = getattr(live, "issued", ()) if live is not None else ()
    failures = getattr(live, "failures", 0) if live is not None else 0
    if live is None:
        referee = "off"
    elif anchors or issued:
        referee = "linked"
    elif failures:
        referee = "no link"
    else:
        referee = "waiting"
    return {
        "state": st.get("state") or "",
        "secs": st.get("recording_secs") or 0,
        "fps_actual": st.get("fps_actual") or 0,
        "fps_target": st.get("fps_target") or 0,
        "frames": st.get("frames") or 0,
        "dup": st.get("dup_frames") or 0,
        "drop": st.get("drop_frames") or 0,
        "hid_state": inp.get("state") or "off",
        "hid_device": inp.get("device") or "",
        "disk_mb": st.get("disk_free_mb"),
        "warning": st.get("warning") or "",
        "score": info.get("score") or None,
        "segments": info.get("segments") or 0,
        "clips": info.get("clips") or 0,
        "signed": signed,
        "role": (st.get("role") or "").upper(),
        "referee": referee,
        "anchors": anchors,
        "retries": failures if live is not None else 0,
        "res": st.get("res") or "",
    }


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _plain_env(env: dict) -> bool:
    return env.get("QORCAP_PLAIN", "").strip().lower() in {"1", "true", "yes", "on"}


def _color_level(env: dict) -> str:
    if "NO_COLOR" in env:
        return "none"
    pick = env.get("QORCAP_COLOR", "").strip().lower()
    if pick in {"none", "16", "256", "truecolor"}:
        return pick
    if env.get("WT_SESSION") or env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return "truecolor"
    term = env.get("TERM", "")
    if "truecolor" in term:
        return "truecolor"
    if "256color" in term:
        return "256"
    if os.name == "nt":
        return "truecolor"
    if term and term != "dumb":
        return "16"
    return "16"


def _want_unicode(env: dict, stream) -> bool:
    if env.get("QORCAP_ASCII", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    enc = getattr(stream, "encoding", None)
    if not enc:
        return True
    enc = enc.lower().replace("-", "")
    return enc in {"utf8", "utf8sig", "cp65001"}


def enable_windows_vt(stream) -> bool:
    """Turn on VT processing and UTF-8 for the caller's conhost or Windows Terminal.

    A no-op off Windows. The stream's own handle is used. The process standard
    output is a pipe under CI, and probing that pipe refused the branded layout
    for a different terminal. A stand-in with no Win32 handle has no mode bit to
    set. A real console whose mode cannot be set still returns False.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        try:
            handle = msvcrt.get_osfhandle(stream.fileno())
        except (AttributeError, OSError, ValueError):
            return True
        kernel = ctypes.windll.kernel32
        mode = ctypes.c_uint()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not kernel.SetConsoleMode(handle, mode.value | enable):
            return False
        kernel.SetConsoleOutputCP(65001)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return True
    except Exception:
        return False


def branded_requested(plain_flag: bool, stream, env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    if plain_flag or _plain_env(env) or env.get("TERM") == "dumb" or not _isatty(stream):
        return False
    if os.name == "nt" and not enable_windows_vt(stream):
        return False
    return True


class _Palette:
    def __init__(self, level: str):
        self.level = level

    def code(self, name: str) -> str:
        if self.level == "none" or not name or name == "bg":
            if self.level == "none":
                return ""
        if self.level == "none":
            return ""
        name = name or "ink"
        if self.level == "truecolor":
            fg = _TRUE.get(name, _TRUE["ink"])
            bg = _TRUE["bg"]
            return f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m"
        if self.level == "256":
            fg = _C256.get(name, _C256["ink"])
            return f"\x1b[38;5;{fg}m\x1b[48;5;{_C256['bg']}m"
        fg = _C16.get(name, _C16["ink"])
        return f"\x1b[{fg}m\x1b[40m"

    def reset(self) -> str:
        return "" if self.level == "none" else "\x1b[0m"


def _blank(width: int, height: int) -> Grid:
    return [[(" ", "bg") for _ in range(width)] for _ in range(height)]


def _put(grid: Grid, y: int, x: int, text: str, color: str) -> None:
    if y < 0 or y >= len(grid):
        return
    row = grid[y]
    for i, ch in enumerate(text):
        col = x + i
        if 0 <= col < len(row) and ch != "":
            row[col] = (ch, color)


def _center(grid: Grid, y: int, text: str, color: str, x0: int = 0, x1: int | None = None) -> None:
    if y < 0 or y >= len(grid):
        return
    x1 = len(grid[0]) if x1 is None else x1
    span = max(0, x1 - x0)
    start = x0 + max(0, (span - len(text)) // 2)
    _put(grid, y, start, text[:span], color)


def _blit(grid: Grid, y: int, x: int, lines: list[str], color: str) -> None:
    for i, line in enumerate(lines):
        _put(grid, y + i, x, line, color)


def _frame(grid: Grid, color: str, unicode: bool) -> None:
    h, w = len(grid), len(grid[0])
    if w < 2 or h < 2:
        return
    hch, vch = ("─", "│") if unicode else ("-", "|")
    tl, tr, bl, br = ("┌", "┐", "└", "┘") if unicode else ("+", "+", "+", "+")
    _put(grid, 0, 0, tl + hch * (w - 2) + tr, color)
    _put(grid, h - 1, 0, bl + hch * (w - 2) + br, color)
    for y in range(1, h - 1):
        _put(grid, y, 0, vch, color)
        _put(grid, y, w - 1, vch, color)


def _fmt_clock(secs) -> str:
    if secs is None:
        return "--:--"
    s = int(secs)
    return f"{s // 60}:{s % 60:02d}"


def _fmt_elapsed(secs) -> str:
    s = int(secs or 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _fmt_disk(mb) -> str:
    if not isinstance(mb, int):
        return "--"
    if mb >= 10240:
        return f"{mb / 1024:.1f} GB"
    return f"{mb} MB"


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _score_text(score) -> str:
    if not score:
        return "waiting for scorebug"
    return (f"AWAY {score.get('away')}  -  HOME {score.get('home')}"
            f"    Q{score.get('quarter')}  {_fmt_clock(score.get('clock_s'))}")


def _state_label(state: str) -> str:
    return _STATE.get(state, state or "?")


def grid_text(grid: Grid) -> str:
    return "\n".join("".join(ch for ch, _ in row) for row in grid)


def paint(grid: Grid, level: str, *, clear_through: int = 0) -> str:
    pal = _Palette(level)
    parts: list[str] = []
    for y, row in enumerate(grid, 1):
        parts.append(f"\x1b[{y};1H")
        cur = None
        buf: list[str] = []
        for ch, name in row:
            style = pal.code(name)
            if style != cur:
                buf.append(style)
                cur = style
            buf.append(ch)
        parts.append("".join(buf))
        parts.append(pal.reset() + "\x1b[K")
    for y in range(len(grid) + 1, clear_through + 1):
        parts.append(f"\x1b[{y};1H\x1b[K")
    return "".join(parts)


def _spinner(tick: int, reduce: bool) -> str:
    if reduce:
        return "*"
    return _SPINNER[tick % len(_SPINNER)]


def layout_dashboard(snap: dict, *, width: int = 78, height: int = 24, tick: int = 0,
                     unicode: bool = True, banner: str = "", last: tuple | None = None,
                     reduce_motion: bool = False) -> Grid:
    grid = _blank(width, height)
    _frame(grid, "dim", unicode)
    warn = str(snap.get("warning") or "").strip()
    extra = (2 if warn else 0) + (2 if last else 0) + (2 if banner else 0)
    # Wordmark through the footer is 11 rows, plus any warning, last result, or banner.
    top = 1 + max(0, (height - 2 - (11 + extra)) // 2)
    mark = "QOR"
    _put(grid, top, 2, mark, "ink")
    _put(grid, top, 2 + len(mark), "CAP", "cyan")
    tag = "  HDMI IN  ·  HASH CHAIN OUT" if unicode else "  HDMI IN  *  HASH CHAIN OUT"
    _put(grid, top, 2 + len(mark) + 3, tag, "mute")
    state = _state_label(str(snap.get("state") or ""))
    state_color = "red" if state in {"REC", "DISK FULL", "NO SIGNAL"} else "cyan"
    spin = _spinner(tick, reduce_motion)
    spin_color = "red" if state == "REC" else state_color
    role = str(snap.get("role") or "")
    _put(grid, top + 1, 2, f"side {role}" if role else "side --", "ink")
    clock = _fmt_elapsed(snap.get("secs"))
    x_clock = width - 2 - len(clock)
    x_state = x_clock - 2 - len(state)
    x_spin = x_state - 2
    _put(grid, top + 1, x_clock, clock, "ink")
    _put(grid, top + 1, x_state, state, state_color)
    _put(grid, top + 1, x_spin, spin, spin_color)
    _put(grid, top + 3, 4, _score_text(snap.get("score")), "ink" if snap.get("score") else "mute")
    fps = snap.get("fps_actual") or 0
    try:
        fps_s = f"{float(fps):.1f}"
    except (TypeError, ValueError):
        fps_s = "0.0"
    target = snap.get("fps_target") or 0
    res = snap.get("res") or ""
    card = f"CARD    {fps_s} / {target} fps    frames {_fmt_int(snap.get('frames'))}    dup {_fmt_int(snap.get('dup'))}  drop {_fmt_int(snap.get('drop'))}"
    if res:
        card += f"   {res}"
    _put(grid, top + 5, 4, _clip(card, width - 6), "ink")
    _put(grid, top + 5, 4, "CARD", "mute")
    hid = _HID.get(str(snap.get("hid_state") or "off"), str(snap.get("hid_state") or "off"))
    dev = str(snap.get("hid_device") or "").strip()
    hid_line = f"HID     {hid}" + (f"    {dev}" if dev else "")
    hid_color = "cyan" if hid == "connected" else "mute" if hid in {"off", "searching"} else "red"
    _put(grid, top + 6, 4, _clip(hid_line, width - 6), hid_color)
    _put(grid, top + 6, 4, "HID", "mute")
    ref = str(snap.get("referee") or "off")
    ref_line = f"REFEREE {ref}    checkpoints {_fmt_int(snap.get('signed'))}    anchors {_fmt_int(snap.get('anchors'))}"
    retries = snap.get("retries") or 0
    if retries:
        ref_line += f"    retries {_fmt_int(retries)}"
    ref_color = "cyan" if ref == "linked" else "red" if ref == "no link" else "mute"
    _put(grid, top + 7, 4, _clip(ref_line, width - 6), ref_color)
    _put(grid, top + 7, 4, "REFEREE", "mute")
    disk = f"DISK    {_fmt_disk(snap.get('disk_mb'))}    segments {_fmt_int(snap.get('segments'))}    clips {_fmt_int(snap.get('clips'))}"
    _put(grid, top + 8, 4, _clip(disk, width - 6), "ink")
    _put(grid, top + 8, 4, "DISK", "mute")
    y = top + 10
    if warn:
        _put(grid, y, 4, _clip(warn, width - 6), "red")
        y += 2
    if last:
        label, result, verified = last
        word = {"pass": "PASS", "fail": "FAIL", "expired": "EXPIRED"}.get(result, str(result).upper())
        color = "gold" if verified else "cyan" if result == "pass" else "red" if result == "fail" else "mute"
        tail = "referee verified" if verified else "buttons on this pad" if result == "pass" else "window closed"
        _put(grid, y, 4, _clip(f"last challenge   {label}   {word}   {tail}", width - 6), color)
        y += 2
    if banner and y < height - 1:
        _put(grid, y, 4, _clip(banner.replace("\n", " "), width - 6), "ink")
        y += 2
    foot = "Enter or Q stops.   Controller stays read-only."
    _put(grid, min(y, height - 2), 4, _clip(foot, width - 6), "dim")
    return grid


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    if n <= 3:
        return text[:n]
    return text[: n - 3] + "..."


def _pause_hint_fits(pattern: list[str], width: int) -> bool:
    names = {str(name).upper() for name in pattern}
    if "CIRCLE" not in names and "CROSS" not in names:
        return False
    return len(PAUSE_MENU_HINT) <= max(0, width - 4)


def _pattern_rows(pattern: list[str], width: int) -> int:
    if not pattern:
        return 0
    room = max(1, width - 4)
    per_row = max(1, (room + 2) // 11)
    return (len(pattern) + per_row - 1) // per_row


def layout_challenge(card: ChallengeCard, *, width: int = 78, height: int = 36, tick: int = 0,
                     now_ms: int = 0, unicode: bool = True, reduce_motion: bool = False) -> Grid:
    """Challenge card. The big title and countdown fit a 24-row console; extra rows center the card."""
    grid = _blank(width, height)
    color = _frame_color(card)
    _frame(grid, color, unicode)
    phase = (card.label or card.phase or "challenge").upper()
    # 23 rows is the packed card (frame + block title + glyphs + block clock). Shorter windows
    # keep the glyphs and the literal seconds line, with a one-line title.
    compact = height < 23 or width < 58
    title = phase if card.result is None else {"pass": "PASS", "fail": "FAIL", "expired": "EXPIRED"}[card.result]
    title_lines = [title] if compact else (big(title) if card.result is None else emphasize(title, width))
    title_color = _title_color(card)
    specs: list[tuple] = [("center", "REFEREE CHALLENGE", "pink"), ("gap",)]
    for line in title_lines:
        specs.append(("center", line, title_color))
    if card.result is None:
        specs.append(("center", "At the line, before the snap. Then resume.", "ink"))
    else:
        note = "Referee verified." if card.referee_verified else card.note
        specs.append(("center", _clip(note, width - 6), "gold" if card.referee_verified else "ink"))
    specs.append(("gap",))
    specs.append(("glyphs",))
    if card.result is None:
        total = max(1, (int(card.deadline_ms) - int(card.issued_ms)) // 1000)
        remain = max(0, (int(card.deadline_ms) - int(now_ms)) // 1000)
        clock = f"{remain // 60:02d}:{remain % 60:02d}"
        clock_color = "red" if remain <= 10 else "cyan"
        specs.append(("gap",))
        if compact:
            specs.append(("center", clock, clock_color))
        else:
            for line in emphasize(clock, width):
                specs.append(("center", line, clock_color))
        bar_w = min(40, width - 8)
        filled = int(round(bar_w * remain / total))
        filled = max(0, min(bar_w, filled))
        on, off = ("█", "░") if unicode else ("#", "-")
        specs.append(("center", on * filled + off * (bar_w - filled), clock_color))
        left = f"{remain} seconds left    entered {card.matched} of {len(card.pattern)}"
        specs.append(("center", left, "red" if remain <= 10 else "mute"))
        fb = press_feedback(card)
        if fb:
            specs.append(("center", _clip(fb[0], width - 6), fb[1]))
        specs.append(("center", "If the snap comes first, do it at the next play.", "dim"))
    else:
        specs.append(("center", "The referee reads the controller log and the pause.", "dim"))
    glyph_h = _pattern_rows(card.pattern, width) * 6

    def spec_height(spec: tuple) -> int:
        if spec[0] == "gap":
            return 1
        if spec[0] == "glyphs":
            return glyph_h
        return 1

    inner = max(1, height - 2)
    while sum(spec_height(s) for s in specs) > inner and any(s[0] == "gap" for s in specs):
        specs.remove(next(s for s in specs if s[0] == "gap"))
    # A blank row is room. The pattern, the clock, and the snap line stay.
    if card.result is None and _pause_hint_fits(card.pattern, width):
        hint = ("center", PAUSE_MENU_HINT, "dim")
        if sum(spec_height(s) for s in specs) + 1 <= inner:
            specs.append(hint)
        elif any(s[0] == "gap" for s in specs):
            specs.remove(next(s for s in specs if s[0] == "gap"))
            if sum(spec_height(s) for s in specs) + 1 <= inner:
                specs.append(hint)
    content_h = sum(spec_height(s) for s in specs)
    y = 1 + max(0, (inner - content_h) // 2)
    for spec in specs:
        if y >= height - 1:
            break
        kind = spec[0]
        if kind == "gap":
            y += 1
            continue
        if kind == "glyphs":
            y = _blit_pattern(grid, y, card, tick, reduce_motion)
            continue
        _center(grid, y, spec[1], spec[2], 2, width - 2)
        y += 1
    return grid


def press_feedback(card: ChallengeCard) -> tuple[str, str] | None:
    """One line for the newest press: it counted, or it broke the try and what to do next."""
    if card.result is not None or not card.last:
        return None
    if card.wrong:
        extra = f"Extra {card.wrong} (d-pad roll)" if card.rolled else f"Extra {card.wrong}"
        if card.matched:
            return (f"{extra}: not counted. New try started at OPTIONS.", "red")
        return (f"{extra}: not counted. Redo OPTIONS + all at next snap.", "red")
    if card.last_ok and card.matched:
        nxt = card.pattern[card.matched] if card.matched < len(card.pattern) else ""
        return (f"Got {card.last}. Next: {nxt}. Press each button once." if nxt else f"Got {card.last}.", "ok")
    return None


def _frame_color(card: ChallengeCard) -> str:
    if card.referee_verified:
        return "gold"
    if card.result == "fail":
        return "red"
    if card.result == "expired":
        return "mute"
    return "cyan"


def _title_color(card: ChallengeCard) -> str:
    if card.referee_verified:
        return "gold"
    if card.result == "pass":
        return "cyan"
    if card.result == "fail":
        return "red"
    if card.result == "expired":
        return "ink"
    return "cyan"


def _blit_pattern(grid: Grid, y: int, card: ChallengeCard, tick: int, reduce_motion: bool) -> int:
    pattern = list(card.pattern)
    if not pattern or y >= len(grid) - 2:
        return y
    glyph_w, gap = 9, 2
    per = glyph_w + gap
    room = len(grid[0]) - 4
    per_row = max(1, (room + gap) // per)
    rows = [pattern[i:i + per_row] for i in range(0, len(pattern), per_row)]
    index = 0
    pulse = (tick % 2 == 0) or reduce_motion
    for group in rows:
        span = len(group) * glyph_w + (len(group) - 1) * gap
        x = max(2, (len(grid[0]) - span) // 2)
        for name in group:
            done = index < card.matched
            hot = card.result is None and index == card.matched
            if hot:
                color = "hot" if pulse else "cyan"
            elif done and card.result != "fail":
                color = _BUTTON_COLOR.get(name, "ink")
            elif card.result == "fail":
                color = "red"
            elif card.result == "expired":
                color = "mute"
            else:
                color = "dim"
            art = button_glyph(name)
            _blit(grid, y, x, art, color)
            label = name[:glyph_w].center(glyph_w)
            _put(grid, y + len(art), x, label, color)
            x += per
            index += 1
        y += 6
    return y


def layout_plain_screen(line: str, *, width: int = 78, height: int = 12) -> Grid:
    """The historical status line, for the before picture. No chrome."""
    grid = _blank(width, height)
    _put(grid, 1, 0, _clip(line, width), "line")
    return grid


class MatchConsole:
    """Paints on its own thread. ``show_challenge`` only stores the card and returns."""

    def __init__(self, *, plain: bool = False, force: bool = False, out=None, clock=None,
                 status_getter=None, columns: int | None = None, rows: int | None = None,
                 env: dict | None = None):
        self.out = out or sys.stdout
        self.env = os.environ if env is None else env
        self._clock = clock or time.time
        self._status_getter = status_getter
        self._columns = columns
        self._rows = rows
        if force:
            self.branded = not plain
        else:
            self.branded = branded_requested(plain, self.out, self.env)
        self.unicode = _want_unicode(self.env, self.out)
        self.level = "none" if not self.branded else _color_level(self.env)
        self.reduce = self.env.get("QORCAP_REDUCE_MOTION", "").strip().lower() in {"1", "true", "yes", "on"}
        self._lock = threading.Lock()
        self._life = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._snap: dict = {}
        self._card: ChallengeCard | None = None
        self._banner = ""
        self._notices: list[str] = []
        self._last_result: tuple | None = None
        self._thread: threading.Thread | None = None
        self._sampler: threading.Thread | None = None
        self._sampler_started = False
        self._sampler_ready = False
        self._closed = False
        self._entered = False
        self._left = False
        self._last_blob = ""
        self._clear_through = 0

    def start(self) -> None:
        if not self.branded or self._thread is not None:
            return
        set_say_hook(self.hook)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="qorcap-console", daemon=True)
        self._thread.start()

    def hook(self, msg: str) -> bool:
        if not self.branded or self._closed:
            return False
        text = msg.strip()
        if not text:
            return True
        with self._lock:
            self._notices.append(msg)
            if len(self._notices) > 20:
                del self._notices[:-20]
            self._banner = text.replace("\n", " ")
        self._wake.set()
        return True

    def update(self, snap: dict) -> None:
        with self._lock:
            self._snap = dict(snap)
        self._wake.set()

    def show_challenge(self, *, phase: str, label: str, pattern: list[str], issued_ms: int, deadline_ms: int) -> bool:
        card = ChallengeCard(phase=phase, label=label, pattern=list(pattern),
                             issued_ms=int(issued_ms), deadline_ms=int(deadline_ms))
        with self._lock:
            self._card = card
            self._banner = ""
        self._wake.set()
        return bool(self.branded)

    def observe_buttons(self, held, *, trusted: bool = False, recent=None) -> None:
        """``recent`` is ``/status`` ``input.recent_presses`` when this QorCap has it; otherwise held-state edges."""
        with self._lock:
            card = self._card
            if card is None or card.result:
                return
            note_edges(card, recent, trusted=trusted)
            note_held(card, [str(b) for b in held], trusted=trusted)
        self._wake.set()

    def disable(self) -> None:
        self.branded = False
        self.close()

    def close(self) -> None:
        notices: list[str]
        with self._life:
            if self._closed:
                return
            self._closed = True
            self.branded = False
            self._stop.set()
            self._wake.set()
            with self._lock:
                notices = list(self._notices)
                self._notices.clear()
        if self._thread is not None:
            self._thread.join(1.0)
        if self._sampler is not None:
            self._sampler.join(1.0)
        self._leave()
        set_say_hook(None)
        for msg in notices:
            try:
                self.out.write(msg + ("\n" if not msg.endswith("\n") else ""))
                self.out.flush()
            except Exception:
                pass

    def render(self, *, tick: int = 0, now_ms: int | None = None) -> Grid:
        snap, card, banner, last = self._copy()
        width, height = self._size(card)
        now_ms = int(self._clock() * 1000) if now_ms is None else now_ms
        if card is not None:
            return layout_challenge(card, width=width, height=height, tick=tick, now_ms=now_ms,
                                    unicode=self.unicode, reduce_motion=self.reduce)
        return layout_dashboard(snap, width=width, height=min(height, 22), tick=tick, unicode=self.unicode,
                                banner=banner, last=last, reduce_motion=self.reduce)

    def _copy(self):
        with self._lock:
            card = replace(self._card, presses=list(self._card.presses), prev=list(self._card.prev)) if self._card else None
            return dict(self._snap), card, self._banner, self._last_result

    def _size(self, card: ChallengeCard | None) -> tuple[int, int]:
        if self._columns:
            cols = self._columns
            rows = self._rows or (36 if card else 22)
            return cols, rows
        size = shutil.get_terminal_size(fallback=(80, 28))
        cols = max(60, min(size.columns, 100))
        rows = size.lines if size.lines >= 24 else 24
        if card is not None:
            rows = max(rows, 24)
        return cols, rows

    def _loop(self) -> None:
        try:
            self._enter()
            while not self._stop.is_set():
                self._tick()
                self._wake.wait(0.25 if not self.reduce else 0.8)
                self._wake.clear()
        except Exception:
            self.branded = False
            self._leave()
            try:
                self.out.write("\nQorCap console display stopped. Plain status follows.\n")
                self.out.flush()
            except Exception:
                pass
        else:
            self._leave()

    def _enter(self) -> None:
        if self._entered:
            return
        self._entered = True
        seq = "\x1b[?1049h\x1b[?25l\x1b[H"
        if self.env.get("WT_SESSION"):
            seq = "\x1b[?2026h" + seq
        self.out.write(seq)
        self.out.flush()

    def _leave(self) -> None:
        if self._left or not self._entered:
            self._left = True
            return
        self._left = True
        try:
            seq = "\x1b[?25h\x1b[?1049l"
            if self.env.get("WT_SESSION"):
                seq = "\x1b[?2026l" + seq
            self.out.write(seq)
            self.out.flush()
        except Exception:
            pass

    def _ensure_sampler(self) -> None:
        with self._lock:
            if self._sampler_started or self._status_getter is None or self._card is None:
                return
            self._sampler_started = True
        self._sampler = threading.Thread(target=self._sample_loop, name="qorcap-console-pad", daemon=True)
        self._sampler.start()

    def _sample_loop(self) -> None:
        last_ok = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                active = self._card is not None and self._card.result is None
            if not active:
                if self._stop.wait(0.25):
                    return
                continue
            try:
                st = self._status_getter()
                inp = (st or {}).get("input") or {}
                held = inp.get("buttons") or []
                self.observe_buttons(held, trusted=False, recent=inp.get("recent_presses"))
                self._sampler_ready = True
                # A slow or stalled read can miss a tap on the held path. The edge ring does not miss one;
                # its own seq gap check covers an overflow.
                if time.monotonic() - last_ok > 0.45:
                    with self._lock:
                        if self._card is not None and not self._card.edges:
                            self._card.unhealthy = True
                last_ok = time.monotonic()
            except Exception:
                if time.monotonic() - last_ok > 0.45:
                    with self._lock:
                        if self._card is not None:
                            self._card.unhealthy = True
            self._stop.wait(0.125)

    def _settle_locked(self, card: ChallengeCard) -> None:
        now_s = float(self._clock())
        now_ms = int(now_s * 1000)
        trusted = card.saw_trusted or (self._sampler_ready and not card.unhealthy)
        settle(card, now_ms, trusted=trusted, now_s=now_s)
        if card.result and card.settled_at is not None and now_s - card.settled_at >= _HOLD_S:
            verified = card.referee_verified
            self._last_result = (card.label, card.result, verified)
            self._card = None

    def _tick(self) -> None:
        self._ensure_sampler()
        with self._lock:
            if self._card is not None:
                self._settle_locked(self._card)
        tick = 0 if self.reduce else int(time.monotonic() * 4)
        grid = self.render(tick=tick)
        blob = paint(grid, self.level, clear_through=self._clear_through)
        self._clear_through = max(self._clear_through, len(grid))
        if blob != self._last_blob:
            self.out.write(blob)
            self.out.flush()
            self._last_blob = blob


def sample_snap() -> dict:
    return {
        "state": "capturing", "secs": 14 * 60 + 2, "fps_actual": 29.7, "fps_target": 30,
        "frames": 25920, "dup": 12, "drop": 1, "hid_state": "connected",
        "hid_device": "DualSense Edge", "disk_mb": 84100, "warning": "",
        "score": {"away": 14, "home": 21, "quarter": 2, "clock_s": 8 * 60 + 41},
        "segments": 128, "clips": 3, "signed": 410, "role": "HOME", "referee": "linked",
        "anchors": 7, "retries": 0, "res": "1280x720",
    }


def sample_card(result: str | None = None, *, verified: bool = False, matched: int | None = None) -> ChallengeCard:
    pattern = ["OPTIONS", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE"]
    card = ChallengeCard(phase="kickoff", label="game start", pattern=pattern,
                         issued_ms=1_000, deadline_ms=91_000, referee_verified=verified)
    if result == "pass":
        card.matched = len(pattern)
        card.presses = list(pattern)
        card.result = "pass"
        card.note = "Got it. Buttons matched on this pad. Not a referee result."
    elif result == "fail":
        card.matched = 2
        card.presses = ["OPTIONS", "CROSS", "UP"]
        card.result = "fail"
        card.note = "Buttons in this window did not match the pattern."
    elif result == "expired":
        card.result = "expired"
        card.note = "Time window closed before the pattern was entered."
    elif matched:
        card.matched = matched
    if verified and result == "pass":
        card.referee_verified = True
    return card


def _self_check() -> None:
    for name in ("CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "OPTIONS", "L1"):
        button_glyph(name)
    for lines in _FONT.values():
        if any(len(row) != 4 for row in lines):
            raise SystemExit("font width drifted")


def main(argv: list[str] | None = None) -> int:
    """Paint sample frames in this console. Records nothing and talks to no device."""
    _self_check()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv:
        print(__doc__)
        return 0
    out = sys.stdout
    branded = branded_requested("--plain" in argv, out)
    level = "none" if not branded else _color_level(os.environ)
    unicode = _want_unicode(os.environ, out)
    scenes: list[tuple[str, Grid]] = []
    plain = status_line_sample()
    scenes.append(("before", layout_plain_screen(plain, width=78, height=8)))
    scenes.append(("status", layout_dashboard(sample_snap(), width=78, height=18, tick=1, unicode=unicode)))
    now = 1_000 + 18_000
    for name, card in (
        ("pending", sample_card(matched=1)),
        ("pass", sample_card("pass")),
        ("fail", sample_card("fail")),
        ("expired", sample_card("expired")),
    ):
        scenes.append((name, layout_challenge(card, width=78, height=36, tick=1, now_ms=now, unicode=unicode)))
    if not branded:
        for name, grid in scenes:
            print(f"\n# {name}")
            print(grid_text(grid))
        return 0
    ui = MatchConsole(force=True, out=out, columns=78, rows=36)
    try:
        ui._enter()
        for name, grid in scenes:
            height = 18 if name in {"before", "status"} else 36
            if name == "before":
                height = 8
            blob = paint(grid, level, clear_through=height)
            out.write("\x1b[H" + blob)
            out.flush()
            time.sleep(1.6 if name != "before" else 1.2)
    finally:
        ui._leave()
    return 0


def status_line_sample() -> str:
    return "[REC] 14:02 | AWAY 14 - HOME 21  Q2 8:41 | segments 128 | signed 410 | clips 3 | disk 84100MB"


if __name__ == "__main__":
    _self_check()
    raise SystemExit(main())
