"""Controller-to-screen lag band. Thresholds are PROVISIONAL.

Samples come from a natural on-screen response after a button edge: an L2 aim
zoom in the center crop, or an R2 ammo-crop change, and from OPTIONS-to-pause
when that read exists. One OPTIONS hold does not meet the sample floor.

A band is measured only when there are at least 30 samples, at least 0.999 of
them sit within one frame of the median, and the three time-thirds differ by
at most one frame. Otherwise the band is not measured.
"""
from __future__ import annotations

import json
from pathlib import Path

# PROVISIONAL. Not validated on a clean measured match.
LAG_MIN_SAMPLES = 30
LAG_SHARE_MIN = 0.999
LAG_THIRDS_MAX_FRAMES = 1
FRAME_US = 16_667
# Center crop and ammo crop on a 16:9 frame. PROVISIONAL places.
CENTER_BOX = (0.40, 0.40, 0.20, 0.20)
AMMO_BOX = (0.78, 0.86, 0.18, 0.10)
# Activity has to move by this fraction after the edge. PROVISIONAL.
ACTIVITY_STEP = 0.15
PROBE_WINDOW_US = 500_000

SCHEMA = "qorcap-lag-band/v0"


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def lag_band(samples) -> dict:
    """Return the band, or not_measured. Does not invent a median."""
    rows = [s for s in samples or [] if isinstance(s, dict) and isinstance(s.get("lag_us"), int)]
    if len(rows) < LAG_MIN_SAMPLES:
        options_only = bool(rows) and all(s.get("method") == "options_pause" for s in rows)
        return _not_measured(rows, "options_hold_alone" if options_only else "sample_floor")
    lags = [s["lag_us"] for s in rows]
    median = _median(lags)
    inside = sum(1 for n in lags if abs(n - median) <= FRAME_US)
    ordered = sorted(rows, key=lambda s: s.get("t_us") or 0)
    thirds = [ordered[i * len(ordered) // 3:(i + 1) * len(ordered) // 3] for i in range(3)]
    medians = [_median([s["lag_us"] for s in part]) for part in thirds if part]
    spread = max(medians) - min(medians) if medians else 0
    # share >= 0.999 and thirds differ by at most one frame, in integers.
    share_ok = inside * 1000 >= len(lags) * 999
    spread_ok = spread <= LAG_THIRDS_MAX_FRAMES * FRAME_US
    if not share_ok or not spread_ok:
        return _not_measured(rows, "spread", median=median, inside=inside, spread_us=spread)
    iqr = _median([abs(n - median) for n in lags])
    return {
        "schema": SCHEMA,
        "status": "measured",
        "reason": "sample_floor_met",
        "n": len(rows),
        "median_us": median,
        "iqr_us": iqr,
        "inside": inside,
        "spread_us": spread,
        "provisional": True,
        "label": "PROVISIONAL",
        "note": "Lag band from on-screen responses. PROVISIONAL until a real practice match checks the floor.",
    }


def _not_measured(rows, reason, median=None, inside=None, spread_us=None) -> dict:
    return {
        "schema": SCHEMA,
        "status": "not_measured",
        "reason": reason,
        "n": len(rows),
        "median_us": median,
        "inside": inside,
        "spread_us": spread_us,
        "provisional": True,
        "label": "PROVISIONAL",
        "note": "No lag band. One OPTIONS hold does not meet the sample floor.",
    }


def _crop_activity(img, box) -> int | None:
    try:
        import numpy as np
    except ImportError:
        return None
    if not isinstance(img, np.ndarray) or img.ndim != 3:
        return None
    h, w = img.shape[:2]
    x, y, bw, bh = box
    x0, y0 = int(x * w), int(y * h)
    x1, y1 = max(x0 + 2, int((x + bw) * w)), max(y0 + 2, int((y + bh) * h))
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    gray = crop.mean(axis=2).astype("int16")
    diff = abs(gray[:, 1:] - gray[:, :-1]).sum() + abs(gray[1:, :] - gray[:-1, :]).sum()
    return int(diff)


def response_lag(edge_us, frames, *, before, after_limit=PROBE_WINDOW_US) -> int | None:
    """First frame after the edge whose metric steps away from the pre-edge value.

    ``frames`` is ``(mono_us, metric)``. No step means no sample.
    """
    if edge_us is None or before is None:
        return None
    for t, metric in frames or []:
        if not isinstance(t, int) or not isinstance(metric, int):
            continue
        dt = t - edge_us
        if dt < 0 or dt > after_limit:
            continue
        if before == 0:
            moved = metric > 0
        else:
            moved = abs(metric - before) / abs(before) >= ACTIVITY_STEP
        if moved:
            return dt
    return None


class LagProbe:
    """Collects samples during a match. Image metrics are skipped when the frame is not an array."""

    def __init__(self) -> None:
        self.samples: list[dict] = []
        self._prev_center = None
        self._prev_ammo = None
        self._used = set()

    def observe(self, img, inputs, mono_us) -> None:
        if not isinstance(mono_us, int):
            return
        center = _crop_activity(img, CENTER_BOX)
        ammo = _crop_activity(img, AMMO_BOX)
        edges = _edges(inputs)
        if center is not None and self._prev_center is not None:
            self._take(edges.get("l2"), "ads_zoom", mono_us, center, self._prev_center)
        if ammo is not None and self._prev_ammo is not None:
            self._take(edges.get("r2"), "ammo_decrement", mono_us, ammo, self._prev_ammo)
        if center is not None:
            self._prev_center = center
        if ammo is not None:
            self._prev_ammo = ammo

    def _take(self, edge_us, method, mono_us, metric, before) -> None:
        if not isinstance(edge_us, int):
            return
        key = (method, edge_us)
        if key in self._used:
            return
        lag = response_lag(edge_us, [(mono_us, metric)], before=before)
        if lag is None:
            return
        self._used.add(key)
        self.samples.append({"method": method, "lag_us": lag, "t_us": edge_us})

    def band(self) -> dict:
        return lag_band(self.samples)

    def measured_us(self):
        band = self.band()
        if band["status"] != "measured":
            return None
        return band["median_us"]

    def write(self, path: Path) -> dict:
        doc = self.band()
        doc["samples"] = list(self.samples)
        Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return doc


def _edges(inputs) -> dict:
    """Latest L2 and R2 rising times. Empty when the log has none."""
    prev_l2 = prev_r2 = None
    found = {}
    for row in inputs or []:
        if not isinstance(row, dict) or row.get("type") != "in":
            continue
        t = row.get("t_us")
        if not isinstance(t, int):
            continue
        l2, r2 = row.get("l2"), row.get("r2")
        if isinstance(l2, int) and isinstance(prev_l2, int) and l2 - prev_l2 > 60:
            found["l2"] = t
        if isinstance(r2, int) and isinstance(prev_r2, int) and r2 - prev_r2 > 60:
            found["r2"] = t
        if isinstance(l2, int):
            prev_l2 = l2
        if isinstance(r2, int):
            prev_r2 = r2
    return found
