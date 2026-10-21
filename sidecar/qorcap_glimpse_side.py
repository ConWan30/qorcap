"""Cheap-tier Glimpse observer helpers: flags, caps, the unhinted-prompt guard, and the call lane.

The watcher imports this module. This module does not import the watcher, the reader, or the attest tool,
and it does not write a lock, a receipt, a challenge, or a touchdown verdict. Witness and burst output
stays in the Glimpse folder. ``feeds_lock`` is never set true from here.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import threading
from pathlib import Path

WITNESS_ENV = "QORCAP_GLIMPSE_WITNESS"
BURST_ENV = "QORCAP_GLIMPSE_BURST"
RING_ENV = "QORCAP_GLIMPSE_BURST_RING"
ATTEST_ENV = "QORCAP_GLIMPSE_ATTEST"
CLIP_ENV = "QORCAP_GLIMPSE_CLIP_AUDIT"
COPY_ENV = "QORCAP_GLIMPSE_ATTEST_COPY"
WEEK_ENV = "QORCAP_GLIMPSE_WEEKLY_LIMIT"
GAME_ENV = "QORCAP_GLIMPSE_GAME_CAP"
WITNESS_CAP_ENV = "QORCAP_GLIMPSE_WITNESS_CAP"
BURST_CAP_ENV = "QORCAP_GLIMPSE_BURST_CAP"
CLIP_CAP_ENV = "QORCAP_GLIMPSE_CLIP_CAP"
WITNESS_INTERVAL_ENV = "QORCAP_GLIMPSE_WITNESS_INTERVAL"

# Cheap tier from the teardown. S_witness fits 100 successful crops; S_burst fits a short disagreement.
S_WITNESS_DEFAULT = 0.08
S_BURST_DEFAULT = 0.02
S_CLIP_DEFAULT = 0.03
WITNESS_INTERVAL_DEFAULT = 45.0
BURST_MAX_EVENTS = 8
BURST_CROPS_PER_EVENT = 1
BURST_MAX_AGE_S = 15.0
DISK_MIN_BYTES = 1 << 30
LOCK_WITHHELD = "not given for this crop"

_ON = frozenset({"1", "true", "yes", "on"})


def env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ON


def finite_positive(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n <= 0:
        return None
    return n


def resolve_money(env_name: str, fallback):
    """Env wins when it is set. Missing, blank, non-numeric, or <= 0 is unconfigured (None).

    An unset env uses ``fallback`` (the CLI value, or the cheap-tier stratum default). A non-positive
    fallback is also unconfigured: the caller refuses the call instead of inventing a cap.
    """
    if env_name in os.environ:
        return finite_positive(os.environ.get(env_name, "").strip())
    return finite_positive(fallback)


def witness_interval() -> float:
    """Seconds between witness calls. ``0`` is allowed so a test can arm every fresh frame."""
    if WITNESS_INTERVAL_ENV not in os.environ:
        return WITNESS_INTERVAL_DEFAULT
    raw = os.environ.get(WITNESS_INTERVAL_ENV, "").strip()
    try:
        n = float(raw)
    except ValueError:
        return WITNESS_INTERVAL_DEFAULT
    if not math.isfinite(n) or n < 0:
        return WITNESS_INTERVAL_DEFAULT
    return n


def task_withholds_lock(task: str, lock) -> bool:
    """Guard 6: the user text withholds the lock. Static task digits (1, 4, 5) may remain."""
    if not isinstance(task, str) or LOCK_WITHHELD not in task:
        return False
    if re.search(r"AWAY \d+ - HOME \d+", task):
        return False
    ctx = task.split("Context for this crop", 1)[-1]
    if lock:
        for n in (lock.get("away"), lock.get("home")):
            if isinstance(n, bool) or not isinstance(n, int):
                continue
            if re.search(rf"(?<!\d){int(n)}(?!\d)", ctx):
                return False
    return True


def _implausible(flag: str) -> bool:
    return flag in ("score_decrease_vs_lock", "sides_swapped_vs_lock", "both_sides_changed_vs_lock") or (
        isinstance(flag, str) and (flag.endswith("not_a_football_score") or "plus1_without_td" in flag)
    )


def is_burst_trigger(res, obj, lock, pending_td: bool = False) -> bool:
    """Disagreement, implausible transition, or a pending touchdown. Agreement and stats are not triggers."""
    if pending_td:
        return True
    if not res:
        return False
    flags = list(res.get("flags") or [])
    region = res.get("region")
    if region == "STATS_OVERLAY" or "rejected_or_unknown" in flags or "agrees_with_lock" in flags:
        return False
    if any(_implausible(f) for f in flags):
        return True
    if region != "SCOREBUG" or not obj or not lock:
        return False
    a, h = obj.get("away_score"), obj.get("home_score")
    if a is None or h is None:
        return False
    return (a, h) != (lock.get("away"), lock.get("home"))


def burst_verdict(obj, lock) -> str:
    if not obj or obj.get("region") != "SCOREBUG":
        return "no_read"
    a, h = obj.get("away_score"), obj.get("home_score")
    if a is None or h is None:
        return "no_read"
    if lock and (a, h) == (lock.get("away"), lock.get("home")):
        return "agree"
    return "disagree"


def disk_free_ok(path, minimum: int = DISK_MIN_BYTES) -> bool:
    try:
        return shutil.disk_usage(Path(path)).free >= minimum
    except OSError:
        return False


def _lower_thread() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


class CallLane:
    """One background slot for witness and burst model work. Sync in tests. Async drops when busy."""

    def __init__(self):
        self.sync = True
        self.dropped = 0
        self._mu = threading.Lock()
        self._busy = False
        self._thread = None

    def submit(self, fn) -> bool:
        if self.sync:
            fn()
            return True
        with self._mu:
            if self._busy:
                self.dropped += 1
                return False
            self._busy = True

            def wrap():
                try:
                    _lower_thread()
                    fn()
                finally:
                    with self._mu:
                        self._busy = False

            self._thread = threading.Thread(target=wrap, name="glimpse-observer", daemon=True)
            self._thread.start()
            return True

    def drain(self, timeout: float = 30.0) -> None:
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
