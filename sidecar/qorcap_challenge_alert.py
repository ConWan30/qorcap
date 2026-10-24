"""Windows alerts for a live QorTime challenge. Presentation only.

The operator often plays with the QorCap console off-screen, so a challenge card
goes unseen. This module makes the same challenge audible and visible without
moving the challenge, the response window, or anything written to the bundle.

Nothing here is recorded. A miss stays unanswered. ``notify_challenge`` only
enqueues work and returns. One daemon worker runs the beep, toast, and flash.
The queue holds two jobs; a third is dropped. On any other platform the Windows
calls are skipped and this module does not raise.

A failed backend logs one warning per process on the ``qorcap.challenge_alert``
logger (stderr when nothing else is configured). It never writes a bundle file,
a checkpoint, or a receipt field.

Environment (unset means on):

* ``QORCAP_CHALLENGE_BEEP=0`` skips the three-beep. ``false`` / ``no`` / ``off`` too.
* ``QORCAP_CHALLENGE_BEEP_HZ`` tone frequency, default 2400, kept inside 200..8000.
* ``QORCAP_CHALLENGE_BEEP_VOLUME`` amplitude. A value with a decimal point is a
  fraction from 0 to 1 (``1.0`` full, ``0.5`` half). An integer is a percent
  from 0 to 100 (``100`` full, ``50`` half, ``1`` is one percent). ``0`` and
  ``0.0`` turn the beep off. Anything else, including ``1.5``, stays at full scale.
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
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_LOG = logging.getLogger("qorcap.challenge_alert")
_OFF = {"0", "false", "no", "off"}
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

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()
_WAV_CACHE: dict[tuple[int, float], bytes] = {}
_JOBS: queue.Queue[Callable[[], None]] | None = None
_WORKER_LOCK = threading.Lock()
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


def _hz(raw: str | None) -> int:
    if raw is None or not str(raw).strip():
        return DEFAULT_HZ
    try:
        hz = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_HZ
    if hz < _HZ_MIN or hz > _HZ_MAX:
        return DEFAULT_HZ
    return hz


def _volume(raw: str | None) -> float:
    """Fraction ``0..1`` if the text has a decimal point, otherwise percent ``0..100``.

    ``0`` and ``0.0`` are silence. ``1.5`` is neither a fraction nor a percent, so
    the default full scale is kept. ``1`` is one percent; full scale is ``1.0`` or ``100``.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_VOLUME
    text = str(raw).strip()
    try:
        if any(mark in text for mark in ".eE"):
            vol = float(text)
            if 0.0 <= vol <= 1.0:
                return vol
            return DEFAULT_VOLUME
        percent = int(text, 10)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    if percent < 0 or percent > 100:
        return DEFAULT_VOLUME
    return percent / 100.0


def config_from_env(env: Mapping[str, str]) -> AlertConfig:
    volume = _volume(env.get("QORCAP_CHALLENGE_BEEP_VOLUME"))
    return AlertConfig(
        beep=_enabled(env, "QORCAP_CHALLENGE_BEEP") and volume > 0.0,
        toast=_enabled(env, "QORCAP_CHALLENGE_TOAST"),
        flash=_enabled(env, "QORCAP_CHALLENGE_FLASH"),
        beep_hz=_hz(env.get("QORCAP_CHALLENGE_BEEP_HZ")),
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


def _play_wav(data: bytes) -> None:
    """Play a memory WAV on the alert worker. No-op off Windows. Logs once on failure.

    Flags are ``SND_MEMORY | SND_NODEFAULT`` and not ``SND_ASYNC``. CPython raises
    ``RuntimeError: Cannot play asynchronously from memory`` for that pair, which
    used to hide the clip and fall through to a quiet ``Beep``.
    """
    if os.name != "nt" or not data:
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
    if os.name != "nt":
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
    if os.name != "nt":
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


def _worker_loop() -> None:
    jobs = _JOBS
    if jobs is None:
        return
    while True:
        job = jobs.get()
        try:
            job()
        except Exception as exc:
            warn_once("worker", exc)
        finally:
            jobs.task_done()


def _ensure_worker() -> queue.Queue[Callable[[], None]]:
    global _JOBS
    with _WORKER_LOCK:
        if _JOBS is None:
            _JOBS = queue.Queue(maxsize=_QUEUE_MAX)
            threading.Thread(target=_worker_loop, name="qorcap-challenge-alert", daemon=True).start()
        return _JOBS


def drain(timeout: float = 5.0) -> bool:
    """Wait until queued alert jobs finish. Used by tests so one sleep cannot block the next."""
    jobs = _JOBS
    if jobs is None:
        return True
    done = threading.Event()

    def _join() -> None:
        jobs.join()
        done.set()

    threading.Thread(target=_join, name="qorcap-challenge-alert-drain", daemon=True).start()
    return done.wait(timeout)


def _schedule(prompt: str, *, env: Mapping[str, str] | None, backends: AlertBackends | None) -> None:
    cfg = config_from_env(os.environ if env is None else env)
    if not (cfg.beep or cfg.toast or cfg.flash):
        return
    backs = backends if backends is not None else default_backends()

    def run() -> None:
        if cfg.beep:
            _safe("beep", lambda: backs.beep(prompt, cfg))
        if cfg.toast:
            _safe("toast", lambda: backs.toast(prompt, cfg))
        if cfg.flash:
            _safe("flash", lambda: backs.flash(prompt, cfg))

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
