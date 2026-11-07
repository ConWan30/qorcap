"""Controller-to-screen lag band. Thresholds are PROVISIONAL.

ADS zoom and ammo decrement are separate hooks. OPTIONS-to-pause is not a
measurement. A probe counts only with its own id and its own source frame.
The thirds are elapsed time, not three slices of a list. A set that meets
the numeric floor is diagnostic. It is not a measured causal lag.
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


def _unique_observed(samples, method: str) -> tuple[list[dict], str | None]:
    """One row per probe id and source frame. Duplicates and missing ids do not count."""
    seen_probes: set[str] = set()
    seen_frames: set[str] = set()
    rows = []
    for sample in samples or []:
        if not isinstance(sample, dict) or sample.get("method") != method:
            continue
        if sample.get("outcome") not in (None, "observed", "success"):
            continue
        if not isinstance(sample.get("lag_us"), int) or isinstance(sample.get("lag_us"), bool):
            continue
        probe = sample.get("probe_id")
        frame = sample.get("source_frame")
        when = sample.get("t_us")
        if not isinstance(probe, str) or not probe or not isinstance(frame, str) or not frame:
            continue
        if isinstance(when, bool) or not isinstance(when, int):
            continue
        if probe in seen_probes or frame in seen_frames:
            continue
        seen_probes.add(probe)
        seen_frames.add(frame)
        rows.append(sample)
    return rows, None


def _time_thirds(rows: list[dict]) -> tuple[bool, int]:
    """True when each third of the elapsed span holds a sample. Count splits do not."""
    times = [s["t_us"] for s in rows]
    start, end = min(times), max(times)
    span = end - start
    if span <= 0:
        return False, 0
    cut1 = start + span // 3
    cut2 = start + (2 * span) // 3
    buckets = [
        [s for s in rows if start <= s["t_us"] < cut1],
        [s for s in rows if cut1 <= s["t_us"] < cut2],
        [s for s in rows if cut2 <= s["t_us"] <= end],
    ]
    if any(not part for part in buckets):
        return False, 0
    medians = [_median([s["lag_us"] for s in part]) for part in buckets]
    return True, max(medians) - min(medians)


def lag_band(samples) -> dict:
    """Diagnostic or not_measured. Does not report a causal lag."""
    probes = [s for s in samples or [] if isinstance(s, dict)]
    best = None
    blocked = None
    for method in ("ads_zoom", "ammo_decrement"):
        rows, _ = _unique_observed(probes, method)
        if len(rows) < LAG_MIN_SAMPLES:
            blocked = blocked or "sample_floor"
            continue
        covered, spread = _time_thirds(rows)
        if not covered:
            blocked = "no_time_coverage"
            continue
        lags = [s["lag_us"] for s in rows]
        median = _median(lags)
        inside = sum(1 for n in lags if abs(n - median) <= FRAME_US)
        share_ok = inside * 1000 >= len(lags) * 999
        if not share_ok or spread > LAG_THIRDS_MAX_FRAMES * FRAME_US:
            blocked = "spread"
            continue
        best = (method, rows, median, inside, spread)
    if best is None:
        options, _ = _unique_observed(probes, "options_pause")
        if options:
            reason = "not_a_causal_lag" if len(options) >= LAG_MIN_SAMPLES else "options_hold_alone"
            return _not_measured(options, reason)
        options_present = any(
            isinstance(s, dict) and s.get("method") == "options_pause" and isinstance(s.get("lag_us"), int)
            for s in probes)
        natural_present = any(
            isinstance(s, dict) and s.get("method") in ("ads_zoom", "ammo_decrement") for s in probes)
        if options_present and not natural_present:
            return _not_measured([], "options_hold_alone")
        return _not_measured([], blocked or "sample_floor")
    method, rows, median, inside, spread = best
    iqr = _median([abs(n - median) for n in (s["lag_us"] for s in rows)])
    return {
        "schema": SCHEMA,
        "status": "diagnostic",
        "reason": "not_a_causal_lag",
        "method": method,
        "n": len(rows),
        "median_us": median,
        "iqr_us": iqr,
        "inside": inside,
        "spread_us": spread,
        "provisional": True,
        "label": "PROVISIONAL",
        "note": "Diagnostic only. A consistent natural response is not a measured causal lag.",
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

    def observe(self, img, inputs, mono_us, source_frame=None) -> None:
        if not isinstance(mono_us, int) or isinstance(mono_us, bool):
            return
        center = _crop_activity(img, CENTER_BOX)
        ammo = _crop_activity(img, AMMO_BOX)
        edges = _edges(inputs)
        self._consider(edges.get("l2"), "ads_zoom", mono_us, center, self._prev_center, source_frame)
        self._consider(edges.get("r2"), "ammo_decrement", mono_us, ammo, self._prev_ammo, source_frame)
        if center is not None:
            self._prev_center = center
        if ammo is not None:
            self._prev_ammo = ammo

    def _consider(self, edge_us, method, mono_us, metric, before, source_frame) -> None:
        if not isinstance(edge_us, int) or isinstance(edge_us, bool):
            return
        key = (method, edge_us)
        if key in self._used:
            return
        self._used.add(key)
        frame = source_frame if isinstance(source_frame, str) and source_frame else None
        row = {"method": method, "probe_id": f"{method}:{edge_us}", "source_frame": frame, "t_us": edge_us}
        if metric is None or before is None:
            row["outcome"] = "ambiguous"
            row["lag_us"] = None
            self.samples.append(row)
            return
        lag = response_lag(edge_us, [(mono_us, metric)], before=before)
        if lag is None:
            row["outcome"] = "failed"
            row["lag_us"] = None
        else:
            row["outcome"] = "observed"
            row["lag_us"] = lag
        self.samples.append(row)

    def band(self) -> dict:
        return lag_band(self.samples)

    def measured_us(self):
        """None until a causal lag exists. A diagnostic band does not release the seal window."""
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
