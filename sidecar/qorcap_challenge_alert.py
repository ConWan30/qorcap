"""Windows alerts for a live QorTime challenge. Presentation only.

The operator often plays with the QorCap console off-screen, so a challenge card
goes unseen. This module makes the same challenge audible and visible without
moving the challenge, the response window, or anything written to the bundle.

Nothing here is recorded. A miss stays unanswered. ``notify_challenge`` returns
as soon as the work is queued. The beep, toast, and flash run on a daemon thread
and each one swallows its own errors. On any other platform the Windows calls
are skipped and this module does not raise.

Environment (unset means on):

* ``QORCAP_CHALLENGE_BEEP=0`` skips the three-beep. ``false`` / ``no`` / ``off`` too.
* ``QORCAP_CHALLENGE_BEEP_HZ`` tone frequency, default 2400, kept inside 200..8000.
* ``QORCAP_CHALLENGE_BEEP_VOLUME`` sample scale. ``1`` (default) is full int16
  amplitude. ``0.5`` is half. A number above 1 is a percent (``50`` -> half).
* ``QORCAP_CHALLENGE_TOAST=0`` skips the toast.
* ``QORCAP_CHALLENGE_FLASH=0`` skips flashing the console window and its taskbar button.

The existing ``winsound.MessageBeep`` in the challenge path is separate and still runs.
"""
from __future__ import annotations

import array
import io
import math
import os
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass

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

# PlaySound SND_MEMORY reads the buffer while the sound plays. Keep the last few
# clips alive so a return from the alert thread cannot free them mid-beep.
_HELD_WAVS: list[bytes] = []
_TOAST_PROCS: list[subprocess.Popen] = []

# PowerShell's own AUMID. An unpackaged console has no toast identity; this one
# often works, and a failure is ignored.
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
    if raw is None or not str(raw).strip():
        return DEFAULT_VOLUME
    try:
        vol = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    if vol > 1.0:
        vol = vol / 100.0
    if vol < 0.0 or vol > 1.0:
        return DEFAULT_VOLUME
    return vol


def config_from_env(env: Mapping[str, str]) -> AlertConfig:
    return AlertConfig(
        beep=_enabled(env, "QORCAP_CHALLENGE_BEEP"),
        toast=_enabled(env, "QORCAP_CHALLENGE_TOAST"),
        flash=_enabled(env, "QORCAP_CHALLENGE_FLASH"),
        beep_hz=_hz(env.get("QORCAP_CHALLENGE_BEEP_HZ")),
        beep_volume=_volume(env.get("QORCAP_CHALLENGE_BEEP_VOLUME")),
    )


def render_three_beep_wav(hz: int, volume: float) -> bytes:
    """Three high tones separated by silence. Peak sample is full int16 scale at volume 1.

    A sine at ±32767 is the loudest tone this format can hold. ``winsound.Beep`` is a
    fallback only: on some machines it is quiet or missing, and it does not take a volume.
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


def _retain_wav(data: bytes) -> None:
    _HELD_WAVS.append(data)
    del _HELD_WAVS[:-4]


def _play_wav_async(data: bytes, hz: int) -> None:
    """Play a WAV without waiting. No-op off Windows. Never raises.

    ``SND_ASYNC | SND_MEMORY`` returns as soon as playback is queued. ``SND_NODEFAULT``
    avoids substituting a quiet system sound when the clip cannot be played. If that
    call fails, three ``Beep``s run on this same (already background) thread.
    """
    if os.name != "nt" or not data:
        return
    try:
        import winsound
        winsound.PlaySound(data, winsound.SND_ASYNC | winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        return
    except Exception:
        pass
    try:
        import winsound
        for _ in range(_BEEPS):
            winsound.Beep(int(hz), 160)
            time.sleep(0.08)
    except Exception:
        return


def play_three_beep(prompt: str, cfg: AlertConfig) -> None:
    del prompt
    data = render_three_beep_wav(cfg.beep_hz, cfg.beep_volume)
    _retain_wav(data)
    _play_wav_async(data, cfg.beep_hz)


def _toast_text(prompt: str) -> str:
    text = " ".join(str(prompt).split())
    if len(text) > 240:
        text = text[:237] + "..."
    return text or "QorCap challenge"


def show_toast(prompt: str, cfg: AlertConfig) -> None:
    """Toast via WinRT in a hidden PowerShell process. No-op off Windows. Never raises."""
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
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _TOAST_SCRIPT],
            **kwargs,
        )
        _TOAST_PROCS.append(proc)
        del _TOAST_PROCS[:-4]
        for old in _TOAST_PROCS:
            old.poll()
    except Exception:
        return


def flash_console(prompt: str, cfg: AlertConfig) -> None:
    """Flash the console caption and its taskbar button until the window is focused.

    ``FLASHW_ALL`` is the caption plus the taskbar button. ``FLASHW_TIMERNOFG`` keeps
    going until the operator focuses the console. No-op off Windows, or when this
    process has no console. Never raises.
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
    except Exception:
        return


def default_backends() -> AlertBackends:
    return AlertBackends(beep=play_three_beep, toast=show_toast, flash=flash_console)


def _safe(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        return


def _schedule(prompt: str, *, env: Mapping[str, str] | None, backends: AlertBackends | None) -> None:
    cfg = config_from_env(os.environ if env is None else env)
    if not (cfg.beep or cfg.toast or cfg.flash):
        return
    backs = backends if backends is not None else default_backends()

    def run() -> None:
        if cfg.beep:
            _safe(lambda: backs.beep(prompt, cfg))
        if cfg.toast:
            _safe(lambda: backs.toast(prompt, cfg))
        if cfg.flash:
            _safe(lambda: backs.flash(prompt, cfg))

    threading.Thread(target=run, name="qorcap-challenge-alert", daemon=True).start()


def notify_challenge(prompt: str, *, env: Mapping[str, str] | None = None,
                     backends: AlertBackends | None = None) -> None:
    """Queue the enabled alerts and return. A failure here cannot reach the caller."""
    try:
        _schedule(prompt, env=env, backends=backends)
    except Exception:
        return
