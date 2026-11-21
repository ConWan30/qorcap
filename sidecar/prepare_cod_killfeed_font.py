#!/usr/bin/env python3
"""Build the CoD killfeed glyph bank from a monospace font. Text output only.

    python3 sidecar/prepare_cod_killfeed_font.py --font /path/RobotoMono-VariableFont_wght.ttf

The killfeed draws gamertags in a fixed-pitch font. The game's own font file is
not shipped with QorCap, so this bank renders every glyph from Roboto Mono
(Apache-2.0), the open monospace font that fit other players' killfeed names
best (see FIT_NOTE). Every character of Qortrola30, and every other letter and
digit, comes from that font. None is cut from a frame of Con's own name.

Writes sidecar/packs/cod_killfeed_font.json. Needs Pillow and the font file;
runtime reads only the JSON and does not need either.
"""
from __future__ import annotations

import argparse
import json
import string
from pathlib import Path

OUT = Path(__file__).resolve().parent / "packs" / "cod_killfeed_font.json"
CHARSET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "_[]-."
# Fitted on 80 killer names of other players (BDR UptightH, Luna11, XXXskittlesXXX,
# Alley Beejs, [TLDS]Trogdor, Limit_Jaayyy, Hard Rs, TheCrackMonster, [318]3I8Bigivory)
# from one private 720p Kill Confirmed practice video, over 40+ open monospace fonts,
# weights and sizes. Qortrola30 lines were not used to pick the font, the weight,
# the size, or the pitch.
FONT_NAME = "Roboto Mono"
FONT_LICENSE = "Apache-2.0"
WEIGHT = 700
SIZE_PX_720 = 13.0
PITCH_PX_720 = 7.0
CELL_H_720 = 14
SUPERSAMPLE = 4
FIT_NOTE = (
    "Font chosen by word-level fit to other players' killfeed names on a private 720p "
    "practice video; Qortrola30 was not used for the fit. Real-match recall is measured "
    "separately and reported as such."
)


def build(font_path: Path) -> dict:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    ss = SUPERSAMPLE * 2
    font = ImageFont.truetype(str(font_path), int(round(SIZE_PX_720 * ss)))
    try:
        font.set_variation_by_axes([WEIGHT])
    except Exception:
        pass
    adv = font.getlength("0")
    cap_top = font.getbbox("H")[1]
    out_w = int(round(PITCH_PX_720 * SUPERSAMPLE))
    out_h = CELL_H_720 * SUPERSAMPLE
    glyphs = []
    for ch in CHARSET:
        img = Image.new("L", (int(adv) + 4 * ss, int(SIZE_PX_720 * ss * 2)), 0)
        ImageDraw.Draw(img).text((2 * ss, 0), ch, font=font, fill=255)
        arr = np.asarray(img, dtype=np.float32)
        y0 = max(0, cap_top - ss)
        cell = arr[y0:y0 + CELL_H_720 * ss, 2 * ss:2 * ss + int(round(adv))]
        small = Image.fromarray(cell.astype("uint8")).resize((out_w, out_h), Image.BOX)
        q = np.clip(np.rint(np.asarray(small, dtype=np.float32) / 255.0 * 9.0), 0, 9).astype(int)
        glyphs.append({"char": ch, "rows": ["".join(str(int(v)) for v in row) for row in q]})
    return {
        "kind": "cod_killfeed_font_v1",
        "font": FONT_NAME,
        "license": FONT_LICENSE,
        "weight": WEIGHT,
        "size_px_720": SIZE_PX_720,
        "pitch_px_720": PITCH_PX_720,
        "cell_h_720": CELL_H_720,
        "supersample": SUPERSAMPLE,
        "provenance": "rendered from font",
        "note": FIT_NOTE,
        "glyphs": glyphs,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    data = build(args.font)
    args.out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(data['glyphs'])} glyphs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
