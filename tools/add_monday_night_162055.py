"""Append Monday Night templates cut from match-20261017-162055.

The v1 pack was averaged from one other recording (strip.mp4). Quarter words and some clock and away-score
digits on 162055 are thicker, so the correct label leads and still falls under the gate. This adds glyphs
from the labeled frames whose thin-pack first choice is already that label. A glyph the thin pack ranks as
a different digit is not stored.

Ids look like ``162055:m12:clock:6:f_0751750:2``. The minute prefix is how a test holds the template out:
``drop_src="162055:m12:"`` skips every glyph cut from minute 12, including the frame under test.
Near-duplicate crops (NCC >= 0.93 against a glyph of the same label already kept) are not stored twice.

The labels are an offline agreement between an advisory vision pass and a human check. They are used only
while building and testing this pack. ``MondayNightReader.read`` never opens them, and nothing here is a
live lock.

    python tools/add_monday_night_162055.py

Idempotent: existing ``162055:`` rows are replaced. Original rows stay ``pack:v1``.
Aborts without replacing the pack if a labeled frame would be read as a different value, including when
its own minute is held out, or if the three shipped Monday Night fixtures change.
"""
from __future__ import annotations

import csv
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))
import qorcap_mnp as M  # noqa: E402

FX = ROOT / "sidecar" / "fixtures" / "mnp162055"
LABELS = FX / "labeled_candidates.csv"
FRAMES = FX / "frames"
DIVERSE = 0.93  # same-label crops at least this similar are one template
GROUPS = ("score", "clock", "quarter")
QWORD = {1: "1ST", 2: "2ND", 3: "3RD", 4: "4TH"}
FIXTURES = {
    "m27_mnp_q1.jpg": (0, 0, 1, "4:07", False),
    "m27_mnp_q2.jpg": (14, 13, 2, "2:27", False),
    "m27_mnp_q4_final.jpg": (34, 29, 4, "0:00", True),
}


def minute_of(video_time: str) -> int:
    return int(video_time.split(":")[0])


def digits_of(clock: str) -> str:
    return "".join(ch for ch in clock if ch.isdigit())


def load_rows():
    rows = list(csv.DictReader(LABELS.open()))
    by = defaultdict(dict)
    meta = {}
    for r in rows:
        by[r["frame_path"]][r["field"]] = r["value"]
        meta[r["frame_path"]] = r
    return meta, by


def load_image(stem: str):
    import cv2
    path = FRAMES / f"{stem}.jpg"
    im = cv2.imread(str(path))
    if im is None:
        raise SystemExit(f"unreadable {path}")
    return im


def band(im):
    hsv = M.bar_hsv(im)
    if hsv is None:
        raise SystemExit("frame has no bar")
    return hsv[..., 2]


def clock_glyphs(v):
    thr = M.clock_thr(v)
    gs = M.components(v, *M.CLOCK_X, M.CLOCK_H, thr=thr, max_w=M.CLOCK_MAX_W)
    if any(g is None for g in gs) or not 3 <= len(gs) <= 4:
        return None
    return gs


def argmax(labels, vecs, mask, size):
    vec = M.norm_vec(mask, size)
    if vec is None:
        return None, None
    scores = vecs @ vec
    best = {}
    for lab, sc in zip(labels, scores):
        best[str(lab)] = max(best.get(str(lab), -9.0), float(sc))
    order = sorted(best.items(), key=lambda kv: -kv[1])
    return order[0][0], vec


def load_base(path: Path):
    d = np.load(str(path))
    groups = {}
    for g in GROUPS:
        labels = [str(x) for x in d[f"{g}_labels"]]
        vecs = np.array(d[f"{g}_vecs"], dtype=np.float32, copy=True)
        key = f"{g}_src"
        src = [str(x) for x in d[key]] if key in d.files else ["pack:v1"] * len(labels)
        keep = [i for i, s in enumerate(src) if not s.startswith("162055:")]
        src = ["pack:v1" if not src[i] else src[i] for i in keep]
        groups[g] = ([labels[i] for i in keep], vecs[keep], src)
    source = {str(k): str(v) for k, v in zip(d["source_keys"], d["source_vals"]) if "162055" not in str(k)}
    return groups, source


def diversify(items, thr=DIVERSE):
    """items are (minute, label, vec, src_id). Keep a glyph only when it is not a near-copy of one already kept."""
    kept = []
    for it in items:
        if any(it[1] == k[1] and float(it[2] @ k[2]) >= thr for k in kept):
            continue
        kept.append(it)
    return kept


