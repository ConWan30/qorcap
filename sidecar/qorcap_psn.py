"""On-screen PSN names (v0): read the PSN Online IDs Madden 27 shows in an ONLINE head-to-head game.

    qorcap psn-names [BUNDLE] [--samples 60] [--video FILE]       (post-match only)
    python sidecar/qorcap_psn.py ...

Sony and EA offer no public login or API for this, and unofficial PSN APIs are off-limits, so QorCap only reads what
Madden draws on screen. Where the names appear (found on match-20261017-021003, Con's own account vs an online opponent):

* the player banners at the top of the screen in an online game (pre-snap / play call, also behind the pause menu):
  left banner at ~(215, 8) and right banner at ~(974, 8) on a 1280x720 frame, text ~8 px high;
* the player cards at the bottom of the pause menu (y ~587) and the end-of-game menu (y ~607), 1.475x larger.

Each banner starts with the PlayStation logo, then the headset icon, then the name. The logo is found by template match
(both banners of a surface must be present, which drops look-alike play-card icons); the name is cut at a fixed offset
from the logo, scaled to one canonical size, turned into a white-text mask, split into glyphs and every glyph is
matched against ``packs/madden27_psn_banner_v1.npz``. Glyphs seen in real footage are in the pack as ``seen``; the
other PSN characters are rendered from a look-alike font (Barlow Condensed ExtraBold, OFL) as ``font`` and are
much less reliable. A name is read from many frames; each character is the vote across the sightings that agree on
the length. A character that is not clearly voted, or only matched a ``font`` glyph weakly, becomes ``?``. A name with
any ``?`` gets ``handle: null`` (its ``pattern`` keeps the readable characters). Fail closed: nothing is guessed.

Side: the left banner is the AWAY team, the right banner HOME (the same order as Madden's scoreboard, QOR 22 - EVE 41).

This is ON-SCREEN EVIDENCE, NOT ACCOUNT PROOF: a name drawn by the game in a recorded video is not a signed-in
identity, and it does not show who held the controller. Look-alike characters (0/O/o/Q, 1/l/I, g/y ...) cannot be
told apart reliably at this size; QorTime compares with ``qor_receipt.h2h.same_name``.

Writes ``psn_names.json`` (schema ``qorcap-psn-names/v0``) in the bundle; ``finalize`` copies its receipt-safe part
into ``receipt.match.observed_handles`` when the file is bound to the same video chain. Never runs while QorCap is
recording (exit 3), and sends nothing anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCHEMA = "qorcap-psn-names/v0"
READER = "qorcap-psn/0.1.0"
HERE = Path(__file__).resolve().parent
PACK = HERE / "packs" / "madden27_psn_banner_v1.npz"
CLAIMS = ("On-screen evidence, not account proof: the PSN names Madden showed in this recorded video, read by QorCap "
          "from the player banners. Sony and EA offer no public login or API, and a name on screen does not show who "
          "held the controller.")
SIDES = {"left": "away", "right": "home"}

W, H = 1280, 720
K = 4                                   # canonical upscale: 1 banner pixel at scale 1.0 = K canonical pixels
BAND_H = 19 * K                         # canonical text band height
TOP_ROW, CORE, BOTTOM_ROW = 12, (22, 46), 60
TEXT_ROWS = (3 * K, 14 * K)
X_OFF, Y_OFF, WIDTH = 26, 1.5, 110      # banner px from the PS logo's top-left to the name band
LOGO_MIN = 0.88
# (surface, side) -> search box (y0, y1, x0, x1) on 1280x720, and the banner scale on that surface
REGIONS = {("top", "left"): (0, 45, 150, 420), ("top", "right"): (0, 45, 880, 1150),
           ("menu", "left"): (560, 650, 380, 620), ("menu", "right"): (560, 650, 820, 1050)}
SCALE = {"top": 1.0, "menu": 1.475}
# 1280x148 strip that holds every name surface: rows 0-47 and 555-654 of the 720p frame
STRIP_ROWS = ((0, 48), (555, 655))
MIN_SEG = 4
CHAR_MIN = 0.80          # glyph match (NCC) needed to count a vote
FONT_MIN = 0.88          # a glyph that only matched a rendered-font template needs more
VOTE_MIN = 0.6           # share of the agreeing sightings that must vote for the same character
MIN_SIGHTINGS = 3


class Refuse(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ pack
_PACK: dict | None = None


def load_pack(path: Path = PACK) -> dict:
    global _PACK
    if _PACK is not None and path == PACK:
        return _PACK
    import numpy as np
    z = np.load(path, allow_pickle=False)
    chars = [str(c) for c in z["chars"]]
    glyphs = {}
    for i, c in enumerate(chars):
        glyphs[c] = z["glyphs"][i][:, : int(z["widths"][i])].astype("float32")
    pack = {"logo": z["logo"].astype("uint8"), "glyphs": glyphs,
            "source": {c: str(s) for c, s in zip(chars, z["source"])}, "version": str(z["version"])}
    if path == PACK:
        _PACK = pack
    return pack


# ------------------------------------------------------------------ geometry
def to_canvas(img):
    """Any frame -> 1280x720 BGR. A 1280x148 name strip (see STRIP_ROWS) is placed back at its rows."""
    import cv2
    import numpy as np
    if img.shape[0] == sum(b - a for a, b in STRIP_ROWS) and img.shape[1] == W:
        c = np.zeros((H, W, 3), np.uint8)
        o = 0
        for a, b in STRIP_ROWS:
            c[a:b] = img[o:o + b - a]
            o += b - a
        return c
    if img.shape[:2] != (H, W):
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
    return img


def locate(img, pack: dict | None = None) -> list[dict]:
    """PS-logo anchors of the name banners; a surface counts only when BOTH of its banners are found."""
    import cv2
    pack = pack or load_pack()
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    found = []
    for (surf, side), (y0, y1, x0, x1) in REGIONS.items():
        s = SCALE[surf]
        t = pack["logo"] if s == 1.0 else cv2.resize(pack["logo"], None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        r = cv2.matchTemplate(g[y0:y1, x0:x1], t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx >= LOGO_MIN:
            found.append({"surface": surf, "side": side, "logo": round(float(mx), 3), "x": x0 + loc[0], "y": y0 + loc[1], "s": s})
    out = []
    for surf in SCALE:
        p = [b for b in found if b["surface"] == surf]
        if len(p) == 2:
            out += p
    return out


def name_band(img, b: dict):
    import cv2
    import numpy as np
    s = b["s"]
    x0, y0 = b["x"] + X_OFF * s, b["y"] - Y_OFF * s
    m = np.float32([[K / s, 0, -x0 * K / s], [0, K / s, -y0 * K / s]])
    return cv2.warpAffine(img, m, (WIDTH * K, BAND_H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def ink(band):
    """White, low-saturation text -> [0,1] mask; drops the banner art (lightning, icons) that is not glyph-shaped."""
    import cv2
    import numpy as np
    f = band.astype(np.float32)
    mn, mx = f.min(2), f.max(2)
    v = np.clip((mn - 120) / 100, 0, 1) * np.clip(1 - (mx - mn - 25) / 40, 0, 1)
    v[:TOP_ROW] = 0
    v[BOTTOM_ROW:] = 0
    n, lab, st, _ = cv2.connectedComponentsWithStats((v > 0.45).astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        core = y <= CORE[1] and y + h >= CORE[0] + 4
        dot = y + h <= CORE[0] + 6 and w <= 4 * K and h <= 4 * K and a >= 6
        under = y >= CORE[1] - 4 and w >= 1.6 * h and w >= 8 and h <= 3 * K
        keep[i] = core or dot or under
    grow = cv2.dilate(keep[lab].astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    out = v * grow
    # an underscore is often drawn dimmer than the letters: look for flat strokes at a lower threshold
    low = np.zeros_like(v, np.uint8)
    low[CORE[1] - 2:BOTTOM_ROW - 2] = (v[CORE[1] - 2:BOTTOM_ROW - 2] > 0.25)
    n, lab, st, _ = cv2.connectedComponentsWithStats(low, connectivity=8)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if w >= 8 and w >= 1.6 * h and h <= 3 * K and not grow[y:y + h, x:x + w].any():
            sel = lab == i
            out[sel] = np.maximum(out[sel], 0.6)
    return out


def text_span(v, gap: int = 10):
    import numpy as np
    m = v > 0.45
    on = np.where(m[TEXT_ROWS[0]:TEXT_ROWS[1]].sum(0) >= 2)[0]
    i = 0
    while i < len(on):  # skip the tail of the headset/mic icon: thin horizontal leading strokes
        rows = np.where(m[:, on[i]])[0]
        if len(rows) and rows.max() - rows.min() >= 5 * K:
            break
        i += 1
    if i >= len(on):
        return None
    on = on[i:]
    last = on[0]
    for x in on[1:]:
        if x - last > gap:
            break
        last = x
    return int(on[0]), int(last) + 1


def continues_after(v, end: int, reach: int = 12 * K) -> bool:
    """True when glyph-like ink (tall strokes in the text rows) resumes shortly after the span: the name was cut
    (occlusion, a dropped letter); the read is then incomplete, never a shorter name."""
    import numpy as np
    m = v[TEXT_ROWS[0]:TEXT_ROWS[1], end:end + reach] > 0.45
    tall = 0
    for x in range(m.shape[1]):
        rows = np.where(m[:, x])[0]
        tall += len(rows) > 0 and rows.max() - rows.min() >= 5 * K
    return tall >= 3


def segments(v, span) -> list[tuple[int, int]]:
    m = v[:, span[0]:span[1]] > 0.45
    col = m[TEXT_ROWS[0]:TEXT_ROWS[1]].sum(0) >= 2
    segs, s = [], None
    for i, c in enumerate(col):
        if c and s is None:
            s = i
        if not c and s is not None:
            segs.append((span[0] + s, span[0] + i))
            s = None
    if s is not None:
        segs.append((span[0] + s, span[1]))
    return [(a, b) for a, b in segs if b - a >= MIN_SEG]


# ------------------------------------------------------------------ glyphs
def _ncc(a, b) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = float((a * a).sum() * (b * b).sum()) ** 0.5
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def _fit(g, t) -> float:
    import cv2
    h, w = t.shape
    if not (0.6 * w <= g.shape[1] <= 1.5 * w):
        return -1.0
    gg = cv2.resize(g, (w, g.shape[0]), interpolation=cv2.INTER_AREA)
    return max(_ncc(gg[max(0, dy):h + min(0, dy)], t[max(0, -dy):h - max(0, dy)]) for dy in (-2, 0, 2))


FONT_PENALTY = 0.03  # a rendered look-alike glyph must beat a glyph seen in real footage by this much


def classify(g, pack: dict) -> tuple[str, float, float]:
    src = pack["source"]
    sc = sorted(((_fit(g, t) - (FONT_PENALTY if src.get(c) != "seen" else 0.0), c) for c, t in pack["glyphs"].items()),
                reverse=True)
    return sc[0][1], sc[0][0], sc[0][0] - sc[1][0]


def read_band(v, pack: dict) -> list[tuple[str, float, float]]:
    """Glyphs of one name band: [(char, score, margin)]. Merged blobs (touching letters) are split by best fit."""
    sp = text_span(v)
    if sp is None:
        return []
    if continues_after(v, sp[1]):
        return [("?", 0.0, 0.0)]  # incomplete: consensus sees a length that no clean sighting agrees with
    widths = [t.shape[1] for t in pack["glyphs"].values()]
    wmin = min(widths)
    out = []
    for a, b in segments(v, sp):
        single = classify(v[:, a:b], pack)
        if b - a < 2 * wmin + 4:
            out.append(single)
            continue
        best = None
        for n in ([2, 3] if b - a > 3 * wmin + 4 else [2]):
            r = _split(v, a, b, n, pack, wmin)
            if r and (best is None or r[0] > best[0]):
                best = r
        if best is None or single[1] + 0.02 >= best[0]:
            out.append(single)
        else:
            out.extend(best[1])
    return out


def _split(v, a, b, n, pack, wmin):
    lo = int(0.6 * wmin)
    if n == 1:
        r = classify(v[:, a:b], pack)
        return r[1], [r]
    best = None
    for cut in range(a + lo, b - lo * (n - 1) + 1, 2):
        r = classify(v[:, a:cut], pack)
        rest = _split(v, cut, b, n - 1, pack, wmin)
        if rest is None:
            continue
        tot = (r[1] + rest[0] * (n - 1)) / n
        if best is None or tot > best[0]:
            best = (tot, [r] + rest[1])
    return best


def read_frame(img, pack: dict | None = None) -> list[dict]:
    """Every name banner visible in one frame: [{surface, side, text, glyphs:[[c, score, margin]]}]."""
    pack = pack or load_pack()
    img = to_canvas(img)
    out = []
    for b in locate(img, pack):
        gl = read_band(ink(name_band(img, b)), pack)
        if gl:
            out.append({"surface": b["surface"], "side": b["side"], "logo": b["logo"],
                        "text": "".join(c for c, _, _ in gl), "glyphs": [[c, round(s, 3), round(m, 3)] for c, s, m in gl]})
    return out


# ------------------------------------------------------------------ consensus
def consensus(reads: list[dict], pack: dict | None = None) -> dict:
    """Per side: vote every character across the sightings that agree on the name length. Fail closed with '?'."""
    pack = pack or load_pack()
    if len(reads) < MIN_SIGHTINGS:
        return {"handle": None, "pattern": None, "confidence_permille": 0, "sightings": len(reads), "agree": 0,
                "why": f"name banner seen in {len(reads)} sampled frame(s), need {MIN_SIGHTINGS}"}
    n = Counter(len(r["glyphs"]) for r in reads).most_common(1)[0][0]
    agree = [r for r in reads if len(r["glyphs"]) == n]
    total = len(agree)
    pattern, confs, font_chars = [], [], 0
    for i in range(n):
        votes, scores = Counter(), {}
        for r in agree:
            c, s, _ = r["glyphs"][i]
            if s < CHAR_MIN:
                continue
            votes[c] += 1
            scores.setdefault(c, []).append(s)
        if not votes:
            pattern.append("?")
            confs.append(0.0)
            continue
        c, v = votes.most_common(1)[0]
        share = v / total
        mean = sum(scores[c]) / len(scores[c])
        src = pack["source"].get(c, "font")
        ok = share >= VOTE_MIN and (src == "seen" or mean >= FONT_MIN)
        font_chars += src != "seen"
        pattern.append(c if ok else "?")
        confs.append(share * min(1.0, mean / 0.9) if ok else 0.0)
    pat = "".join(pattern)
    conf = round(1000 * (sum(confs) / len(confs) if confs else 0))
    doc = {"handle": pat if "?" not in pat and len(agree) >= MIN_SIGHTINGS else None, "pattern": pat,
           "confidence_permille": conf, "sightings": len(reads), "agree": len(agree),
           "surfaces": sorted({r["surface"] for r in agree}), "font_glyphs": font_chars}
    if doc["handle"] is None:
        doc["why"] = ("some characters could not be read reliably" if "?" in pat else
                      f"only {len(agree)} sightings agree on the length")
    return doc


# ------------------------------------------------------------------ video
def _videos(bundle: Path) -> list[tuple[str, float]]:
    """(file, seconds) for every video part, from manifest.jsonl (frame counter / 30 fps)."""
    files: dict[str, float] = {}
    mp = bundle / "manifest.jsonl"
    if mp.exists():
        for line in mp.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") == "segment" and isinstance(m.get("frames"), (int, float)):
                f = m.get("file") or "video.mp4"
                files[f] = max(files.get(f, 0.0), m["frames"] / 30.0)
    if not files and (bundle / "video.mp4").exists():
        files["video.mp4"] = 0.0
    return sorted(files.items())


def _probe(ffmpeg: str, path: Path) -> float:
    probe = str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe")))
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60, check=False).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def grab_strip(ffmpeg: str, path: Path, t: float):
    """One 1280x148 name strip at t seconds (fast seek, one frame, nothing written to disk)."""
    import numpy as np
    (a0, a1), (b0, b1) = STRIP_ROWS
    fc = (f"[0:v]scale={W}:{H},split[a][b];[a]crop={W}:{a1 - a0}:0:{a0}[t];[b]crop={W}:{b1 - b0}:0:{b0}[u];"
          "[t][u]vstack")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-threads", "1", "-ss", f"{t:.2f}", "-i", str(path),
           "-filter_complex", fc, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=60, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = (a1 - a0) + (b1 - b0)
    if len(raw) != W * rows * 3:
        return None
    return np.frombuffer(raw, np.uint8).reshape(rows, W, 3)


def _sha256_file(p: Path) -> str | None:
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def binding(bundle: Path) -> dict:
    """What psn_names.json is bound to: the bundle's video hash-chain head and the manifest that lists every segment."""
    b = {}
    try:
        b["chain_head"] = json.loads((bundle / "bundle.json").read_text(encoding="utf-8")).get("chain_head")
    except (OSError, ValueError):
        b["chain_head"] = None
    b["manifest_sha256"] = _sha256_file(bundle / "manifest.jsonl")
    return b


