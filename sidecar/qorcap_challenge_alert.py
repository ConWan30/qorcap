"""Windows alerts for a live QorTime challenge. Presentation only.

The operator often plays with the QorCap console off-screen, so a challenge card
goes unseen. This module makes the same challenge audible and visible without
moving the challenge, the response window, or anything written to the bundle.

Nothing here is recorded. A miss stays unanswered. ``notify_challenge`` only
enqueues work and returns. One daemon worker runs each alert: the toast, then
the flash, then the beep. The queue holds two jobs; a third is dropped. Toast
and flash do not wait on the sound player. If playback is still going after
``_BEEP_TIMEOUT_S`` the player is abandoned, one warning is logged, and the
worker is replaced so a later alert still runs. On any other platform the
Windows calls are skipped and this module does not raise.

A failed backend logs one warning per process on the ``qorcap.challenge_alert``
logger (stderr when nothing else is configured). It never writes a bundle file,
a checkpoint, or a receipt field.

Environment (unset means on):

* ``QORCAP_CHALLENGE_BEEP=0`` skips the three-beep. ``false`` / ``no`` / ``off`` too.
* ``QORCAP_CHALLENGE_BEEP_HZ`` tone frequency, default 2400. A non-finite or
  unreadable value logs one warning and uses 2400. A finite value outside
  200..8000 is clamped into that range and logs one warning. A bad value never
  turns off the toast or the flash.
* ``QORCAP_CHALLENGE_BEEP_VOLUME`` amplitude, default full scale. This does not
  affect the toast or the flash.
  Mute, any case: ``off``, ``false``, ``no``, ``0``. ``0.0`` and ``0%`` are
  silence too.
  An explicit percent is ``80%`` or a bare integer from 0 to 100. A bare ``1``
  is 100% (full scale), not one percent. ``50`` is half. ``100`` is full scale.
  A decimal strictly between 0 and 1 is a fraction (``0.5`` is half). ``1.0``
  is full scale, the same loudness as bare ``1`` and ``100``.
  Anything else (``1.5``, ``loud``, ``inf``) logs one warning and stays at full scale.
* ``QORCAP_CHALLENGE_TOAST=0`` skips the toast.
* ``QORCAP_CHALLENGE_FLASH=0`` skips flashing the console window and its taskbar button.

The existing ``winsound.MessageBeep`` in the challenge path is separate and still runs.
"""
from __future__ import annotations

import array
import io
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_LOG = logging.getLogger("qorcap.challenge_alert")
_OFF = {"0", "false", "no", "off"}
# Volume mute tokens. ``0`` is also the integer percent 0. Matched case-insensitively.
_MUTE_VOLUME = {"off", "false", "no", "0"}
_RATE = 44100
_BEEP_S = 0.16
_GAP_S = 0.10
_FADE_S = 0.004
_BEEPS = 3
DEFAULT_HZ = 2400
DEFAULT_VOLUME = 1.0
_HZ_MIN = 200
_HZ_MAX = 8000
_QUEUE_MAX = 2
# A normal three-beep is under a second. A PlaySound that is still blocked after
# this is treated as hung: the player thread is abandoned and the worker replaced.
_BEEP_TIMEOUT_S = 5.0

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()
_WAV_CACHE: dict[tuple[int, float], bytes] = {}
_JOBS: queue.Queue[Callable[[], None]] | None = None
_WORKER_LOCK = threading.Lock()
_WORKER_GEN = 0
_TOAST_PROCS: list[subprocess.Popen] = []

