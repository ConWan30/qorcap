"""Qortrola30 on the CoD killfeed: whose kill is each line.

Each killfeed line is ``killer  [weapon icon]  victim``. The killer is the name
on the left. This reader matches the gamertag ``OWNER_TAG`` on each side of a
line and returns, per line:

* ``killer``: ``self``, ``not_self`` or ``unreadable``, with a confidence.
* ``victim``: the same three values. ``self`` there is a death, not a kill.

Unreadable stays unknown. Nothing is guessed: a line that is not clearly
Qortrola30 and not clearly another name is ``unreadable``.

The gamertag template is built from a glyph bank rendered from a monospace
font (``packs/cod_killfeed_font.json``), one glyph per character, for every
letter and digit. No glyph is cut from a frame of Con's own name. The tag is
the font's ``Q o r t r o l a 3 0`` laid out at the killfeed pitch. Where the
font draws two glyphs nearly alike (Q/O, l/I), a cell that only loses to its
twin is settled by colour: the game draws the local player's own name in gold.

``KillfeedTally`` follows lines across frames, keeps one count per line,
and reports own-kill coverage for the three-leg agreement gate. Coverage is
``covered`` only when every line seen was read as self or not self, and the
frames have no hole longer than a line stays on screen. Anything else is
``unknown`` and cannot release a Counted final.

Layout measured on Con's 720p capture (match-20261106-201029
and the committed mid_173922_120s fixture): the feed sits at the left, killer
names start at x 30 of 1280, lines are about 15.6 px apart, the font pitch is
about 7 px. Other layouts are not read.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FONT_PACK_PATH = Path(__file__).resolve().parent / "packs" / "cod_killfeed_font.json"

SELF = "self"
NOT_SELF = "not_self"
UNREADABLE = "unreadable"

# Reference frame is 1280x720. Everything below scales by height / 720.
REF_H = 720
# Killer names are left-aligned here (x of the first glyph column).
FEED_LEFT_X = 30
# Vertical band the feed can occupy, as a share of height.
FEED_BAND = (0.40, 0.76)
# The feed never runs past this share of the width.
FEED_RIGHT = 0.46
# Glyph pitch is searched in this range (px at 720p).
PITCH_RANGE = (6.85, 7.15)
# Victim names are drawn narrower than killer names: pitch 6.5 to 6.75 px at
# 720p, measured on other players' victim names (11 lines,
# match-20261106-201029); killer names measured 7.0 to 7.15 the same way.
VICTIM_PITCH_RANGE = (6.45, 6.80)
PITCH_STEP = 0.05
# A thin bright stroke on a 7x7 top-hat of brightness is ink.
INK_MIN = 40
# A line's glyph rows span this many px at 720p.
LINE_MIN_H = 8
LINE_MAX_H = 16
# The victim name starts within this many pitches after the icon.
VICTIM_SPAN_PITCH = 8
# Killer names (clan tag included) are at most this many characters, so the
# icon starts no further right than this. White blobs further out are scenery.
MAX_KILLER_CHARS = 24
# Weapon, grenade, knife or streak icon: a white mark at least this many pitches wide.
ICON_MIN_PITCH = 1.0
ICON_SAT_MAX = 80
ICON_VAL_MIN = 150
# The icon is drawn solid white; pale name text that leaks into the white mask
# is dimmer once its gaps are closed. Icons measured 166 to 177 mean brightness,
# leaked name text 89 to 148 (match-20261106-201029).
ICON_MEAN_V_MIN = 155
# Gates on the gamertag template. Fixed before validation on real lines.
SELF_MIN = 0.70
NOT_SELF_MAX = 0.50
# Each character cell, at the fitted placement, has to match its own font glyph
# at least this well, and no other glyph may beat it by more than CELL_SLACK.
# A cell that reads as another character (Qortrola31, Qortro1a30) stops the tag.
CELL_MIN = 0.55
CELL_SLACK = 0.03
# Two glyphs whose own font templates match this well (Q/O 0.94, l/I 0.86 at
# 720p) cannot be told apart by shape at killfeed size. A cell that only loses
# to such a twin is settled by the name colour instead: the game draws the
# local player's own name in gold, teammates in cyan and enemies in red.
CONFUSABLE_NCC = 0.80
# Gold: hue 15 to 35 (OpenCV 0-179) with saturation above GOLD_SAT, as a share
# of the name's saturated ink. Own-name lines measured 0.80 to 1.00, other
# names 0.00 to 0.10 (match-20261106-201029, 199 lines).
GOLD_HUE = (15, 35)
GOLD_SAT = 60
GOLD_MIN = 0.60
# A name that cannot be read as text but whose ink is clearly not gold is not
# the local player's name: the game never draws the own name in another colour.
NOT_GOLD_MAX = 0.20
# A side is only read as another name when its cells look like font text:
# the 20th percentile of each cell's best glyph score. Faint or covered text
# stays unreadable.
LEGIBLE_MIN = 0.50
# A cell after the tag that matches a font glyph this well is another character.
NEXT_GLYPH_MIN = 0.65
# The runner reads the feed about this often (it was checked at 4 per second).
READ_EVERY_US = 250_000
# Frames may not be farther apart than this, or a short line could be missed.
MAX_FRAME_GAP_US = 1_500_000
# A line not seen for this long is closed. Lines stay on screen about 5 s.
TRACK_GAP_US = 2_500_000
# The icon sits right after the killer name, so its x stays put for one line
# and moves with the killer name's length. Its thin left tip comes and goes
# (116 to 124 px seen on one line), so the tolerance is about 1.5 glyphs.
# Two reads further apart than this are two lines.
ICON_X_TOL = 10.0
# The feed is anchored at the bottom: a new line pushes every older line up by
# one line pitch. Lines that time out at the top do not move the others.
LINE_PITCH = 16.0
LINE_Y_TOL = 6.0
MAX_SHIFT = 4
# A line is counted only after this many frames show it.
MIN_TRACK_FRAMES = 2
# One line split across two tracks (a missed match while it slid or while
# something crossed it) is merged back when: the two were never seen in the
# same frame, the hole between them is under MAX_FRAME_GAP_US, the line never
# moves down (lines only rise), both have the same killer verdict (or one is
# unreadable), and the whole run fits one line's life on screen. A line stays
# about 5.25 s (measured on match-20261106-201029, all lines, max 5.5 s).
LINE_LIFE_MAX_US = 6_000_000
REENTRY_GAP_US = 750_000


def _libs():
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, None
    return cv2, np


_BANK_CACHE: dict = {}


def font_bank():
    """Glyph masks from the font pack, as float arrays at the supersampled size."""
    cv2, np = _libs()
    if np is None or not FONT_PACK_PATH.is_file():
        return None
    key = str(FONT_PACK_PATH)
    if key in _BANK_CACHE:
        return _BANK_CACHE[key]
    data = json.loads(FONT_PACK_PATH.read_text(encoding="utf-8"))
    glyphs = {}
    for item in data.get("glyphs", []):
        rows = item.get("rows") or []
        if not rows or any(len(r) != len(rows[0]) for r in rows):
            continue
        arr = np.array([[int(c) for c in r] for r in rows], dtype=np.float32) * (255.0 / 9.0)
        glyphs[str(item["char"])] = arr
    bank = {
        "glyphs": glyphs,
        "supersample": int(data.get("supersample", 4)),
        "pitch": float(data.get("pitch_px_720", 7.0)),
        "cell_h": int(data.get("cell_h_720", 14)),
        "font": data.get("font"),
    }
    _BANK_CACHE[key] = bank
    return bank


def word_template(text: str, pitch: float, scale: float = 1.0):
    """The word laid out from font glyphs at ``pitch`` px per character (720p px times scale)."""
    cv2, np = _libs()
    bank = font_bank()
    if bank is None:
        return None
    ss = bank["supersample"]
    glyphs = bank["glyphs"]
    cell_w = next(iter(glyphs.values())).shape[1]
    cell_h = next(iter(glyphs.values())).shape[0]
    step = cell_w * pitch / bank["pitch"]
    width = int(round(step * (len(text) - 1) + cell_w))
    canvas = np.zeros((cell_h, width), np.float32)
    for i, ch in enumerate(text):
        g = glyphs.get(ch)
        if g is None:
            if ch == " ":
                continue
            return None
        x = int(round(i * step))
        w = min(cell_w, width - x)
        canvas[:, x:x + w] = np.maximum(canvas[:, x:x + w], g[:, :w])
    out_w = max(1, int(round(width / ss * scale)))
    out_h = max(1, int(round(cell_h / ss * scale)))
    return cv2.resize(canvas, (out_w, out_h), interpolation=cv2.INTER_AREA)


def _as_bgr(img):
    cv2, np = _libs()
    if np is None or not isinstance(img, np.ndarray):
        return None
    if img.ndim != 3 or img.shape[2] < 3 or img.dtype != np.uint8:
        return None
    if img.shape[0] < 120 or img.shape[1] < 200:
        return None
    return img[:, :, :3]


def _scale(bgr):
    h, w = bgr.shape[:2]
    s = h / REF_H
    if not (0.9 <= s <= 1.6) or abs(w / h - 16 / 9) > 0.02:
        return None
    return s


def _tophat(cv2, np, v, scale):
    k = max(5, int(round(7 * scale)) | 1)
    return cv2.morphologyEx(v, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))).astype(np.float32)


def _ncc(cv2, patch, templ):
    if patch.shape[0] < templ.shape[0] or patch.shape[1] < templ.shape[1]:
        return -1.0, (0, 0)
    res = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
    _mn, mx, _l, loc = cv2.minMaxLoc(res)
    return float(mx), loc


def _row_runs(profile, min_h, max_h):
    runs = []
    start = None
    gap = 0
    for y, on in enumerate(list(profile) + [False, False]):
        if on:
            if start is None:
                start = y
            gap = 0
            end = y
        elif start is not None:
            gap += 1
            if gap > 1:
                if min_h <= end - start + 1 <= max_h:
                    runs.append((start, end))
                start = None
                gap = 0
    return runs


@dataclass
class _Fit:
    score: float = -1.0
    pitch: float = 0.0
    x: int = 0
    y: int = 0
    template: object = None


def _fit_tag(cv2, np, ink, x0, y0, scale, tag, jx, jy, pitches=PITCH_RANGE):
    """Best placement of the tag template near (x0, y0) over the pitch range."""
    best = _Fit()
    p = pitches[0]
    while p <= pitches[1] + 1e-9:
        t = word_template(tag, p * scale)
        if t is not None:
            ys, xs = max(0, y0 - jy), max(0, x0 - jx)
            patch = ink[ys:y0 + t.shape[0] + jy, xs:x0 + t.shape[1] + jx]
            score, loc = _ncc(cv2, patch, t)
            if score > best.score:
                best = _Fit(score, p, xs + loc[0], ys + loc[1], t)
        p += PITCH_STEP
    return best


_CELL_CACHE: dict = {}


def _glyph_templates(pitch_px: float):
    key = round(pitch_px, 3)
    if key not in _CELL_CACHE:
        bank = font_bank()
        _CELL_CACHE[key] = {c: word_template(c, pitch_px) for c in bank["glyphs"]}
    return _CELL_CACHE[key]


def _cell_check(cv2, np, ink, fit: _Fit, scale, tag):
    """Each character of the tag against every font glyph, in its own cell.

    Returns the smallest lead of the expected glyph over the best other glyph,
    the cell where that happens, and the glyph that came closest.
    """
    pitch = fit.pitch * scale
    temps = _glyph_templates(pitch)
    lead = None
    where = None
    rival = None
    leads = []
    rivals = []
    _cell_check.last = None
    _cell_check.rivals = None
    for i, ch in enumerate(tag):
        t0 = temps.get(ch)
        if t0 is None:
            return None, i, None
        cx = int(round(fit.x + i * pitch))
        patch = ink[max(0, fit.y - 1):fit.y + t0.shape[0] + 1, max(0, cx - 1):cx + t0.shape[1] + 1]
        own, _ = _ncc(cv2, patch, t0)
        if own < CELL_MIN:
            return own - 1.0, i, None
        best_other, best_c = -1.0, None
        for c, t in temps.items():
            if c == ch or t is None or t.shape != t0.shape:
                continue
            s, _ = _ncc(cv2, patch, t)
            if s > best_other:
                best_other, best_c = s, c
        d = own - best_other
        leads.append(round(d, 3))
        rivals.append(best_c)
        if lead is None or d < lead:
            lead, where, rival = d, i, best_c
    _cell_check.last = leads
    _cell_check.rivals = rivals
    return lead, where, rival


def _side(cv2, np, ink, x0, y_top, scale, tag, jx):
    fit = _fit_tag(cv2, np, ink, x0, y_top - int(round(1 * scale)), scale, tag, jx, int(round(3 * scale)))
    return fit


def read_killfeed_lines(img, tag: str | None = None) -> dict:
    """Every killfeed line on one frame, with the killer and victim checked against the tag."""
    from qorcap_cod import OWNER_TAG
    tag = tag or OWNER_TAG
    cv2, np = _libs()
    bgr = _as_bgr(img)
    if cv2 is None or bgr is None:
        return {"ok": False, "reason": "frame_invalid", "lines": []}
    scale = _scale(bgr)
    if scale is None:
        return {"ok": False, "reason": "layout_unknown", "lines": []}
    if font_bank() is None:
        return {"ok": False, "reason": "font_pack_missing", "lines": []}
    H, W = bgr.shape[:2]
    y0b, y1b = int(FEED_BAND[0] * H), int(FEED_BAND[1] * H)
    x1b = int(FEED_RIGHT * W)
    band = bgr[y0b:y1b, :x1b]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    ink = _tophat(cv2, np, hsv[:, :, 2], scale)
    on = ink > INK_MIN
    left = int(round(FEED_LEFT_X * scale))
    pitch = 7.0 * scale
    strip = on[:, max(0, left - 1):left + int(round(2.5 * pitch))]
    profile = strip.sum(axis=1) >= 2
    runs = _row_runs(profile, int(LINE_MIN_H * scale), int(round(LINE_MAX_H * scale)))
    white = ((hsv[:, :, 1] < ICON_SAT_MAX) & (hsv[:, :, 2] > ICON_VAL_MIN)).astype(np.uint8)
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)))
    lines = []
    for r0, r1 in runs:
        ya, yb = max(0, r0 - 2), min(band.shape[0], r1 + 3)
        n, lab, st, _ = cv2.connectedComponentsWithStats(white[ya:yb, :], connectivity=8)
        icon = None
        for i in range(1, n):
            x, y, w, h, area = (int(t) for t in st[i])
            if w >= ICON_MIN_PITCH * pitch and h >= 4 * scale and area >= 20 * scale * scale and x > left + pitch and w <= 8 * pitch \
                    and x <= left + (MAX_KILLER_CHARS + 1.5) * pitch:
                if icon is not None and x >= icon[0]:
                    continue
                if float(hsv[ya:yb, :, 2][lab == i].mean()) < ICON_MEAN_V_MIN:
                    continue
                icon = (x, w)
        if icon is None:
            continue
        icon_x0, icon_w = icon
        killer_cols = np.nonzero(on[r0:r1 + 1, left - 1:max(left, icon_x0 - 1)].any(axis=0))[0]
        line = {
            "y": int(y0b + r0), "h": int(r1 - r0 + 1), "icon_x": int(icon_x0),
            "killer": UNREADABLE, "killer_conf": 0.0, "killer_reason": None,
            "victim": UNREADABLE, "victim_conf": 0.0, "victim_reason": None,
        }
        if killer_cols.size == 0:
            line["killer_reason"] = "no_killer_ink"
        else:
            killer_end = left - 1 + int(killer_cols.max())
            fit = _side(cv2, np, ink, left, r0, scale, tag, int(round(3 * scale)))
            gold = _gold_share(cv2, np, hsv, ink, left - 1, icon_x0 - 1, r0, r1 + 1)
            line.update(_classify(cv2, np, ink, fit, scale, tag, killer_end, icon_x0, "killer", word_x0=left, gold=gold))
        line["killer_sig"] = _signature(cv2, np, ink, left - 1, r0, 16 * pitch, scale)
        # Victim: the tag is slid across the stretch after the icon. Skull or
        # headshot marks can sit between the icon and the name.
        vx0 = icon_x0 + icon_w
        span = int(round(VICTIM_SPAN_PITCH * pitch))
        fit = _fit_tag(cv2, np, ink, vx0 + span // 2, r0 - int(round(scale)), scale, tag,
                       span // 2, int(round(3 * scale)), VICTIM_PITCH_RANGE)
        if fit.template is None:
            line["victim_reason"] = "font_pack_missing"
        else:
            vcols = np.nonzero(on[r0:r1 + 1, fit.x:].any(axis=0))[0]
            victim_end = fit.x + int(_word_end(vcols, pitch))
            gold = _gold_share(cv2, np, hsv, ink, fit.x, fit.x + fit.template.shape[1], r0, r1 + 1)
            line.update(_classify(cv2, np, ink, fit, scale, tag, victim_end, None, "victim", gold=gold))
        line["victim_sig"] = _signature(cv2, np, ink, icon_x0, r0, 18 * pitch, scale)
        lines.append(line)
    return {"ok": True, "reason": "read", "lines": lines, "scale": scale}


def _word_end(cols, pitch):
    """Last ink column before a gap wider than two glyphs."""
    if cols.size == 0:
        return 0
    end = int(cols[0])
    for c in cols[1:]:
        if c - end > 2.0 * pitch:
            break
        end = int(c)
    return end


def _signature(cv2, np, ink, x0, r0, width, scale):
    """Shape of one side of a line, for following it across frames.

    Binarized ink in a fixed window, so where the name ends does not change it.
    """
    y0 = max(0, r0 - int(round(scale)))
    y1 = r0 + int(round(12 * scale))
    x0 = max(0, int(x0))
    patch = (ink[y0:y1, x0:x0 + int(width)] > INK_MIN).astype(np.float32)
    if patch.size == 0 or patch.shape[1] < 4:
        return None
    v = cv2.resize(patch, (56, 6), interpolation=cv2.INTER_AREA).ravel()
    v = v - float(v.mean())
    n = float(np.linalg.norm(v))
    return None if n < 1e-6 else (v / n)


def _legibility(cv2, np, ink, x0, y_top, n_cells, pitch):
    """How much the stretch reads as font text: 20th percentile of the best glyph per cell."""
    temps = _glyph_templates(pitch)
    t0 = next(iter(temps.values()))
    best = []
    for i in range(max(1, n_cells)):
        cx = int(round(x0 + i * pitch))
        patch = ink[max(0, y_top - 2):y_top + t0.shape[0] + 1, max(0, cx - 1):cx + t0.shape[1] + 1]
        best.append(max(_ncc(cv2, patch, t)[0] for t in temps.values() if t is not None))
    return float(np.percentile(best, 20))


def _glyph_after(cv2, np, ink, fit: _Fit, pitch, scale, n):
    """True when the cell right after the tag holds a font glyph, not a speck."""
    temps = _glyph_templates(pitch)
    t0 = temps.get("o")
    cx = int(round(fit.x + n * pitch))
    patch = ink[max(0, fit.y - 1):fit.y + t0.shape[0] + 1, max(0, cx - 1):cx + t0.shape[1] + 1]
    if int((patch > INK_MIN).sum()) < int(round(6 * scale * scale)):
        return False
    best = max(_ncc(cv2, patch, t)[0] for t in temps.values() if t is not None)
    return best >= NEXT_GLYPH_MIN


_TWIN_CACHE: dict = {}


def _twins(a: str, b) -> bool:
    """True when the font itself draws a and b nearly alike at killfeed size."""
    if b is None or a == b:
        return False
    key = (a, b) if a < b else (b, a)
    if key not in _TWIN_CACHE:
        cv2, np = _libs()
        temps = _glyph_templates(7.0)
        ta, tb = temps.get(a), temps.get(b)
        if ta is None or tb is None or ta.shape != tb.shape:
            _TWIN_CACHE[key] = False
        else:
            score, _ = _ncc(cv2, ta, tb)
            _TWIN_CACHE[key] = score >= CONFUSABLE_NCC
    return _TWIN_CACHE[key]


def _gold_share(cv2, np, hsv, ink, x0, x1, y0, y1):
    """Share of a name's saturated ink drawn in the local player's gold, or None."""
    x0, x1 = max(0, int(x0)), max(0, int(x1))
    y0, y1 = max(0, int(y0)), max(0, int(y1))
    if x1 <= x0 or y1 <= y0:
        return None
    m = ink[y0:y1, x0:x1] > INK_MIN
    px = hsv[y0:y1, x0:x1][m]
    if px.shape[0] < 20:
        return None
    strong = px[px[:, 1] > GOLD_SAT]
    if strong.shape[0] < 10:
        return 0.0
    hue = strong[:, 0]
    return float(((hue >= GOLD_HUE[0]) & (hue <= GOLD_HUE[1])).mean())


def _classify(cv2, np, ink, fit: _Fit, scale, tag, word_end, icon_x0, side, word_x0=None, gold=None):
    """self, not_self or unreadable for one side of a line. Never a guess."""
    out = {f"{side}_score": round(fit.score, 4), f"{side}_gold": None if gold is None else round(gold, 3)}
    val, conf, why = side, f"{side}_conf", f"{side}_reason"
    if fit.template is None:
        out[val], out[conf], out[why] = UNREADABLE, 0.0, "font_pack_missing"
        return out
    pitch = fit.pitch * scale
    x0 = fit.x if word_x0 is None else word_x0
    tag_end = fit.x + fit.template.shape[1]
    chars = (word_end - x0 + 1 + 1.0 * scale) / pitch
    out[f"{side}_chars"] = round(chars, 2)
    if fit.score >= SELF_MIN:
        if _glyph_after(cv2, np, ink, fit, pitch, scale, len(tag)):
            # A glyph follows the tag before the icon: a longer name, not this tag.
            out[val], out[conf], out[why] = NOT_SELF, round(fit.score, 4), "longer_name"
            return out
        lead, where, rival = _cell_check(cv2, np, ink, fit, scale, tag)
        out[f"{side}_cell_lead"] = None if lead is None else round(lead, 4)
        cells = getattr(_cell_check, "last", None)
        out[f"{side}_cells"] = None if cells is None else list(cells)
        rivals = getattr(_cell_check, "rivals", None)
        if lead is not None and lead >= -CELL_SLACK:
            out[val], out[conf], out[why] = SELF, round(min(fit.score, 1.0 + min(0.0, lead)), 4), "tag_match"
        elif (cells is not None and rivals is not None and gold is not None and gold >= GOLD_MIN
              and all(d >= -CELL_SLACK or _twins(tag[i], rivals[i]) for i, d in enumerate(cells))):
            # Only font twins (Q/O, l/I) beat a cell, and the name is in the
            # local player's gold.
            out[val], out[conf], out[why] = SELF, round(min(fit.score, gold), 4), "tag_match_gold"
        else:
            note = f"cell_{where}" + ("" if rival is None else f"_reads_{rival}")
            out[val], out[conf], out[why] = UNREADABLE, round(fit.score, 4), note
        return out
    n_cells = max(1, int(round(chars)))
    legible = _legibility(cv2, np, ink, x0, fit.y + int(round(scale)), n_cells, pitch)
    out[f"{side}_legibility"] = round(legible, 4)
    if legible >= LEGIBLE_MIN and fit.score <= NOT_SELF_MAX:
        out[val], out[conf], out[why] = NOT_SELF, round(min(legible, 1.0 - fit.score), 4), "tag_mismatch"
        return out
    if gold is not None and gold <= NOT_GOLD_MAX and fit.score < SELF_MIN:
        out[val], out[conf], out[why] = NOT_SELF, round(1.0 - gold, 4), "not_gold"
        return out
    if legible < LEGIBLE_MIN:
        out[val], out[conf], out[why] = UNREADABLE, round(legible, 4), "faint_text"
        return out
    out[val], out[conf], out[why] = UNREADABLE, round(fit.score, 4), "between_gates"
    return out


def _conflict(reads: dict, val) -> bool:
    """A self read on a line read only as another name so far, or the reverse."""
    if val == SELF:
        return reads[NOT_SELF] > 0 and reads[SELF] == 0
    if val == NOT_SELF:
        return reads[SELF] > 0 and reads[NOT_SELF] == 0
    return False


def _sig_ncc(np, a, b) -> float:
    if a is None or b is None or a.shape != b.shape:
        return -1.0
    return float(np.dot(a, b))


@dataclass
class _Track:
    n: int
    first_us: int
    last_us: int
    y: float
    killer_sig: object
    victim_sig: object
    killer: dict = field(default_factory=lambda: {SELF: 0, NOT_SELF: 0, UNREADABLE: 0})
    victim: dict = field(default_factory=lambda: {SELF: 0, NOT_SELF: 0, UNREADABLE: 0})
    frames: int = 0
    best_conf: float = 0.0
    seen: list = field(default_factory=list)
    icon_x: float = 0.0
    def verdict(self, side: str = "killer") -> str:
        reads = self.killer if side == "killer" else self.victim
        s, ns = reads[SELF], reads[NOT_SELF]
        if s >= MIN_TRACK_FRAMES and s >= 3 * ns:
            return SELF
        if ns >= 1 and s == 0:
            return NOT_SELF
        return UNREADABLE


class KillfeedTally:
    """Own kills from the killfeed, one count per line, across frames.

    ``update`` takes one frame's lines and its frame-clock time. Frames without
    a time are not placed and make coverage unknown. A hole longer than
    ``MAX_FRAME_GAP_US`` makes coverage unknown: a line could have come and
    gone in it. A line read in fewer than ``MIN_TRACK_FRAMES`` frames is
    transient and not counted. A line whose killer stays unreadable, or reads
    as both Qortrola30 and another name, makes coverage unknown. ``hold``
    advances the tally's time on a frame that shows no feed (the end screen).
    """

    def __init__(self):
        self.tracks: list[_Track] = []
        self.frames = 0
        self.first_us = None
        self.last_us = None
        self.gaps = 0
        self.unplaced = 0
        self.faults = 0
        self.held = False
        self._next = 0

    def _clock(self, t_us) -> bool:
        if isinstance(t_us, bool) or not isinstance(t_us, int) or t_us < 0:
            self.unplaced += 1
            return False
        if self.last_us is not None:
            if t_us <= self.last_us:
                self.faults += 1
                return False
            if t_us - self.last_us > MAX_FRAME_GAP_US:
                self.gaps += 1
        if self.first_us is None:
            self.first_us = t_us
        self.last_us = t_us
        self.frames += 1
        return True

    def fault(self) -> None:
        self.faults += 1

    def hold(self, t_us) -> None:
        """A frame with no killfeed on screen (the end board). No lines are read."""
        if self._clock(t_us):
            self.held = True

    def update(self, t_us, read: dict) -> None:
        if not isinstance(read, dict) or read.get("ok") is not True:
            self.faults += 1
            return
        if self.held:
            # The end board already showed. Lines after it are not counted.
            self._clock(t_us)
            return
        if not self._clock(t_us):
            return
        np = _libs()[1]
        scale = float(read.get("scale") or 1.0)
        self.scale = scale
        lines = sorted(read.get("lines") or [], key=lambda ln: ln.get("y", 0))
        live = [t for t in self.tracks if t_us - t.last_us <= TRACK_GAP_US]
        best = None
        for k in range(MAX_SHIFT + 1):
            pairs = []
            for li, ln in enumerate(lines):
                for tr in live:
                    if abs(ln.get("y", 0) - (tr.y - k * LINE_PITCH * scale)) > LINE_Y_TOL * scale:
                        continue
                    if abs(ln.get("icon_x", 0) - tr.icon_x) > ICON_X_TOL * scale:
                        continue
                    if _conflict(tr.killer, ln.get("killer")):
                        continue
                    # Pixel-shape likeness only breaks ties; y, icon x and the
                    # killer reads decide whether a read can be this line.
                    sim = 0.7 * _sig_ncc(np, ln.get("killer_sig"), tr.killer_sig) + 0.3 * _sig_ncc(np, ln.get("victim_sig"), tr.victim_sig)
                    pairs.append((sim, li, tr))
            pairs.sort(key=lambda p: -p[0])
            used_l, used_t, owner = set(), set(), {}
            for sim, li, tr in pairs:
                if li in used_l or id(tr) in used_t:
                    continue
                used_l.add(li)
                used_t.add(id(tr))
                owner[li] = (tr, sim)
            score = sum(1.0 + sim for _tr, sim in owner.values())
            if best is None or score > best[0] + 1e-9:
                best = (score, k, owner)
        _score, shift, owner = best if best is not None else (0.0, 0, {})
        for tr in live:
            tr.y -= shift * LINE_PITCH * scale
        for li, ln in enumerate(lines):
            hit = owner.get(li)
            tr = hit[0] if hit else None
            if tr is None:
                self._next += 1
                tr = _Track(self._next, t_us, t_us, float(ln.get("y", 0)), ln.get("killer_sig"), ln.get("victim_sig"))
                tr.icon_x = float(ln.get("icon_x", 0))
                self.tracks.append(tr)
            tr.last_us = t_us
            tr.y = float(ln.get("y", 0))
            if ln.get("killer_sig") is not None:
                tr.killer_sig = ln.get("killer_sig")
            if ln.get("victim_sig") is not None:
                tr.victim_sig = ln.get("victim_sig")
            tr.frames += 1
            tr.seen.append((int(t_us), float(ln.get("y", 0))))
            k = ln.get("killer") if ln.get("killer") in (SELF, NOT_SELF, UNREADABLE) else UNREADABLE
            v = ln.get("victim") if ln.get("victim") in (SELF, NOT_SELF, UNREADABLE) else UNREADABLE
            tr.killer[k] += 1
            tr.victim[v] += 1
            if k == SELF:
                tr.best_conf = max(tr.best_conf, float(ln.get("killer_conf") or 0.0))

    def _mergeable(self, a: _Track, b: _Track) -> bool:
        if not a.seen or not b.seen:
            return False
        ta = {t for t, _y in a.seen}
        if any(t in ta for t, _y in b.seen):
            return False
        lo = min(a.first_us, b.first_us)
        hi = max(a.last_us, b.last_us)
        if hi - lo > LINE_LIFE_MAX_US:
            return False
        first, second = (a, b) if a.first_us <= b.first_us else (b, a)
        if second.first_us - first.last_us > MAX_FRAME_GAP_US:
            return False
        sc = float(getattr(self, "scale", 1.0) or 1.0)
        low = None
        prev_t = None
        for t, y in sorted(a.seen + b.seen):
            # After the feed was hidden (a death, a killcam) lines slide back
            # in from below, by less than half a line.
            tol = LINE_Y_TOL * sc
            if prev_t is not None and t - prev_t >= REENTRY_GAP_US:
                tol = (LINE_Y_TOL + LINE_PITCH / 2) * sc
            if low is not None and y > low + tol:
                return False
            low = y if low is None else min(low, y)
            prev_t = t
        if abs(a.icon_x - b.icon_x) > ICON_X_TOL * float(getattr(self, "scale", 1.0) or 1.0):
            return False
        va, vb = a.verdict("killer"), b.verdict("killer")
        return va == vb or UNREADABLE in (va, vb)

    def _merged(self) -> list:
        out: list[_Track] = []
        for tr in sorted(self.tracks, key=lambda t: t.first_us):
            host = None
            for cand in reversed(out):
                if self._mergeable(cand, tr):
                    host = cand
                    break
            if host is None:
                out.append(_Track(tr.n, tr.first_us, tr.last_us, tr.y, tr.killer_sig, tr.victim_sig,
                                  dict(tr.killer), dict(tr.victim), tr.frames, tr.best_conf, list(tr.seen), tr.icon_x))
                continue
            host.first_us = min(host.first_us, tr.first_us)
            if tr.last_us >= host.last_us:
                host.last_us, host.y, host.victim_sig = tr.last_us, tr.y, tr.victim_sig
            for side in ("killer", "victim"):
                for key in (SELF, NOT_SELF, UNREADABLE):
                    getattr(host, side)[key] += getattr(tr, side)[key]
            host.frames += tr.frames
            host.best_conf = max(host.best_conf, tr.best_conf)
            host.seen = sorted(host.seen + tr.seen)
        return out

    def summary(self) -> dict:
        merged = self._merged()
        lasting = [t for t in merged if t.frames >= MIN_TRACK_FRAMES]
        kills = [t for t in lasting if t.verdict("killer") == SELF]
        deaths = [t for t in lasting if t.verdict("victim") == SELF]
        unread = [t for t in lasting if t.verdict("killer") == UNREADABLE]
        return {
            "frames": self.frames, "lines": len(lasting), "transient": len(merged) - len(lasting),
            "self_kills": len(kills), "deaths": len(deaths), "unreadable_lines": len(unread),
            "not_self_lines": len([t for t in lasting if t.verdict("killer") == NOT_SELF]),
            "gaps": self.gaps, "unplaced": self.unplaced, "faults": self.faults,
            "first_us": self.first_us, "last_us": self.last_us, "held": self.held,
            "kill_us": [t.first_us for t in kills],
        }

    def coverage(self) -> dict:
        """Own-kill tally for the agreement gate. Unknown unless every line was read."""
        s = self.summary()
        base = {
            "ownership_qualified": True, "deduped": True, "source": "killfeed_gamertag",
            "lines": s["lines"], "unreadable_lines": s["unreadable_lines"], "deaths": s["deaths"],
            "t_us": s["last_us"],
        }
        reason = None
        if s["frames"] == 0:
            reason = "no_feed_frames"
        elif s["unplaced"]:
            reason = "frame_clock_unknown"
        elif s["faults"]:
            reason = "feed_read_fault"
        elif s["gaps"]:
            reason = "feed_gap"
        elif s["unreadable_lines"]:
            reason = "unreadable_lines"
        if reason is not None:
            return {**base, "coverage": "unknown", "kills": None, "reason": reason}
        return {**base, "coverage": "covered", "kills": s["self_kills"], "reason": "every_line_read"}
