"""Build packs/madden27_psn_banner_v1.npz for sidecar/qorcap_psn.py (dev tool, not shipped in the zip).

    python tools/make_psn_pack.py --logo logo.png --font BarlowCondensed-ExtraBold.ttf \
        --labelled DIR[:PREFIX]=LEFTNAME,RIGHTNAME [--labelled ...] [--exclude-prefix 20261017-021003] [--out PACK]

DIR holds 1280x720 frames or 1280x148 name strips (qorcap_psn.STRIP_ROWS) of ONLINE games whose two PSN names are
known (left banner first). Frames stay local: only the averaged glyph bitmaps (one per character, ~76x15 px) go into the
pack, never footage. ``seen`` glyphs come from those frames; every other PSN character is rendered from --font
(OFL look-alike) as ``font``. Needs numpy, opencv and Pillow.
"""
from __future__ import annotations

import argparse
import glob
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import qorcap_psn as P

CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
CAP_TOP, BASE = 16, 47   # canonical rows of a capital's top and of the baseline (measured on 'E' in real footage)


def labelled(v, name):
    sp = P.text_span(v)
    if sp is None:
        return None
    segs = P.segments(v, sp)
    while len(segs) < len(name):
        i = max(range(len(segs)), key=lambda j: segs[j][1] - segs[j][0])
        a, b = segs[i]
        if b - a < 18:
            return None
        col = (v[P.TEXT_ROWS[0]:P.TEXT_ROWS[1], a:b] > 0.45).sum(0)
        lo, hi = (b - a) // 4, 3 * (b - a) // 4
        c = a + lo + int(np.argmin(col[lo:hi]))
        segs[i:i + 1] = [(a, c), (c, b)]
    if len(segs) != len(name):
        return None
    return [(ch, v[:, a:b]) for ch, (a, b) in zip(name, segs)]


def render(font_path, ch, blur=1.6):
    from PIL import Image, ImageDraw, ImageFont
    if ch == "_":
        g = np.zeros((P.BAND_H, 13), np.float32)
        g[48:53, 1:12] = 1
        return cv2.GaussianBlur(g, (0, 0), 1.0)
    big = 4
    f = ImageFont.truetype(font_path, 200)
    cap = -f.getbbox("E", anchor="ls")[1]
    size = int(200 * (BASE - CAP_TOP) * big / cap)
    f = ImageFont.truetype(font_path, size)
    im = Image.new("L", (size * 2, P.BAND_H * big), 0)
    ImageDraw.Draw(im).text((size // 4, BASE * big), ch, fill=255, font=f, anchor="ls")
    a = np.asarray(im, np.float32) / 255
    cols = np.where(a.max(0) > 0.3)[0]
    a = a[:, cols[0]:cols[-1] + 1]
    a = cv2.resize(a, (max(2, round(a.shape[1] / big)), P.BAND_H), interpolation=cv2.INTER_AREA)
    return np.clip(cv2.GaussianBlur(a, (0, 0), blur) * 1.15, 0, 1)


def harvest(specs, logo, exclude=()):
    pack = {"logo": logo, "glyphs": {"x": np.zeros((P.BAND_H, 10), np.float32), "y": np.ones((P.BAND_H, 10), np.float32)},
            "source": {}, "version": "bootstrap"}
    by = defaultdict(list)
    for spec in specs:
        d, names = spec.split("=", 1)
        d, _, prefix = d.partition(":")
        left, right = names.split(",")
        for f in sorted(glob.glob(str(Path(d) / f"{prefix}*.png"))):
            if any(Path(f).name.startswith(x) for x in exclude):
                continue
            img = P.to_canvas(cv2.imread(f))
            for b in P.locate(img, pack):
                L = labelled(P.ink(P.name_band(img, b)), left if b["side"] == "left" else right)
                for ch, g in L or []:
                    by[ch].append(g)
    return by


def build(by, font, logo, version):
    chars, glyphs, widths, source = [], [], [], []
    for ch in CHARSET:
        if by.get(ch):
            w = int(np.median([g.shape[1] for g in by[ch]]))
            g = np.mean([cv2.resize(x, (w, P.BAND_H), interpolation=cv2.INTER_AREA) for x in by[ch]], 0)
            src = "seen"
        else:
            g = render(font, ch)
            src = "font"
        chars.append(ch)
        glyphs.append(g)
        widths.append(g.shape[1])
        source.append(src)
    wmax = max(widths)
    arr = np.zeros((len(chars), P.BAND_H, wmax), np.float16)
    for i, g in enumerate(glyphs):
        arr[i, :, : g.shape[1]] = g
    return {"chars": np.array(chars), "glyphs": arr, "widths": np.array(widths, np.int16), "source": np.array(source),
                "logo": logo, "version": np.array(version)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--logo", required=True)
    ap.add_argument("--font", required=True)
    ap.add_argument("--labelled", action="append", required=True)
    ap.add_argument("--exclude-prefix", action="append", default=[])
    ap.add_argument("--version", default="madden27_psn_banner_v1")
    ap.add_argument("--out", default=str(P.PACK))
    a = ap.parse_args(argv)
    logo = cv2.imread(a.logo, 0)
    by = harvest(a.labelled, logo, a.exclude_prefix)
    np.savez_compressed(a.out, **build(by, a.font, logo, a.version))
    print(a.out, {c: len(v) for c, v in sorted(by.items())})


if __name__ == "__main__":
    main()