# PowerShell's own AUMID. An unpackaged console has none. Some Windows builds
# still refuse this borrowed identity; the toast is best-effort and a failure
# is logged once.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$text = [string]$env:QORCAP_TOAST_TEXT
if ([string]::IsNullOrWhiteSpace($text)) { $text = 'QorCap challenge' }
$xmlText = [System.Security.SecurityElement]::Escape($text)
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(('<toast duration="short"><visual><binding template="ToastGeneric"><text>QorCap challenge</text><text>' + $xmlText + '</text></binding></visual><audio silent="true"/></toast>'))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
"""


@dataclass(frozen=True)
class AlertConfig:
    beep: bool
    toast: bool
    flash: bool
    beep_hz: int
    beep_volume: float


@dataclass(frozen=True)
class AlertBackends:
    beep: Callable[[str, AlertConfig], None]
    toast: Callable[[str, AlertConfig], None]
    flash: Callable[[str, AlertConfig], None]


def warn_once(backend: str, detail: object) -> None:
    """Log one warning for this backend. Stderr only; never a bundle file."""
    with _WARN_LOCK:
        if backend in _WARNED:
            return
        _WARNED.add(backend)
    _LOG.warning("challenge alert %s failed: %s", backend, detail)


def _enabled(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name)
    if raw is None:
        return True
    return raw.strip().lower() not in _OFF


def _is_int_token(text: str) -> bool:
    body = text[1:] if text[:1] in "+-" else text
    return bool(body) and body.isascii() and body.isdigit()


def _hz(raw: str | None) -> int:
    """Default 2400. Non-finite values fall back; finite values clamp to 200..8000.

    Either path logs one warning. This function does not raise, so a bad frequency
    cannot skip the toast or the flash.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_HZ
    text = str(raw).strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        warn_once("beep_hz", f"unusable frequency {text!r}; using {DEFAULT_HZ}")
        return DEFAULT_HZ
    if not math.isfinite(value):
        warn_once("beep_hz", f"non-finite frequency {text!r}; using {DEFAULT_HZ}")
        return DEFAULT_HZ
    hz = int(value)
    if hz < _HZ_MIN or hz > _HZ_MAX:
        clamped = min(_HZ_MAX, max(_HZ_MIN, hz))
        warn_once("beep_hz", f"frequency {hz} Hz outside {_HZ_MIN}..{_HZ_MAX}; clamped to {clamped}")
        return clamped
    return hz


def _warn_volume(text: str) -> float:
    warn_once("beep_volume", f"unusable volume {text!r}; using full scale")
    return DEFAULT_VOLUME


def _as_percent(body: str, original: str) -> float:
    try:
        percent = float(int(body, 10)) if _is_int_token(body) else float(body)
    except (TypeError, ValueError):
        return _warn_volume(original)
    if not math.isfinite(percent) or percent < 0.0 or percent > 100.0:
        return _warn_volume(original)
    return percent / 100.0


