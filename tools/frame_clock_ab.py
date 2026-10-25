#!/usr/bin/env python3
"""Re-score a bundle's fragment-close clock against frame_clock.jsonl.

Prints JSON on stdout. Does not write a receipt, a proof, or the hash chain.
The input-proof score (match_permille, chance, offset) is `qorcap align-ab DIR`.
See docs/FRAME_CLOCK_AB.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FPS = 30.0


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def pct(xs: list[int], p: int) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[(len(xs) - 1) * p // 100]


def file_clocks(manifest: list[dict]) -> list[dict]:
    segs = [m for m in manifest if m.get("type") == "segment"]
    files: list[str] = []
    for s in segs:
        f = s.get("file") or "video.mp4"
        if f not in files:
            files.append(f)
    clocks = []
    for f in files:
        mine = [s for s in segs if (s.get("file") or "video.mp4") == f]
        pts = []
        for s in mine:
            if s.get("kind") != "media":
                continue
            fr, mono = s.get("frames"), s.get("mono_ms")
            if isinstance(fr, (int, float)) and not isinstance(fr, bool) and isinstance(mono, (int, float)) and not isinstance(mono, bool) and fr > 0:
                pts.append((float(fr), float(mono)))
        init = 0.0
        found = False
        for s in mine:
            if s.get("kind") == "init" and isinstance(s.get("mono_ms"), (int, float)) and not isinstance(s.get("mono_ms"), bool):
                init = float(s["mono_ms"])
                found = True
                break
        if not found and mine and isinstance(mine[0].get("mono_ms"), (int, float)) and not isinstance(mine[0].get("mono_ms"), bool):
            init = float(mine[0]["mono_ms"])
        nominal = 1000.0 / FPS
        start, mpf, fitted = init, nominal, False
        if len(pts) >= 5 and pts[-1][0] - pts[0][0] >= 10.0 * FPS:
            n = float(len(pts))
            mx = sum(p[0] for p in pts) / n
            my = sum(p[1] for p in pts) / n
            sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
            sxx = sum((p[0] - mx) ** 2 for p in pts)
            slope = sxy / sxx if sxx > 0 else nominal
            if abs(slope / nominal - 1.0) > 0.05:
                slope = nominal
            start, mpf, fitted = my - slope * mx, slope, True
        clocks.append({"file": f, "start_ms": start, "ms_per_frame": mpf, "fitted": fitted, "segs": mine})
    return clocks


def clock_ab(manifest: list[dict], stamps: list[dict]) -> dict:
    clocks = file_clocks(manifest)
    if not clocks:
        return {"fit": None, "measured": None}
    c = next((x for x in clocks if x["fitted"]), clocks[0])
    resid = []
    for s in c["segs"]:
        if s.get("kind") != "media":
            continue
        fr, mono = s.get("frames"), s.get("mono_ms")
        if isinstance(fr, bool) or isinstance(mono, bool):
            continue
        if not isinstance(fr, (int, float)) or not isinstance(mono, (int, float)) or fr <= 0:
            continue
        resid.append(abs(int(round(float(mono) - (c["start_ms"] + c["ms_per_frame"] * float(fr))))))
    fit = {
        "file": c["file"],
        "fitted": c["fitted"],
        "ms_per_frame_milli": int(round(c["ms_per_frame"] * 1000)),
        "n": len(resid),
        "resid_abs_p50_ms": pct(resid, 50),
        "resid_abs_p95_ms": pct(resid, 95),
        "resid_abs_max_ms": max(resid) if resid else 0,
    }
    mine = [s for s in stamps if s.get("type") == "frame" and s.get("file") == c["file"]]
    if not mine:
        return {"fit": fit, "measured": None}
    signed = []
    for s in mine:
        fitted = c["start_ms"] + c["ms_per_frame"] * int(s["frame"])
        signed.append(int(round(s["t_mono_us"] / 1000.0 - fitted)))
    vs = [abs(v) for v in signed]
    nominal_us = int(round(1_000_000.0 / FPS))
    gaps = [abs((int(b["t_mono_us"]) - int(a["t_mono_us"])) - nominal_us) // 1000 for a, b in zip(mine, mine[1:])]
    return {
        "fit": fit,
        "measured": {
            "n": len(mine),
            "vs_fit_abs_p50_ms": pct(vs, 50),
            "vs_fit_abs_p95_ms": pct(vs, 95),
            "vs_fit_abs_max_ms": max(vs) if vs else 0,
            "vs_fit_signed_p50_ms": pct(signed, 50),
            "interval_abs_dev_p50_ms": pct(gaps, 50),
            "interval_abs_dev_p95_ms": pct(gaps, 95),
            "interval_abs_dev_max_ms": max(gaps) if gaps else 0,
        },
    }


def report(bundle: Path) -> dict:
    manifest = read_jsonl(bundle / "manifest.jsonl")
    stamps = [s for s in read_jsonl(bundle / "frame_clock.jsonl") if s.get("type") == "frame"]
    out = clock_ab(manifest, stamps)
    out["frame_clock_file"] = (bundle / "frame_clock.jsonl").is_file()
    out["frame_clock_frames"] = len(stamps)
    out["how_to_read"] = (
        "fit.resid_* is the fragment-close straight line (the clock used with the flag off). "
        "measured.vs_fit_* is how far real frame arrivals sit from that line "
        "(vs_fit_signed_p50_ms < 0: arrivals sit before the line). "
        "measured.interval_* is frame-to-frame jitter of those arrivals (one frame is 33 ms). "
        "This tool does not write a receipt. For match_permille / chance / offset run `qorcap align-ab DIR`."
    )
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: frame_clock_ab.py BUNDLE_DIR", file=sys.stderr)
        return 2
    print(json.dumps(report(Path(argv[0])), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
