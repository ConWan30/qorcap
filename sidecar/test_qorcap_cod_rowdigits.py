"""The CoD end-scoreboard row-digit bank (packs/cod_row_digits_v2.json).

Every digit 0-9 has real samples from Con's private practice boards. The
fixtures are number cells only (no gamertags). A cell that does not pass the
gates (score >= 0.75, margin >= 0.15) stays unread; it is never guessed.
"""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import qorcap_cod_hud as H

HERE = Path(__file__).resolve().parent
PACK = HERE / "packs" / "cod_row_digits_v2.json"
CELLS = HERE / "fixtures" / "cod" / "rowdigits"
END_019 = HERE / "fixtures" / "cod" / "end_173922_019.jpg"
DIGITS = [str(d) for d in range(10)]


def _pack():
    return json.loads(PACK.read_text())


def _cells():
    labels = json.loads((CELLS / "labels.json").read_text())
    out = []
    for name, info in sorted(labels.items()):
        img = cv2.imread(str(CELLS / name))
        assert img is not None, name
        out.append((name, info, img))
    return out


def _read(img, bank=None):
    return H._row_digit_string(img, 0, img.shape[1], 0, img.shape[0], 130, 1.0, bank=bank)


def _bank_without(board):
    vecs, labels = [], []
    for s in _pack()["samples"]:
        if board in s["provenance"].split(":", 1)[1].split()[:4]:
            continue
        v = H._row_feature(H._mask_from_rows(s["mask_rows"]), H._hex_rows(s["gray_rows"]))
        vecs.append(v)
        labels.append(s["label"])
    return np.stack(vecs), labels


def test_every_digit_has_real_samples_and_every_sample_names_its_source():
    pack = _pack()
    real = {}
    for s in pack["samples"]:
        assert s["label"] in DIGITS
        assert s["class"] in ("plain", "owner", "card")
        prov = s["provenance"]
        assert prov.startswith(("real ", "derived "))
        if prov.startswith("derived "):
            # A derived glyph is a real plain-row glyph of the same digit, resampled.
            assert "plain-row sample" in prov and "resampled" in prov
        else:
            real.setdefault(s["label"], set()).add(s["class"])
    assert sorted(real) == DIGITS
    for d in DIGITS:
        assert "plain" in real[d], d
    assert len(pack["boards"]) >= 7


def test_shipped_bank_reads_every_fixture_cell_or_leaves_it_unread():
    cells = _cells()
    assert len(cells) >= 300
    ok = false = 0
    for name, info, img in cells:
        got = _read(img)
        if got == info["value"]:
            ok += 1
        elif got is not None:
            false += 1
            print("FALSE", name, info["value"], got)
    assert false == 0
    assert ok >= 290


def test_leave_one_board_out_has_no_false_reads():
    cells = _cells()
    boards = sorted({info["board"] for _n, info, _i in cells})
    assert len(boards) >= 7
    ok = false = 0
    owner_ok = 0
    for board in boards:
        bank = _bank_without(board)
        for name, info, img in cells:
            if info["board"] != board:
                continue
            got = _read(img, bank)
            if got == info["value"]:
                ok += 1
                owner_ok += info["class"] == "owner"
            elif got is not None:
                false += 1
                print("FALSE", name, info["value"], got)
    assert false == 0
    assert ok >= 255
    assert owner_ok >= 23


def _fade(img, a, b, keep, part):
    out = img.copy()
    bg = np.median(out.reshape(-1, 3), axis=0)
    faded = (bg + (out[:, a:b].astype(np.float32) - bg) * keep).astype(np.uint8)
    hi = out.shape[0] // 2
    rows = {"top": slice(0, hi), "bottom": slice(hi, None), "all": slice(None)}[part]
    out[rows, a:b] = faded[rows]
    return out


def test_fading_one_digit_never_changes_a_value():
    # 194037 row 4 read '13' as '1' when the faint '3' fell under a fixed cut.
    # Fade half of one digit to 20-80% of its contrast, or all of it to 35-80%:
    # the value stays itself or goes unread. It never loses or changes a digit.
    # (A whole digit at 20% contrast is about 12 levels over the row: not covered.)
    checked = 0
    for name, info, img in _cells():
        if len(info["value"]) < 2:
            continue
        pieces = H._row_glyph_pieces(img, 0, img.shape[1], 0, img.shape[0], 130, 1.0)
        if not pieces or len(pieces) != len(info["value"]):
            continue
        xs = [p[0] for p in pieces] + [img.shape[1] + 1]
        for k in range(len(pieces)):
            a, b = max(0, xs[k] - 1), xs[k + 1] - 1
            for keep, parts in ((0.2, ("top", "bottom")), (0.35, ("top", "bottom", "all")),
                                (0.5, ("top", "bottom", "all")), (0.65, ("top", "bottom", "all")),
                                (0.8, ("top", "bottom", "all"))):
                for part in parts:
                    got = _read(_fade(img, a, b, keep, part))
                    assert got in (info["value"], None), (name, k, keep, part, got)
        checked += 1
    assert checked >= 100


def test_194037_owner_kills_cell_reads_13_not_1():
    img = cv2.imread(str(CELLS / "194037_L4_kills.png"))
    assert img is not None
    assert _read(img) == "13"
    assert _read(img, _bank_without("194037")) in ("13", None)


def test_old_boards_still_read_the_owner_row():
    rd = H.read_owner_row(cv2.imread(str(END_019)))
    assert rd.ok and (rd.kills, rd.deaths, rd.confirms) == (13, 13, 7)
    # f_0846 (073204) is not committed (gamertags); its owner cells are.
    for col, want in (("kills", "9"), ("deaths", "21"), ("assists", "2")):
        img = cv2.imread(str(CELLS / f"073204_L4_{col}.png"))
        assert _read(img) == want, col


def test_every_value_0_to_100_paints_from_real_glyphs():
    # Any value, not only values seen on a board: compose real plain glyphs side by side.
    pack = _pack()
    glyph = {}
    for s in pack["samples"]:
        if s["class"] == "plain" and s["provenance"].startswith("real ") and s["label"] not in glyph:
            glyph[s["label"]] = H._hex_rows(s["gray_rows"])
    assert sorted(glyph) == DIGITS
    for n in list(range(0, 101)) + [182, 999]:
        text = str(n)
        parts = [glyph[c] for c in text]
        h = max(p.shape[0] for p in parts)
        bg = int(np.median(np.concatenate([p[0] for p in parts])))
        row = [np.full((h + 6, 4), bg, np.uint8)]
        for p in parts:
            col = np.full((h + 6, p.shape[1]), bg, np.uint8)
            col[3:3 + p.shape[0]] = p
            row += [col, np.full((h + 6, 2), bg, np.uint8)]
        img = cv2.cvtColor(np.hstack(row + [np.full((h + 6, 4), bg, np.uint8)]), cv2.COLOR_GRAY2BGR)
        got = _read(img)
        assert got in (text, None), (text, got)