def _volume(raw: str | None) -> float:
    """Percent, fraction, or mute. Bare ``1`` is 100 percent, not 1 percent.

    * ``off`` / ``false`` / ``no`` / ``0`` (any case) mute the beep.
    * ``80%`` or an integer 0..100 is a percent. Bare ``1`` is full scale.
    * A float strictly between 0 and 1 is a fraction (``0.5`` is half).
    * ``0.0`` is silence and ``1.0`` is full scale, the same loudness as ``0`` and bare ``1``.
    * Anything else logs one warning and uses full scale.

    This never raises. Volume does not turn the toast or the flash off.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_VOLUME
    text = str(raw).strip()
    low = text.lower()
    if low in _MUTE_VOLUME:
        return 0.0
    try:
        if low.endswith("%"):
            return _as_percent(text[:-1].strip(), text)
        if _is_int_token(text):
            # Integers are percents (50 is half). A bare 1 is the ambiguous case:
            # it used to mean one percent. It is full scale, same as 100 and 1.0.
            if int(text, 10) == 1:
                return DEFAULT_VOLUME
            return _as_percent(text, text)
        vol = float(text)
    except (TypeError, ValueError):
        return _warn_volume(text)
    if not math.isfinite(vol):
        return _warn_volume(text)
    if vol == 0.0 or vol == 1.0 or 0.0 < vol < 1.0:
        return vol
    return _warn_volume(text)


def config_from_env(env: Mapping[str, str]) -> AlertConfig:
    try:
        volume = _volume(env.get("QORCAP_CHALLENGE_BEEP_VOLUME"))
    except Exception as exc:
        warn_once("beep_volume", exc)
        volume = DEFAULT_VOLUME
    try:
        hz = _hz(env.get("QORCAP_CHALLENGE_BEEP_HZ"))
    except Exception as exc:
        warn_once("beep_hz", exc)
        hz = DEFAULT_HZ
    return AlertConfig(
        beep=_enabled(env, "QORCAP_CHALLENGE_BEEP") and volume > 0.0,
        toast=_enabled(env, "QORCAP_CHALLENGE_TOAST"),
        flash=_enabled(env, "QORCAP_CHALLENGE_FLASH"),
        beep_hz=hz,
        beep_volume=volume,
    )


def render_three_beep_wav(hz: int, volume: float) -> bytes:
    """Three high tones separated by silence. Peak sample is full int16 scale at volume 1.

    A sine at ±32767 is the loudest tone this format can hold. Playback is synchronous
    ``PlaySound`` on the alert worker: CPython rejects ``SND_ASYNC`` together with
    ``SND_MEMORY`` (``Cannot play asynchronously from memory``).
    """
    amp = int(round(32767 * max(0.0, min(1.0, float(volume)))))
    tone_hz = int(hz) if int(hz) > 0 else DEFAULT_HZ
    beep_n = int(_RATE * _BEEP_S)
    gap_n = int(_RATE * _GAP_S)
    fade_n = int(_RATE * _FADE_S)
    samples = array.array("h")
    for n in range(_BEEPS):
        for i in range(beep_n):
            env = 1.0
            if fade_n and i < fade_n:
                env = i / fade_n
            elif fade_n and i >= beep_n - fade_n:
                env = max(0.0, (beep_n - 1 - i) / fade_n)
            sample = math.sin(2.0 * math.pi * tone_hz * (i / _RATE))
            samples.append(int(round(amp * env * sample)))
        if n != _BEEPS - 1:
            samples.extend([0] * gap_n)
    if sys.byteorder != "little":
        samples.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_RATE)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def _cached_wav(hz: int, volume: float) -> bytes:
    key = (int(hz), float(volume))
    hit = _WAV_CACHE.get(key)
    if hit is None:
        hit = render_three_beep_wav(key[0], key[1])
        _WAV_CACHE[key] = hit
    return hit


def _is_windows() -> bool:
    """True on Windows. Tests replace this instead of patching ``os.name``.

    ``ctypes`` reads ``os.name`` at import and looks for a Windows-only symbol,
    so a test that patches the global breaks later imports.
    """
    return os.name == "nt"


def _play_wav(data: bytes) -> None:
    """Play a memory WAV on the alert worker. No-op off Windows. Logs once on failure.

    Flags are ``SND_MEMORY | SND_NODEFAULT`` and not ``SND_ASYNC``. CPython raises
    ``RuntimeError: Cannot play asynchronously from memory`` for that pair, which
    used to hide the clip and fall through to a quiet ``Beep``. The alert worker
    bounds this call: a stuck ``PlaySound`` does not hold later alerts.
    """
    if not _is_windows() or not data:
        return
    try:
        import winsound
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    except Exception as exc:
        warn_once("beep", exc)


def play_three_beep(prompt: str, cfg: AlertConfig) -> None:
    del prompt
    if cfg.beep_volume <= 0.0:
        return
    _play_wav(_cached_wav(cfg.beep_hz, cfg.beep_volume))


def _toast_text(prompt: str) -> str:
    text = " ".join(str(prompt).split())
    if len(text) > 240:
        text = text[:237] + "..."
    return text or "QorCap challenge"


def show_toast(prompt: str, cfg: AlertConfig) -> None:
    """Toast via WinRT in a hidden PowerShell process. No-op off Windows.

    The borrowed PowerShell AppUserModelID does not show a toast on every build.
    A spawn or WinRT failure is logged once and ignored.
    """
    del cfg
    if not _is_windows():
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": {**os.environ, "QORCAP_TOAST_TEXT": _toast_text(prompt)},
        }
        if flags:
            kwargs["creationflags"] = flags
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT],
            **kwargs,
        )
        _TOAST_PROCS.append(proc)
        for old in list(_TOAST_PROCS):
            old.poll()
        del _TOAST_PROCS[:-4]
    except Exception as exc:
        warn_once("toast", exc)


def flash_console(prompt: str, cfg: AlertConfig) -> None:
    """Flash the console caption and its taskbar button until the window is focused.

    ``FLASHW_ALL`` is the caption plus the taskbar button. ``FLASHW_TIMERNOFG`` keeps
    going until the operator focuses the console. No-op off Windows, or when this
    process has no console. A failure is logged once.
    """
    del prompt, cfg
    if not _is_windows():
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        flashw_all = 0x00000003
        flashw_timernofg = 0x0000000C
        info = FLASHWINFO(
            cbSize=ctypes.sizeof(FLASHWINFO),
            hwnd=hwnd,
            dwFlags=flashw_all | flashw_timernofg,
            uCount=3,
            dwTimeout=0,
        )
        user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
        user32.FlashWindowEx.restype = wintypes.BOOL
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception as exc:
        warn_once("flash", exc)


def default_backends() -> AlertBackends:
    return AlertBackends(beep=play_three_beep, toast=show_toast, flash=flash_console)


def _safe(backend: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:
        warn_once(backend, exc)


def _worker_loop(gen: int) -> None:
    jobs = _JOBS
    if jobs is None:
        return
    while True:
        job = jobs.get()
        if gen != _WORKER_GEN:
            # Retired while waiting. Put the job back for the live worker.
            try:
                jobs.put_nowait(job)
            except queue.Full:
                warn_once("queue", "dropped; alert worker is busy")
            jobs.task_done()
            return
        try:
            job()
        except Exception as exc:
            warn_once("worker", exc)
        finally:
            jobs.task_done()
        if gen != _WORKER_GEN:
            return


def _start_worker_locked(gen: int) -> None:
    threading.Thread(
        target=_worker_loop, args=(gen,), name="qorcap-challenge-alert", daemon=True,
    ).start()


def _ensure_worker() -> queue.Queue[Callable[[], None]]:
    global _JOBS, _WORKER_GEN
    with _WORKER_LOCK:
        if _JOBS is None:
            _JOBS = queue.Queue(maxsize=_QUEUE_MAX)
            _WORKER_GEN += 1
            _start_worker_locked(_WORKER_GEN)
        return _JOBS


def _replace_worker() -> None:
    """Drop the worker blocked behind a hung player and start one on the same queue."""
    global _WORKER_GEN
    with _WORKER_LOCK:
        if _JOBS is None:
            return
        _WORKER_GEN += 1
        _start_worker_locked(_WORKER_GEN)


def drain(timeout: float = 5.0) -> bool:
    """Wait until queued alert jobs finish. Used by tests so one sleep cannot block the next.

    ``Queue.join`` cannot time out. This waits on the same condition, on the caller
    thread, so a drain does not start a thread of its own.
    """
    jobs = _JOBS
    if jobs is None:
        return True
    deadline = time.monotonic() + timeout
    with jobs.all_tasks_done:
        while jobs.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            jobs.all_tasks_done.wait(remaining)
        return True


def _beep_bounded(fn: Callable[[], None]) -> None:
    """Run the beep off the worker. A stuck PlaySound is abandoned after the timeout.

    The player thread is a daemon and is not joined past the timeout, so it cannot
    drop later alerts. One warning is logged and the worker is replaced.
    """
    error: list[BaseException] = []

    def play() -> None:
        try:
            fn()
        except Exception as exc:
            error.append(exc)

    player = threading.Thread(target=play, name="qorcap-challenge-beep", daemon=True)
    player.start()
    player.join(_BEEP_TIMEOUT_S)
    if player.is_alive():
        warn_once("beep-timeout", "playback timed out; replacing the alert worker")
        _replace_worker()
        return
    if error:
        warn_once("beep", error[0])


def _schedule(prompt: str, *, env: Mapping[str, str] | None, backends: AlertBackends | None) -> None:
    cfg = config_from_env(os.environ if env is None else env)
    if not (cfg.beep or cfg.toast or cfg.flash):
        return
    backs = backends if backends is not None else default_backends()

    def run() -> None:
        # Toast and flash first. A stuck beep must not hold them, including the
        # ones that belong to this same alert.
        if cfg.toast:
            _safe("toast", lambda: backs.toast(prompt, cfg))
        if cfg.flash:
            _safe("flash", lambda: backs.flash(prompt, cfg))
        if cfg.beep:
            _beep_bounded(lambda: backs.beep(prompt, cfg))

    try:
        _ensure_worker().put_nowait(run)
    except queue.Full:
        warn_once("queue", "dropped; alert worker is busy")


def notify_challenge(prompt: str, *, env: Mapping[str, str] | None = None,
                     backends: AlertBackends | None = None) -> None:
    """Enqueue the enabled alerts and return. A failure here cannot reach the caller."""
    try:
        _schedule(prompt, env=env, backends=backends)
    except Exception as exc:
        warn_once("schedule", exc)