def read_bundle(bundle: Path, *, ffmpeg: str = "ffmpeg", samples: int = 60, video: Path | None = None,
                frames: list | None = None, pack: dict | None = None) -> dict:
    """Sample ``samples`` frames across the recording, read every banner, vote per side. ``frames`` = [(label, img)]
    replaces the video (tests)."""
    from qorcap_highlights import recorder_running
    if recorder_running(bundle):
        raise Refuse(3, "QorCap is still recording this match (fresh .qorcap-run/recording.lock); run this after the stop")
    pack = pack or load_pack()
    per_side: dict[str, list[dict]] = {"left": [], "right": []}
    seen_at: dict[str, list] = {"left": [], "right": []}
    n_frames = 0
    if frames is None:
        vids = [(str(video), 0.0)] if video else _videos(bundle)
        if not vids:
            raise Refuse(4, f"no video in {bundle}")
        frames = _sample(bundle, vids, ffmpeg, samples)
    for label, img in frames:
        if img is None:
            continue
        n_frames += 1
        for r in read_frame(img, pack):
            per_side[r["side"]].append(r)
            seen_at[r["side"]].append(label)
    sides = {}
    for side, reads in per_side.items():
        c = consensus(reads, pack)
        c["team"] = SIDES[side]
        c["reads"] = Counter(r["text"] for r in reads).most_common(5)
        c["seen_at"] = seen_at[side][:10]
        sides[side] = c
    return {"schema": SCHEMA, "reader": READER, "pack": pack["version"], "claims": CLAIMS,
            "frames_sampled": n_frames, "sides": sides, "bound_to": binding(bundle)}