def collect(groups):
    """Glyphs whose thin-pack first choice is the offline label. Sorted by video time so the kept set is stable."""
    meta, by = load_rows()
    order = sorted(meta.values(), key=lambda r: (minute_of(r["video_time"]), r["video_time"], r["frame_path"]))
    seen = set()
    items = {g: [] for g in GROUPS}
    skipped = []
    labs = {g: groups[g][0] for g in GROUPS}
    vecs = {g: groups[g][1] for g in GROUPS}
    for r in order:
        fp = r["frame_path"]
        if fp in seen:
            continue
        seen.add(fp)
        stem = Path(fp).stem
        m = minute_of(r["video_time"])
        v = band(load_image(stem))
        away = r["away"]
        gs = M.score_glyphs(v, "away")
        if away.isdigit() and len(gs) == len(away):
            for i, g in enumerate(gs):
                lab, vec = argmax(labs["score"], vecs["score"], g, M.SCORE_SIZE)
                if lab == away[i] and vec is not None:
                    items["score"].append((m, lab, vec, f"162055:m{m:02d}:score:{lab}:{stem}:{i}"))
                else:
                    skipped.append((r["video_time"], "score", away[i], lab))
        if "clock" in by[fp]:
            cg = clock_glyphs(v)
            exp = digits_of(by[fp]["clock"])
            if cg is not None and len(cg) == len(exp):
                for i, g in enumerate(cg):
                    lab, vec = argmax(labs["clock"], vecs["clock"], g, M.CLOCK_SIZE)
                    if lab == exp[i] and vec is not None:
                        items["clock"].append((m, lab, vec, f"162055:m{m:02d}:clock:{lab}:{stem}:{i}"))
                    else:
                        skipped.append((r["video_time"], "clock", exp[i], lab))
        if "quarter" in by[fp]:
            crop = M.quarter_crop(v)
            exp = QWORD[int(by[fp]["quarter"])]
            if crop is not None:
                lab, vec = argmax(labs["quarter"], vecs["quarter"], crop, M.QUARTER_SIZE)
                if lab == exp and vec is not None:
                    items["quarter"].append((m, lab, vec, f"162055:m{m:02d}:quarter:{lab}:{stem}"))
                else:
                    skipped.append((r["video_time"], "quarter", exp, lab))
    return {g: diversify(items[g]) for g in GROUPS}, skipped


def save_pack(path: Path, groups, source) -> None:
    payload = {}
    for g in GROUPS:
        labels, vecs, src = groups[g]
        payload[f"{g}_labels"] = np.array(labels)
        payload[f"{g}_vecs"] = np.asarray(vecs, dtype=np.float32)
        payload[f"{g}_src"] = np.array(src)
    payload["source_keys"] = np.array(list(source))
    payload["source_vals"] = np.array(list(source.values()))
    np.savez_compressed(path, **payload)


def with_extra(groups, extra):
    out = {}
    for g in GROUPS:
        labels, vecs, src = groups[g]
        add = extra[g]
        if add:
            labels = list(labels) + [it[1] for it in add]
            vecs = np.vstack([vecs, np.stack([it[2] for it in add]).astype(np.float32)])
            src = list(src) + [it[3] for it in add]
        out[g] = (labels, vecs, src)
    return out


def check(pack_path: Path) -> None:
    import cv2
    reader = M.MondayNightReader(pack_path)
    fx = ROOT / "sidecar" / "fixtures"
    for name, (away, home, q, clock, red) in FIXTURES.items():
        fr = reader.read(cv2.imread(str(fx / name)))
        got = (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red)
        if not fr.ok or got != (away, home, q, clock, red):
            raise SystemExit(f"{name} would become {got} ({fr.reason})")
    meta, by = load_rows()
    seen = set()
    for r in meta.values():
        fp = r["frame_path"]
        if fp in seen:
            continue
        seen.add(fp)
        im = load_image(Path(fp).stem)
        m = minute_of(r["video_time"])
        full = reader.read(im)
        held = reader.read(im, drop_src=f"162055:m{m:02d}:")
        _expect(r, by[fp], full, held)


def _field(fr, field, raw):
    if field == "score":
        if fr.left_score is None:
            return None
        return f"{fr.left_score}-{fr.right_score}"
    if field == "quarter":
        return None if fr.quarter is None else str(fr.quarter)
    if field == "clock":
        return fr.clock_text
    return raw


def _expect(row, labels, full, held) -> None:
    score = f"{row['away']}-{row['home']}"
    want = {"score": score}
    if "quarter" in labels:
        want["quarter"] = labels["quarter"]
    if "clock" in labels:
        want["clock"] = labels["clock"]
    for field, exp in want.items():
        got = _field(full, field, exp)
        if got != exp:
            raise SystemExit(f"{row['video_time']} full {field}: {got} != {exp} ({full.reason})")
        h = _field(held, field, exp)
        if h not in (None, exp):
            raise SystemExit(f"{row['video_time']} holdout {field}: {h} != {exp}")


def main() -> int:
    groups, source = load_base(M.PACK)
    extra, skipped = collect(groups)
    source["scr:162055"] = "real:match-20261017-162055"
    source["qtr:162055"] = "real:match-20261017-162055"
    source["clk:162055"] = "real:match-20261017-162055"
    built = with_extra(groups, extra)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "pack.npz"
        save_pack(tmp, built, source)
        check(tmp)
        save_pack(M.PACK, built, source)
    counts = {g: len(extra[g]) for g in GROUPS}
    print("wrote", M.PACK, "added", counts, "skipped", len(skipped))
    for row in skipped:
        print("  skip", row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
