"""Snapped quarter simulation for the clock gates.

Sixty seeds, 400 s at 10 Hz. Runs of 5–30 s, stops of 10–40 s. One event, snapped
onto the first matching run or stop at or after a random time from 20–40 s.
A read above 15:00 is not clamped. Truth kept is the share of frames at and
after the event whose output equals the true clock. Wrong-after is a published
value other than the truth after the first ``clock_disagree``. Wrong-before is
a published value other than the truth up to that detection.

Not a pytest module. Run: ``python3 tools/clock_recovery_sim.py``.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))

from qorcap_clock import ClockHistory  # noqa: E402


def schedule(rng, horizon=400_000):
    t = 0
    segs = []
    while t < horizon:
        run = rng.randint(5, 30) * 1000
        segs.append(("run", t, min(horizon, t + run)))
        t += run
        if t >= horizon:
            break
        stop = rng.randint(10, 40) * 1000
        segs.append(("stop", t, min(horizon, t + stop)))
        t += stop
    return segs


def truth_at(segs, t, rate, start=900.0):
    remain = float(start)
    for kind, a, b in segs:
        if t <= a:
            break
        span = min(t, b) - a
        if kind == "run" and span > 0:
            remain -= rate * span / 1000.0
        if t <= b:
            break
    return int(max(0.0, remain))


def segment_at(segs, t):
    for i, (kind, a, b) in enumerate(segs):
        if a <= t < b:
            return i, kind
    return len(segs) - 1, segs[-1][0]


def run_case(seed, rate, event, horizon=400_000):
    rng = random.Random(seed)
    segs = schedule(rng, horizon)
    event_t = rng.randint(20, 40) * 1000
    idx, _kind = segment_at(segs, event_t)
    want = "run" if event.startswith("run") else "stop"
    while idx < len(segs) and segs[idx][0] != want:
        idx += 1
    if idx >= len(segs):
        return None
    event_t = segs[idx][1]
    h = ClockHistory()
    det = None
    wa = wb = th = tn = 0
    for t in range(0, horizon, 100):
        truth = truth_at(segs, t, rate)
        read = truth
        i, k = segment_at(segs, t)
        if event == "run+5_4" and segs[idx][0] == "run" and segs[idx][1] <= t < segs[idx][1] + 4_000:
            read = truth + 5
        elif event == "run+5_8" and segs[idx][0] == "run" and segs[idx][1] <= t < segs[idx][1] + 8_000:
            read = truth + 5
        elif event == "run-10" and segs[idx][0] == "run" and segs[idx][1] <= t < segs[idx][1] + 5_000:
            read = max(0, truth - 10)
        elif event == "held+5" and i == idx and k == "stop":
            read = truth + 5
        elif event == "held-10" and i == idx and k == "stop":
            read = max(0, truth - 10)
        elif event == "H2" and (i == idx or (idx + 1 < len(segs) and i == idx + 1 and segs[i][0] == "run")):
            read = truth + 5
        out = h.filter(read, 1, t)
        if h.rule_note == "clock_disagree" and det is None:
            det = t
        if t >= event_t:
            tn += 1
            if out == truth:
                th += 1
            if det is not None and t > det and out is not None and out != truth:
                wa += 1
            elif (det is None or t <= det) and out is not None and out != truth:
                wb += 1
    return wa, wb, th, tn


def summarize(rate, event, seeds=range(60)):
    wa = wb = th = tn = n = 0
    for s in seeds:
        row = run_case(s, rate, event)
        if row is None:
            continue
        n += 1
        wa += row[0]
        wb += row[1]
        th += row[2]
        tn += row[3]
    pct = 100.0 * th / tn if tn else 0.0
    return n, wa, wb, pct


def main() -> None:
    events = ["run+5_4", "run+5_8", "held+5", "H2", "run-10", "held-10"]
    rates = [0.8, 0.85, 0.9, 1.0, 1.1, 1.15, 1.2]
    print("snap no-clamp")
    for rate in rates:
        print(f"== rate {rate}")
        for ev in events:
            n, wa, wb, pct = summarize(rate, ev)
            print(f"  {ev:10} n {n:2} wa {wa:6} wb {wb:5} truth {pct:6.2f}")


if __name__ == "__main__":
    main()