def _sample(bundle: Path, vids, ffmpeg: str, samples: int):
    total = []
    for f, secs in vids:
        p = Path(f) if Path(f).is_absolute() else bundle / f
        if not p.exists():
            continue
        secs = secs or _probe(ffmpeg, p)
        if secs > 5:
            total.append((p, secs))
    dur = sum(s for _, s in total)
    for p, secs in total:
        k = max(2, round(samples * secs / dur)) if dur else 2
        for i in range(k):
            t = secs * (0.01 + 0.98 * i / max(1, k - 1))
            yield f"{p.name}@{t:.1f}s", grab_strip(ffmpeg, p, t)


def observed_handles(doc: dict) -> dict:
    """Receipt-safe part (strings, ints, null only: the receipt is JCS-signed and allows no floats)."""
    sides = {}
    for side, c in (doc.get("sides") or {}).items():
        sides[SIDES[side]] = {"handle": c.get("handle"), "pattern": c.get("pattern"),
                              "confidence_permille": int(c.get("confidence_permille") or 0),
                              "sightings": int(c.get("sightings") or 0), "agree": int(c.get("agree") or 0),
                              "screen_side": side}
    return {"source": "on_screen", "platform": "psn", "reader": str(doc.get("reader")), "pack": str(doc.get("pack")),
            "claims": CLAIMS, **sides}


def load_bound(bundle: Path) -> dict | None:
    """psn_names.json if it is bound to this bundle's video chain, else None (a stale or copied file is ignored)."""
    p = bundle / "psn_names.json"
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if doc.get("schema") != SCHEMA or doc.get("bound_to") != binding(bundle):
        return None
    return doc


def summary(doc: dict) -> list[str]:
    out = []
    for side in ("left", "right"):
        c = doc["sides"][side]
        name = c["handle"] or (f"unreadable ({c['pattern']})" if c.get("pattern") else "not seen")
        out.append(f"  {c['team'].upper():4} ({side:5}): {name}   confidence {c['confidence_permille'] / 10:.0f}%  "
                   f"[{c['agree']}/{c['sightings']} sightings]")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read the PSN names Madden shows on screen (post-match, on-screen evidence)")
    ap.add_argument("bundle", nargs="?", help="match bundle folder (default: newest in Documents/QorCap Matches)")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--video", help="read this video instead of the bundle's")
    ap.add_argument("--samples", type=int, default=60, help="frames to sample across the match (default 60)")
    ap.add_argument("--out", help="write here instead of BUNDLE/psn_names.json")
    ap.add_argument("--qorcap", help=argparse.SUPPRESS)
    ap.add_argument("--qortime", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    bundle = Path(a.bundle) if a.bundle else None
    if bundle is None:
        root = Path.home() / "Documents" / "QorCap Matches"
        cands = sorted((p for p in root.glob("match-*") if p.is_dir()), key=lambda p: p.stat().st_mtime) if root.exists() else []
        bundle = cands[-1] if cands else None
    if bundle is None or not bundle.exists():
        say("No match bundle found. Pass the bundle folder: qorcap psn-names <folder>")
        return 2
    import shutil
    ff = a.ffmpeg or next((str(c) for c in [HERE.parent / "ffmpeg.exe", HERE.parent / "ffmpeg"] if c.exists()), None) \
        or shutil.which("ffmpeg") or "ffmpeg"
    try:
        doc = read_bundle(bundle, ffmpeg=ff, samples=a.samples, video=Path(a.video) if a.video else None)
    except Refuse as e:
        say(str(e))
        return e.code
    out = Path(a.out) if a.out else bundle / "psn_names.json"
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    say("On-screen PSN names (on-screen evidence, not account proof):")
    for line in summary(doc):
        say(line)
    say(f"Wrote {out}. 'Finalize Last Match' adds them to the signed receipt as observed handles.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
