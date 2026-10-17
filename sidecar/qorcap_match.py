"""One-click QorCap match: record + scorebug + live local countersign + signed QorTime receipt.

    python sidecar/qorcap_match.py [--test-pattern] [--qortime DIR] [--qorcap PATH] [--out DIR]
    python sidecar/qorcap_match.py finalize [BUNDLE] [--qortime DIR]     (offline, after the fact)

Started by ``Start Match.cmd`` on Windows. It:
  1. finds a qortime checkout (``--qortime``, ``$QORTIME``, ``qortime/`` next to QorCap, or downloads the
     pinned zip below once into the user's QorCap data folder);
  2. starts ``qorcap record --bundle`` (USB3.0 Video, 1280x720, 30 fps, 90 s replay buffer) and the
     scorebug reader (QorTime's own ScorebugReader + detect_final) in this process, showing one status line;
  3. countersigns every QorCap checkpoint as it appears, using QorTime's ``checkpoints.countersign``
     with a LOCAL referee key (there is no public QorTime referee server yet);
  4. stops on final_basis, Enter/Q, or Ctrl+C; QorCap finalises the bundle;
  5. builds the receipt with QorTime's ``receipt.build`` / ``sign_referee`` / ``sign_player`` using a device
     key (QorTime ``FileKey``), writes receipt.json, then runs ``qortime verify`` and ``qorcap verify-bundle``.

Offline ``finalize`` (also ``qorcap finalize`` / ``Finalize Last Match.cmd``): decodes a bundle's recorded
video.mp4 at 4 fps, runs the same QorTime reader + detect_final over it, writes timeline.offline.jsonl,
score_checkpoints.jsonl (QorTime-shaped hash chain bound to QorCap's video chain) and final.offline.json, then
signs receipt.json exactly like Start Match. If Start Match countersigned live (countersigned.jsonl), those
signatures are reused; otherwise the local referee countersigns QorCap's checkpoints now, stamping each with
QorCap's own recording time, and the referee ``key_kind`` says ``local-self-attested-offline``.

Trust: both keys live on this PC, so the result is SELF-ATTESTED. The receipt's computed QorTime tier is
``single-attested`` and ``referee.key_kind`` is ``local-self-attested`` so any reader can see that the
"referee" is this machine, not an independent server. Keys are never printed.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

QORTIME_PIN = "a57aa3db47be5261b99b0b2c5968faee2a221cde"
QORTIME_ZIP = f"https://github.com/ConWan30/qortime/archive/{QORTIME_PIN}.zip"
VERIFY_URL = "https://conwan30.github.io/qortime/verify/"
REFEREE_KIND = "local-self-attested"
REFEREE_KIND_OFFLINE = "local-self-attested-offline"
CAPTURE_APPS = ("obs64.exe", "obs32.exe", "obs.exe")
HERE = Path(__file__).resolve().parent
WIN = sys.platform == "win32"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def data_dir() -> Path:
    base = os.getenv("QORCAP_HOME")
    if base:
        return Path(base)
    if WIN:
        return Path(os.getenv("LOCALAPPDATA") or Path.home()) / "QorCap"
    return Path.home() / ".qorcap"


# ------------------------------------------------------------------ prerequisites
def find_qortime(arg: str | None, download: bool = True) -> Path:
    cands = [arg, os.getenv("QORTIME"), HERE.parent / "qortime", data_dir() / f"qortime-{QORTIME_PIN}"]
    for c in cands:
        if c and (Path(c) / "qor_receipt" / "receipt.py").exists():
            return Path(c).resolve()
    if not download:
        raise SystemExit("QorTime code not found. Pass --qortime C:\\path\\to\\qortime")
    dst = data_dir()
    dst.mkdir(parents=True, exist_ok=True)
    say(f"Downloading QorTime (pinned {QORTIME_PIN[:7]}) ...")
    try:
        with urllib.request.urlopen(QORTIME_ZIP, timeout=60) as r:
            zipfile.ZipFile(io.BytesIO(r.read())).extractall(dst)
    except Exception as e:
        raise SystemExit(f"Could not download QorTime ({e}). Check the internet connection and try again.")
    out = dst / f"qortime-{QORTIME_PIN}"
    if not (out / "qor_receipt" / "receipt.py").exists():
        raise SystemExit(f"QorTime download looks incomplete: {out}")
    return out


def find_qorcap(arg: str | None) -> str:
    exe = "qorcap.exe" if WIN else "qorcap"
    for c in [arg, HERE.parent / exe, HERE.parent / "target" / "release" / exe, HERE.parent / "target" / "debug" / exe, shutil.which("qorcap")]:
        if c and Path(c).exists():
            return str(c)
    raise SystemExit("qorcap program not found next to this script.")


def check_python_deps(need_vision: bool) -> list[str]:
    missing = []
    mods = ["cryptography"] + (["numpy", "cv2"] if need_vision else [])
    for m in mods:
        try:
            __import__(m)
        except ImportError:
            missing.append({"cv2": "opencv-python-headless"}.get(m, m))
    return missing


def restrict_acl(p: Path) -> None:
    """Key files: owner-only. POSIX: QorTime's FileKey already writes 0600. Windows: icacls."""
    if WIN:
        user = os.getenv("USERNAME", "")
        subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    else:
        os.chmod(p, 0o600)


def load_keys(key_dir: Path):
    from qor_receipt.keys import FileKey  # QorTime's own persistent Ed25519 key (vendored Qoresence signing)

    key_dir.mkdir(parents=True, exist_ok=True)
    if not WIN:
        os.chmod(key_dir, 0o700)
    keys = {}
    for name in ("device", "referee"):
        p = key_dir / f"{name}.key"
        first = not p.exists()
        keys[name] = FileKey(p)
        if first:
            restrict_acl(p)
            say(f"Created your {name} key (stored privately in {key_dir}; it is never shown).")
    return keys["device"], keys["referee"]


# ------------------------------------------------------------------ QorCap API
class Api:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path: str, timeout: float = 3):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.loads(r.read())

    def post(self, path: str, body: dict | None = None, timeout: float = 5):
        rq = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(),
                                    headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}


class Countersigner:
    """Local referee: countersigns each QorCap checkpoint at the moment it is first seen."""

    def __init__(self, key, match_id: str, role: str, clock=lambda: int(time.time() * 1000), sink: Path | None = None) -> None:
        self.key, self.match_id, self.role, self.clock, self.sink = key, match_id, role, clock, sink
        self.signed: list[dict] = []

    def offer(self, cp: dict) -> bool:
        from qor_receipt.checkpoints import body_of, countersign

        if cp["seq"] != len(self.signed):
            return False
        body = body_of(cp)
        body["hash"] = cp["hash"]
        ts = self.clock() if not callable(getattr(self, "ts_of", None)) else self.ts_of(cp)
        self.signed.append(countersign(self.key, self.match_id, self.role, body, ts))
        if self.sink is not None:
            with open(self.sink, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.signed[-1]) + "\n")
        return True


STATIC_FRAME_S = 1.0
SHUTDOWN_TIMEOUT_S = 900  # input proof decodes the whole video once; never kill QorCap mid-bundle


def countersign_new_from_file(path: Path, cs: "Countersigner") -> int:
    """Offer every not-yet-signed checkpoint in QorCap's checkpoints.jsonl (tolerates a partial last line)."""
    n = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except ValueError:
            break  # line still being written
        if c.get("seq", -1) >= len(cs.signed) and cs.offer(c):
            n += 1
    return n


def wait_and_countersign(proc, path: Path, cs: "Countersigner", timeout: float = SHUTDOWN_TIMEOUT_S,
                         poll_s: float = 0.2, sweep=None, report=None, on_tick=None) -> bool:
    """Wait for QorCap to exit, countersigning its stop checkpoints the moment they appear.

    v0.3.3: QorCap seals its chain with a ``stop`` checkpoint right after the video is finalised and keeps its
    API up while it computes the input proof. ``sweep`` pulls new checkpoints over the API (the path that
    worked live); the file is a fallback. ``report(seq)`` tells QorCap how far we have signed so it can start
    the slow bundle work only after the tail carries fresh referee stamps.
    """
    deadline = time.monotonic() + timeout
    reported = -1
    while proc.poll() is None:
        if sweep is not None:
            try:
                sweep()
            except Exception:
                pass
        countersign_new_from_file(path, cs)
        if report is not None and len(cs.signed) - 1 > reported:
            try:
                report(len(cs.signed) - 1)
                reported = len(cs.signed) - 1
            except Exception:
                pass
        if on_tick is not None:
            on_tick()
        if time.monotonic() > deadline:
            proc.kill()
            break
        time.sleep(poll_s)
    countersign_new_from_file(path, cs)
    return proc.returncode == 0


def build_receipt(draft: dict, cps: list[dict], device, referee, *, match_id: str, role: str, started_ms: int,
                  referee_kind: str = REFEREE_KIND) -> dict:
    """QorTime's receipt.build + sign_referee + sign_player on what QorCap observed."""
    from qor_receipt import receipt as R

    m = dict(draft["match"], match_id=match_id, started_ms=started_ms,
             ended_ms=max(cps[-1]["referee_ts_ms"] if cps else started_ms, started_ms))
    player = dict(draft["players"][0], role=role, pubkey=device.pubkey_hex, key_kind=device.kind)
    rec = R.build(match=m, result=draft["result"], players=[player],
                  referee={"pubkey": referee.pubkey_hex, "key_id": referee.key_id, "key_kind": referee_kind},
                  challenges=draft.get("challenges", []), checkpoints={role: cps},
                  agreement=draft.get("agreement") or {"timelines_agree": None, "mismatches": []},
                  signals=draft.get("signals") or {"motion_permille": {role: None}})
    rec = R.sign_referee(rec, referee)
    return R.sign_player(rec, role, device)


# ------------------------------------------------------------------ scorebug sources
def start_reader(api: Api, stop: threading.Event, state: dict, bundle: Path | None = None) -> threading.Thread:
    import hashlib

    import cv2
    import numpy as np
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    sys.path.insert(0, str(HERE))
    from qorcap_endmenu import EndMenuReader
    from qorcap_reader import ReaderCore, madden_final_screen, prep

    def post(b):
        try:
            r = api.post("/events", b)
        except Exception:
            r = {}
        if b.get("type") == "final":
            state["final"] = b
        return r

    from qorcap_mnp import AutoProfileReader
    from qorcap_reader import collect_delta_corroboration, load_delta_corroboration_from_bundle
    core = ReaderCore(post, AutoProfileReader(ScorebugReader(), prep=prep), AgreementWindow(), PauseDetector(), detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=EndMenuReader())
    state["core"] = core
    if bundle is not None:
        try:
            core.delta_corroboration = load_delta_corroboration_from_bundle(bundle)
        except Exception:
            core.delta_corroboration = []

    def _refresh_corr():
        """Pull score-change clip hashes from /timeline + re-read haptics/inputs from the live bundle."""
        clips = []
        try:
            tl = api.get("/timeline", timeout=1)
            clips = list(tl.get("clips") or []) if isinstance(tl, dict) else []
        except Exception:
            pass
        from_bundle = []
        if bundle is not None:
            try:
                from_bundle = load_delta_corroboration_from_bundle(bundle)
            except Exception:
                from_bundle = []
        live_clips = collect_delta_corroboration(clips=clips)
        # Prefer richer bundle-derived list; merge unique clip sha256s from live API
        seen = {(e.get("kind"), e.get("sha256") or e.get("t_ms")) for e in from_bundle}
        merged = list(from_bundle)
        for e in live_clips:
            key = (e.get("kind"), e.get("sha256") or e.get("t_ms"))
            if key not in seen:
                merged.append(e)
                seen.add(key)
        core.delta_corroboration = merged

    def loop():
        last, last_t, last_corr = None, 0.0, 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now - last_corr >= 1.0:
                last_corr = now
                try:
                    _refresh_corr()
                except Exception as e:
                    state["corr_error"] = f"{type(e).__name__}: {e}"
            try:
                with urllib.request.urlopen(api.base + "/snapshot.jpg", timeout=3) as r:
                    data = r.read()
            except Exception:
                time.sleep(0.5)
                continue
            h = hashlib.sha256(data).digest()
            # A perfectly static screen (end-of-game menu) can produce byte-identical snapshots; still feed
            # it about once a second so time-based spans (final_screen >= 3 s) can grow.
            if h != last or time.monotonic() - last_t >= STATIC_FRAME_S:
                last, last_t = h, time.monotonic()
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    try:
                        core.on_frame(int(time.time() * 1000), img)
                    except Exception as e:  # never let one bad frame kill the reader thread
                        state["reader_error"] = f"{type(e).__name__}: {e}"
            time.sleep(0.25)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def start_fake_scores(api: Api, stop: threading.Event, state: dict, secs: float) -> threading.Thread:
    """--test-pattern: plays a short scripted game into /events (no card, no PS5)."""
    script = [(0, 0, 1, 900), (7, 0, 1, 600), (7, 3, 2, 300), (14, 3, 3, 120), (14, 10, 4, 30), (14, 10, 4, 0)]

    def loop():
        step = secs / (len(script) + 1)
        for h, a, q, c in script:
            if stop.wait(step):
                return
            for _ in range(2):
                try:
                    api.post("/events", {"type": "score", "home": h, "away": a, "quarter": q, "clock_s": c, "clock_red": c == 0})
                except Exception:
                    pass
        if stop.wait(1.0):
            return
        fin = {"type": "final", "basis": "clock_zero", "home": 14, "away": 10, "quarter": 4}
        try:
            api.post("/events", fin)
        except Exception:
            pass
        state["final"] = fin

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ apps holding the card
def capture_apps_running() -> list[str]:
    """obs64.exe etc. hold a USB capture card exclusively (Windows)."""
    if not WIN:
        return []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10).stdout.lower()
    except Exception:
        return []
    return [n for n in CAPTURE_APPS if f'"{n}"' in out]


def offer_close_capture_apps(ask=input) -> bool:
    """Returns True when nothing is holding the card (anymore)."""
    apps = capture_apps_running()
    if not apps:
        return True
    say("")
    say("  Close OBS. It's using the capture card.  (" + ", ".join(apps) + " is running)")
    try:
        ans = ask("  Close it for you now? [Y/n] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("", "y", "yes"):
        say("  OK. Close OBS yourself, then recording will start once the card is free.")
        return False
    for n in apps:
        subprocess.run(["taskkill", "/IM", n], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    for _ in range(20):
        if not capture_apps_running():
            say("  OBS closed.")
            return True
        time.sleep(0.5)
    try:
        ans = ask("  OBS did not close (it may be asking a question). Force-close it? [y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans in ("y", "yes"):
        for n in apps:
            subprocess.run(["taskkill", "/F", "/IM", n], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        time.sleep(1.5)
    ok = not capture_apps_running()
    say("  OBS closed." if ok else "  OBS is still running; the capture card may not open.")
    return ok


# ------------------------------------------------------------------ robustness
def ignore_ctrl_c() -> None:
    import signal
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass


MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def mic_privacy_blocked(read=None) -> str | None:
    """Windows privacy switches that silently block the capture card's audio (DirectShow 'I/O error').

    Returns which switch is off ('desktop' / 'all') or None. ``read(subkey) -> Value str | None`` is injectable."""
    if read is None:
        if not WIN:
            return None

        def read(sub):
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                    return str(winreg.QueryValueEx(k, "Value")[0])
            except OSError:
                return None
    if (read(MIC_KEY) or "").lower() == "deny":
        return "all"
    if (read(MIC_KEY + r"\NonPackaged") or "").lower() == "deny":
        return "desktop"
    return None


def mic_hint(which: str | None) -> list[str]:
    if not which:
        return []
    what = ("'Microphone access'" if which == "all" else "'Let desktop apps access your microphone'")
    return ["",
            f"  NOTE: Windows privacy blocks the microphone ({what} is Off).",
            "        The capture card's game audio counts as a microphone, so the match would record WITHOUT sound.",
            "        Fix: Start > Settings > Privacy & security > Microphone > turn ON 'Microphone access'",
            "        and 'Let desktop apps access your microphone'. Then close this window and start again.",
            "        (Recording still works video-only if you continue.)", ""]


# ------------------------------------------------------------------ audio level checks (v0.3.6)
SILENT_MAX_DB = -70.0  # a capture card passing no sound gives ~-90 dB mean / -80 dB max (+-1 LSB noise)


def parse_volumedetect(text: str) -> dict | None:
    import re
    mean = re.findall(r"mean_volume:\s*(-?[\d.]+|-inf) dB", text)
    mx = re.findall(r"max_volume:\s*(-?[\d.]+|-inf) dB", text)
    if not mean or not mx:
        return None
    f = lambda v: -200.0 if v == "-inf" else float(v)  # noqa: E731
    return {"mean_db": f(mean[-1]), "max_db": f(mx[-1]), "silent": f(mx[-1]) <= SILENT_MAX_DB}


def dshow_audio_devices(ffmpeg: str) -> list[str]:
    import re
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                             capture_output=True, text=True, timeout=20, errors="replace")
    except Exception:
        return []
    return re.findall(r'"([^"]+)" \(audio\)', out.stderr or "")


def pick_card_audio(video: str, want: str, devs: list[str]) -> str | None:
    """Same intent as the recorder's pick: an explicit name, else the audio device sharing the card's name tag
    (e.g. 'USB3.0' for 'USB3.0 Video' -> 'Digital Audio Interface (USB3.0 Audio)')."""
    if want not in ("auto", "", None):
        return want
    tag = video.split()[0].lower() if video else ""
    return next((d for d in devs if tag and tag in d.lower()), None)


def probe_card_audio(ffmpeg: str, device: str, secs: float = 4.0) -> dict | None:
    """Short DirectShow capture of the card's audio through volumedetect (run BEFORE the recorder opens the card)."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-f", "dshow", "-i", f"audio={device}", "-t", str(secs),
                              "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True,
                             timeout=secs + 25, errors="replace")
    except Exception as e:
        return {"error": str(e)}
    r = parse_volumedetect(out.stderr or "")
    return r if r else {"error": (out.stderr or "")[-300:]}


def recording_audio_level(ffmpeg: str, video: Path) -> dict | None:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=900, errors="replace")
    except Exception as e:
        return {"error": str(e)}
    if "Audio:" not in (out.stderr or ""):
        return {"no_audio_stream": True, "silent": True}
    return parse_volumedetect(out.stderr or "") or {"error": "volumedetect gave no result"}


def audio_silence_hint(res: dict | None, *, when: str) -> list[str]:
    if not res or not res.get("silent"):
        return []
    lvl = "no audio track" if res.get("no_audio_stream") else f"max {res['max_db']:.1f} dB, mean {res['mean_db']:.1f} dB"
    return ["",
            "  !" * 21,
            f"  WARNING: the capture card's audio is SILENT {when} ({lvl}).",
            "  The match video will have NO GAME SOUND. Usual causes, in order:",
            "   1. PS5: Settings > Sound > Audio Output > Output Device = your TV/HDMI device, and",
            "      'Output to Headphones' = 'Chat Audio' (with 'All Audio' the PS5 sends NO sound over HDMI",
            "      while a headset is plugged into the controller).",
            "   2. PS5 is on the home screen/muted, or the game volume is 0.",
            "   3. Windows: Settings > System > Sound > Input > 'Digital Audio Interface (USB3.0 Audio)' >",
            "      Input volume 100 and not muted (More sound settings > Recording > Properties > Levels).",
            "   4. The capture card's HDMI-out passthrough/TV path: some cards only carry audio over USB when",
            "      the PS5 outputs stereo PCM: PS5 Settings > Sound > Audio Output > Audio Format = Linear PCM.",
            "  (Recording continues; fix the setting and restart the match to get sound.)",
            "  !" * 21, ""]


def report_recording_audio(bundle: Path, ffmpeg: str | None) -> dict | None:
    """Level of the recorded game audio -> audio_check.json + a loud warning when silent."""
    vids = video_files(bundle)
    if not ffmpeg or not vids:
        return None
    res = recording_audio_level(ffmpeg, vids[0])
    (bundle / "audio_check.json").write_text(json.dumps(dict(res or {}, video=vids[0].name), indent=2), encoding="utf-8")
    if res and "max_db" in res:
        say(f"  recorded game audio: max {res['max_db']:.1f} dB, mean {res['mean_db']:.1f} dB" + ("  SILENT" if res["silent"] else "  OK"))
    for line in audio_silence_hint(res, when="in this recording"):
        say(line)
    return res


def key_watcher(stop: threading.Event, state: dict) -> None:
    def loop():
        try:
            if WIN:
                import msvcrt
                while not stop.is_set():
                    if msvcrt.kbhit() and msvcrt.getwch() in "\r\nqQ":
                        state["user_stop"] = True
                        return
                    time.sleep(0.1)
            elif sys.stdin and sys.stdin.isatty():
                import select
                while not stop.is_set():
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        sys.stdin.readline()
                        state["user_stop"] = True
                        return
        except Exception:
            pass
    threading.Thread(target=loop, daemon=True).start()


def fmt_clock(s) -> str:
    return "--:--" if s is None else f"{int(s) // 60}:{int(s) % 60:02d}"


def status_line(st: dict, cps: dict, signed: int) -> str:
    sc = cps.get("score") or {}
    score = (f"AWAY {sc.get('away')} - HOME {sc.get('home')}  Q{sc.get('quarter')} {fmt_clock(sc.get('clock_s'))}"
             if sc else "score: waiting for scorebug")
    state = {"capturing": "REC", "waiting_for_device": "NO SIGNAL (check card/PS5)", "reopening": "reconnecting",
             "disk_full": "DISK FULL"}.get(st.get("state"), st.get("state", "?"))
    warn = st.get("warning") or ""
    warn_bit = f" | ! {warn[:40]}" if warn else ""
    disk = st.get("disk_free_mb")
    disk_bit = f" | disk {disk}MB" if isinstance(disk, int) else ""
    return (f"[{state}] {int(st.get('recording_secs') or 0) // 60:02d}:{int(st.get('recording_secs') or 0) % 60:02d}"
            f" | {score} | segments {cps.get('segments', 0)} | signed {signed} | clips {cps.get('clips', 0)}"
            f"{disk_bit}{warn_bit}")


def run_cmd(cmd: list[str], env=None) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr).strip()


def open_folder(p: Path) -> None:
    try:
        if WIN:
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open") and os.getenv("DISPLAY"):
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


EXIT_CODES = {"VERIFIED": 0, "SELF_ATTESTED": 0, "PARTIAL": 0, "FAIL": 2}  # verdicts: contracts/compat.json


def overall(qt_status: str, bundle_ok: bool) -> str:
    if not bundle_ok or qt_status == "FAIL":
        return "FAIL"
    return qt_status


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "finalize" in argv:  # `qorcap_match.py [--qorcap X ...] finalize [BUNDLE] [opts]` (launcher puts its opts first)
        argv.remove("finalize")
        return finalize_main(argv)
    if "submit" in argv:  # `qorcap_match.py [--qorcap X ...] submit [BUNDLE] [--full]` -> QorTime official referee
        argv.remove("submit")
        from qorcap_submit import main as submit_main
        return submit_main(argv)
    ap = argparse.ArgumentParser(description="QorCap one-click match")
    ap.add_argument("--test-pattern", action="store_true", help="no capture card: synthetic video + scripted scores")
    ap.add_argument("--test-secs", type=float, default=20.0, help="test-pattern game length")
    ap.add_argument("--device", default="USB3.0 Video")
    ap.add_argument("--qortime")
    ap.add_argument("--qorcap")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--out", help="folder for match bundles (default: Documents/QorCap Matches)")
    ap.add_argument("--key-dir", help="where the device/referee keys live (default: user QorCap data folder)")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--role", choices=["home", "away"], default="home")
    ap.add_argument("--player-id", default="local:player")
    ap.add_argument("--encoder")
    ap.add_argument("--res", default="1280x720")
    ap.add_argument("--fps", default="30")
    ap.add_argument("--input-format", default="mjpeg", help="mjpeg works on USB3.0 Video at 720p30; 'auto' probes the card")
    ap.add_argument("--audio", default="auto", help="card audio device ('auto'), a device name, or 'none'")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--referee-url", default=None, help="QorTime official referee (default: the public service)")
    ap.add_argument("--no-anchors", action="store_true", help="do not timestamp the video chain with the official referee")
    ap.add_argument("--official-challenges", action="store_true",
                    help="fetch official challenge nonces at kickoff / Q3 / Q4 (press the shown pattern; needed for VERIFIED)")
    ap.add_argument("--submit", action="store_true", help="submit for verification at the end without asking")
    ap.add_argument("--no-submit-prompt", action="store_true", help="do not offer 'Submit for Verification' at the end")
    a = ap.parse_args(argv)

    say("=" * 64)
    say("  QorCap  -  Start Match" + ("   (TEST PATTERN, no capture card)" if a.test_pattern else ""))
    say("=" * 64)
    qt = find_qortime(a.qortime)
    sys.path.insert(0, str(qt))
    miss = check_python_deps(need_vision=not a.test_pattern)
    if miss:
        say(f"Missing Python packages: {' '.join(miss)}\n  Fix: py -m pip install {' '.join(miss)}")
        return 3
    qorcap = find_qorcap(a.qorcap)
    device, referee = load_keys(Path(a.key_dir) if a.key_dir else data_dir() / "keys")
    if not a.test_pattern and a.audio != "none":
        for line in mic_hint(mic_privacy_blocked()):
            say(line)

    root = Path(a.out) if a.out else Path.home() / "Documents" / "QorCap Matches"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bundle = (root / f"match-{stamp}").resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    match_id = f"local-{stamp}"
    api = Api(a.port)
    try:
        api.get("/status", timeout=1)
        say("QorCap is already running (another Start Match window?). Close it first.")
        return 4
    except Exception:
        pass
    # Also refuse if a prior instance left a live lock for this port (covers the race before /status is up).
    lock = data_dir() / f"qorcap-{a.port}.instance"
    if lock.exists():
        try:
            pid = int((lock.read_text(encoding="utf-8").strip() or "0").split()[0])
        except ValueError:
            pid = 0
        alive = False
        if pid > 0:
            try:
                if WIN:
                    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                         capture_output=True, text=True, timeout=5)
                    alive = str(pid) in (out.stdout or "") and "INFO:" not in (out.stdout or "")
                else:
                    os.kill(pid, 0)
                    alive = True
            except OSError:
                alive = False
            except Exception:
                alive = False
        if alive:
            say(f"QorCap is already running (pid {pid}). Close that Start Match window first.")
            return 4

    cmd = [qorcap, "--port", str(a.port)] + (["--ffmpeg", a.ffmpeg] if a.ffmpeg else []) + [
        "record", "--device", "testpattern" if a.test_pattern else a.device, "--res", a.res, "--fps", a.fps,
        "--bundle", str(bundle), "--replay", "90", "--match-id", match_id, "--role", a.role, "--player-id", a.player_id]
    if a.encoder or a.test_pattern:
        cmd += ["--encoder", a.encoder or "libx264"]
    if a.test_pattern:
        cmd += ["--audio", "none"]
    else:
        cmd += ["--input-format", a.input_format, "--audio", a.audio]
        offer_close_capture_apps()
        if WIN and a.audio != "none":  # v0.3.6: pre-match card audio level check (loud warning when silent)
            ff = a.ffmpeg or next((str(c) for c in [HERE.parent / "ffmpeg.exe"] if c.exists()), None) or shutil.which("ffmpeg")
            dev = pick_card_audio(a.device, a.audio, dshow_audio_devices(ff)) if ff else None
            if dev:
                say(f"Checking game audio on '{dev}' (4 s) ...")
                res = probe_card_audio(ff, dev)
                (bundle / "audio_precheck.json").write_text(json.dumps(dict(res or {}, device=dev), indent=2), encoding="utf-8")
                if res and "max_db" in res:
                    say(f"  level: max {res['max_db']:.1f} dB, mean {res['mean_db']:.1f} dB" + ("  SILENT" if res["silent"] else "  OK"))
                for line in audio_silence_hint(res, when="right now"):
                    say(line)
    log = open(bundle / "console.log", "w", encoding="utf-8")
    started_ms = int(time.time() * 1000)
    # Own process group on Windows: a Ctrl+C in this window reaches only this script, which then stops QorCap
    # cleanly over its API (instead of both racing, and cmd.exe asking 'Terminate batch job (Y/N)?' mid-bundle).
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if WIN else 0
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    for _ in range(50):
        try:
            api.get("/status", timeout=1)
            break
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    if proc.poll() is not None:
        log_tail = ""
        try:
            log_tail = (bundle / "console.log").read_text(encoding="utf-8", errors="replace")[-800:]
        except Exception:
            pass
        if proc.returncode == 4 or "already running" in log_tail.lower():
            say("QorCap is already running (another Start Match window?). Close it first.")
            return 4
        say(f"QorCap could not start. Details: {bundle / 'console.log'}")
        say("Common fixes: plug the capture card into a blue USB 3 port, close OBS/Camera apps, free disk space (~2 GB), run Start Match again.")
        say("You can sign an already-recorded match later with 'Finalize Last Match'.")
        return 5

    say(f"Recording to: {bundle}")
    say("Play your game. Recording stops by itself at the final whistle.")
    say("Press ENTER (or Q) to stop early. Keep this window open until it says RESULT.")
    say("(Ctrl+C also stops it; if Windows then asks 'Terminate batch job (Y/N)?', answer N.)\n")
    stop, state = threading.Event(), {}
    cs = Countersigner(referee, match_id, a.role, sink=bundle / "countersigned.jsonl")
    if a.test_pattern:
        start_fake_scores(api, stop, state, a.test_secs)
    else:
        start_reader(api, stop, state, bundle=bundle)
    key_watcher(stop, state)
    live = None
    if not a.no_anchors or a.official_challenges:
        try:
            from qorcap_submit import OFFICIAL_URL, OfficialLive, start_official_live
            live = OfficialLive(a.referee_url or OFFICIAL_URL, bundle, match_id, a.role, api, challenges=a.official_challenges)
            start_official_live(live, stop)
            if a.official_challenges:
                say("Official QorTime challenges are ON: when a CHALLENGE line appears, press OPTIONS and the shown buttons.\n")
        except Exception as e:  # never block a recording on the network helper
            say(f"(official referee link unavailable: {e})")

    reported = {"seq": -1}

    def report(seq: int) -> None:
        api.post("/countersigned", {"seq": seq}, timeout=2)
        reported["seq"] = seq

    def sweep():
        try:
            got = api.get(f"/checkpoints?since={len(cs.signed)}")
        except Exception:
            return None
        for c in got.get("checkpoints", []):
            cs.offer(c)
        if len(cs.signed) - 1 > reported["seq"]:
            try:
                report(len(cs.signed) - 1)
            except Exception:
                pass
        return got

    final_seen_at = None
    try:
        while proc.poll() is None:
            got = sweep()
            try:
                st = api.get("/status")
                tl = api.get("/timeline")
            except Exception:
                st, tl = {}, {}
            info = {"segments": (got or {}).get("segments", 0),
                    "score": (tl.get("score") if isinstance(tl, dict) else None),
                    "clips": len(tl.get("clips", [])) if isinstance(tl, dict) else 0}
            sys.stdout.write("\r" + status_line(st, info, len(cs.signed)).ljust(110))
            sys.stdout.flush()
            f = state.get("final")
            if f and is_end_menu_final(f) and not state.get("end_menu_seen"):
                state["end_menu_seen"] = True
                final_seen_at = time.monotonic()
                say(f"\n\nFINAL (end-of-game menu): AWAY {f['away']} - HOME {f['home']}. Saving final clip ..."
                    + (f"\n  note: {f['notes'][0]}" if f.get("notes") else ""))
            elif f and final_seen_at is None:
                final_seen_at = time.monotonic()
                say(f"\n\nFINAL on the scorebug ({f['basis']}): AWAY {f['away']} - HOME {f['home']}. Waiting up to "
                    f"{END_MENU_WAIT_S} s for the end-of-game menu to confirm it (stay on that menu; do not RESTART) ...")
            if state.get("user_stop"):
                say("\n\nStopping (you pressed a key) ...")
                break
            if final_seen_at and time.monotonic() - final_seen_at > (4 if state.get("end_menu_seen") else END_MENU_WAIT_S):
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("\n\nStopping (Ctrl+C) ...")
    stop.set()
    ignore_ctrl_c()  # from here on, a Ctrl+C must not abort signing (it caused 'receipt failed' + Y/N prompt)
    say("\n\nStopping QorCap: sealing and countersigning the final checkpoints ...")
    sweep()
    try:
        api.post("/shutdown")
    except Exception:
        pass
    # Countersign the stop checkpoints over the API while QorCap is still up; QorCap waits for our report
    # before the input-proof analysis (v0.3.2 stamped the tail ~105 s late: gap_too_long + clock_skew).
    phase = {"last": None}

    def tick():
        try:
            st = api.get("/status", timeout=1)
        except Exception:
            return
        s_ = st.get("state")
        if s_ != phase["last"]:
            phase["last"] = s_
            msg = {"countersigning": "  final checkpoints: countersigning ...",
                   "finishing": f"  final checkpoints countersigned ({len(cs.signed)}). Checking controller input against the video (1-2 minutes, please wait) ...",
                   "stopped": "  QorCap finished the match bundle."}.get(s_)
            if msg:
                say(msg)
    wait_and_countersign(proc, bundle / "checkpoints.jsonl", cs, timeout=SHUTDOWN_TIMEOUT_S, sweep=sweep, on_tick=tick)
    log.close()

    # Countersign anything QorCap wrote while shutting down (still within seconds of t_ms).
    for line in (bundle / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines() if (bundle / "checkpoints.jsonl").exists() else []:
        if line.strip():
            c = json.loads(line)
            if c["seq"] >= len(cs.signed):
                cs.offer(c)

    say("\nFinishing up: signing the receipt ...")
    draft_p = bundle / "receipt.draft.json"
    if not draft_p.exists():
        say(f"FAIL: QorCap did not write the bundle. See {bundle / 'qorcap.log'}")
        return 2
    draft = json.loads(draft_p.read_text(encoding="utf-8"))
    # Prefer the live ReaderCore final (includes final_confirmed + delta_corroboration) when present.
    live_fin = state.get("final")
    if live_fin and is_end_menu_final(live_fin):
        draft = dict(draft, result=apply_final(draft["result"], live_fin))
    if not a.test_pattern and draft["result"].get("method") != "end_menu_read" and a.ffmpeg:
        draft = tail_final_check(bundle, draft, ffmpeg=a.ffmpeg)
    elif not a.test_pattern and draft["result"].get("method") == "end_menu_read" and draft["result"].get("final_confirmed") is not True and a.ffmpeg:
        # Stop-time fallback: re-read with bundle haptics/clips so a live-uncorroborated delta can still confirm.
        draft = tail_final_check(bundle, draft, ffmpeg=a.ffmpeg)
    if not a.test_pattern and a.audio != "none":
        report_recording_audio(bundle, a.ffmpeg or next((str(c) for c in [HERE.parent / ("ffmpeg.exe" if WIN else "ffmpeg")]
                                                         if c.exists()), None) or shutil.which("ffmpeg"))
    code = sign_and_report(bundle, draft, cs.signed, device, referee, match_id=match_id, role=a.role,
                           started_ms=started_ms, qt=qt, qorcap=qorcap, no_open=a.no_open)
    if live is not None:
        say(f"  Official anchors: {live.anchors} (link failures: {live.failures})")
    if code != 2 and (a.submit or not a.no_submit_prompt) and not a.test_pattern:
        offer_submit(bundle, a.referee_url, auto=a.submit, ask=input)
    return code


def offer_submit(bundle: Path, referee_url: str | None, *, auto: bool = False, ask=input) -> int | None:
    """End-of-match 'Submit for Verification' prompt. Uploading needs an explicit yes (or --submit)."""
    from qorcap_submit import OFFICIAL_URL, submit

    say("")
    say("Submit for Verification: send this match to the QorTime official referee. It re-checks everything")
    say("independently (uploads the match files and ~10-20% of the video) and, if every check passes,")
    say("countersigns with the official key so the receipt can show VERIFIED instead of SELF-ATTESTED.")
    if not auto:
        try:
            ans = ask("Submit now? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            say("Not submitted. You can do it later: double-click 'Submit for Verification' or run 'qorcap submit'.")
            return None
    return submit(bundle, base=referee_url or OFFICIAL_URL)


def sign_and_report(bundle: Path, draft: dict, cps: list[dict], device, referee, *, match_id: str, role: str,
                    started_ms: int, qt: Path, qorcap: str, no_open: bool, referee_kind: str = REFEREE_KIND,
                    note: str | None = None) -> int:
    """Shared by Start Match and finalize: build + sign receipt.json, run both verifiers, print the summary."""
    no_final = "final_basis" not in draft["result"] or draft["result"].get("final_basis") is None
    if no_final:
        draft = dict(draft, result={k: v for k, v in draft["result"].items() if k != "final_basis"})
    rec = build_receipt(draft, cps, device, referee, match_id=match_id, role=role, started_ms=started_ms,
                        referee_kind=referee_kind)
    (bundle / "receipt.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    (bundle / "referee.pub").write_text(referee.pubkey_hex + "\n", encoding="ascii")

    env = dict(os.environ, PYTHONPATH=str(qt) + os.pathsep + os.getenv("PYTHONPATH", ""))
    code, out = run_cmd([sys.executable, "-m", "qor_receipt.cli", "verify", str(bundle / "receipt.json"),
                         "--referee-pub", referee.pubkey_hex], env=env)
    try:
        v = json.loads(out[out.index("{"):])
    except ValueError:
        v = {"status": "FAIL", "errors": [out[-300:]], "partial": []}
    bcode, bout = run_cmd([qorcap, "verify-bundle", str(bundle)])
    (bundle / "verify.txt").write_text(f"qortime verify:\n{out}\n\nqorcap verify-bundle:\n{bout}\n", encoding="utf-8")
    result = overall(v["status"], bcode == 0)

    r = rec["result"]
    say("\n" + "=" * 64)
    say(f"  RESULT: {result}")
    say("=" * 64)
    unconfirmed = no_final or draft["result"].get("final_confirmed") is not True
    score_txt = ("not read (no scorebug read during the match)" if r.get("home_score") is None else
                 f"AWAY {r['away_score']} - HOME {r['home_score']}" + ("  (tie)" if r.get("winner") == "tie" else ""))
    say(f"  Score:        {score_txt}" + (
        "   (final NOT confirmed — last scorebug read)" if no_final else
        f"   (scorebug final via {r.get('final_basis')}; end-of-game menu not read: NOT confirmed)" if unconfirmed else
        "   (final read from the end-of-game menu)"))
    for n in r.get("notes") or []:
        say(f"  Note:         {n}")
    say(f"  Video check:  {'OK - every video byte matches the hash chain' if bcode == 0 else 'FAILED - see verify.txt'}")
    say(f"  Signatures:   {v['status']}  (QorTime verify; tier {v.get('trust_tier')})")
    if v.get("errors"):
        say(f"  Problems:     {', '.join(v['errors'][:6])}")
    if v.get("partial"):
        say(f"  Not checked:  {', '.join(v['partial'][:6])}")
    say("  Trust level:  SELF-ATTESTED. Signed by this PC's device key and a LOCAL referee key. It proves the")
    say("                video was not changed after recording; it does not prove who was playing.")
    say("                For VERIFIED, use 'Submit for Verification' (QorTime official referee re-checks it).")
    if note:
        say(f"  Offline:      {note}")
    say(f"\n  Folder:       {bundle}")
    say("  View online:  open " + VERIFY_URL)
    say("                click 'Choose file' and pick receipt.json from the folder above")
    say("                (or paste its text), paste the text of referee.pub into the key box, click Verify.")
    say("                It runs in your browser; nothing is uploaded.")
    if not no_open:
        open_folder(bundle)
    # SELF_ATTESTED is the normal local result since QorTime's trusted-referee allowlist; unknown -> FAIL code.
    return EXIT_CODES.get(result, 2)


# ------------------------------------------------------------------ offline finalize
FIN_HZ = 4.0


def read_jsonl(p: Path) -> list[dict]:
    """Parse JSONL, skipping blank lines and salvaging past a few corrupt lines."""
    if not p.exists():
        return []
    out, bad = [], 0
    for i, x in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not x.strip():
            continue
        try:
            out.append(json.loads(x))
        except json.JSONDecodeError as e:
            bad += 1
            say(f"WARN: {p.name}:{i}: corrupt JSONL line skipped ({e})")
            if bad > 3:
                raise
    return out


def latest_bundle(root: Path) -> Path | None:
    cands = [d for d in root.glob("match-*") if (d / "video.mp4").exists()] if root.exists() else []
    return max(cands, key=lambda d: d.name) if cands else None


def video_files(bundle: Path) -> list[Path]:
    """video.mp4 then any reopen parts (video.part2.mp4 ...), in manifest order."""
    seen: list[str] = []
    for m in read_jsonl(bundle / "manifest.jsonl"):
        f = m.get("file")
        if m.get("type") == "segment" and f and f not in seen:
            seen.append(f)
    files = [bundle / f for f in seen if (bundle / f).exists()]
    return files or ([bundle / "video.mp4"] if (bundle / "video.mp4").exists() else [])


def decode_frames(ffmpeg: str, video: Path, hz: float = FIN_HZ, size=(1280, 720)):
    """Yields (video_ms, BGR ndarray) at `hz`, full 1280x720 (v0.3.5; ReaderCore downsizes for the 640x360 readers)."""
    import numpy as np

    w, h = size
    p = subprocess.Popen([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(video), "-an",
                          "-vf", f"fps={hz},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                         stdout=subprocess.PIPE)
    n, fb = 0, w * h * 3
    try:
        while True:
            buf = p.stdout.read(fb)
            if len(buf) < fb:
                break
            yield int(n * 1000 / hz), np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        p.stdout.close()
        p.wait()


def segment_at(manifest: list[dict], wall_ms: int) -> dict | None:
    """Latest video segment written at or before wall_ms (else the first one)."""
    segs = [m for m in manifest if m.get("type") == "segment"]
    best = None
    for m in segs:
        if m.get("wall_ms", 0) <= wall_ms:
            best = m
    return best or (segs[0] if segs else None)


def score_checkpoints(events: list[dict], manifest: list[dict], match_id: str, role: str) -> list[dict]:
    """QorTime-shaped chain over offline score changes + final, each bound to the video chain head."""
    from qor_receipt.checkpoints import make_checkpoint, GENESIS
    from qor_receipt.jcs import sha256_hex

    out, prev, last, seq = [], GENESIS, None, 0
    for e in events:
        if e["type"] == "score":
            key = (e["home"], e["away"], e["quarter"])
            if key == last:
                continue
            trig, prev_score, last = "score_change", (dict(zip(("home", "away", "quarter"), last)) if last else None), key
            extra = {"score": {k: e[k] for k in ("home", "away", "quarter", "clock_s", "clock_red")}, "prev_score": prev_score}
        elif e["type"] == "final":
            trig, extra = "final", {"final": {k: e[k] for k in ("home", "away", "quarter", "t_ms", "basis") + (("method",) if "method" in e else ())}}
        else:
            continue
        seg = segment_at(manifest, e["t_ms"]) or {}
        ext = {"trigger": trig, "offline": True, "video_ms": e["video_ms"], "segment_seq": seg.get("seq"),
               "segment_hash": seg.get("sha256"), "chain_head": seg.get("chain_head"), "match_id": match_id, "role": role, **extra}
        cp = make_checkpoint(seq, e["t_ms"], sha256_hex(ext), sha256_hex([]), prev)
        cp["qorcap"] = ext
        out.append(cp)
        prev, seq = cp["hash"], seq + 1
    return out


def run_offline_reader(frames, t0_ms: int, core_factory, progress=None) -> tuple[list[dict], dict]:
    """Feeds decoded frames through ReaderCore; returns (events with video_ms, stats)."""
    events: list[dict] = []
    cur = {"video_ms": 0}

    def post(b):
        events.append(dict(b, video_ms=cur["video_ms"]))
        return {}

    core = core_factory(post)
    for vms, img in frames:
        cur["video_ms"] = vms
        core.on_frame(t0_ms + vms, img)
        if progress and core.stats["frames"] % 240 == 0:
            progress(vms, core)
    return events, core.stats


def default_core_factory(post):
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    sys.path.insert(0, str(HERE))
    from qorcap_endmenu import EndMenuReader
    from qorcap_mnp import AutoProfileReader
    from qorcap_reader import ReaderCore, madden_final_screen, prep

    return ReaderCore(post, AutoProfileReader(ScorebugReader(), prep=prep), AgreementWindow(), PauseDetector(), detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=EndMenuReader())


END_MENU_WAIT_S = 120  # after a scorebug final, keep recording this long for the end-of-game menu (authoritative)


def result_winner(home, away) -> str:
    """``result.winner``: "home" / "away", "tie" on equal scores, "unknown" when no score was read (QorTime
    receipt.v0 from 2026-10-17; before that a tie or a no-read 0-0 was drafted as an "away" win)."""
    if home is None or away is None:
        return "unknown"
    return "home" if home > away else "away" if away > home else "tie"


def is_end_menu_final(f: dict | None) -> bool:
    return bool(f) and (f.get("basis") == "end_menu" or f.get("method") == "end_menu_read")


def apply_final(res: dict, fin: dict | None, *, fallback: dict | None = None) -> dict:
    """Result fields from a reader final. Only an end-of-game-menu final is ``final_confirmed``; a scorebug final
    (clock_zero / scorebug_gone / final_screen) keeps its basis but stays unconfirmed; no final -> last read."""
    res = {k: v for k, v in res.items() if k not in ("final_basis", "final_confirmed", "scorebug_last", "notes")}
    src = fin or fallback
    if src:
        if fin:
            q = max(4, int(src["quarter"]))           # a final is in the 4th quarter or later (OT = 5)
        else:
            q = int(src["quarter"]) if src.get("quarter") is not None and int(src["quarter"]) >= 1 else None
        res.update(home_score=src["home"], away_score=src["away"], quarter=q, final_t_ms=src["t_ms"],
                   winner=result_winner(src["home"], src["away"]))
    else:
        # keep QorCap's drafted last read (null scores/quarter when nothing was read); winner from those scores
        res["winner"] = result_winner(res.get("home_score"), res.get("away_score"))
    if is_end_menu_final(fin):
        # Respect ReaderCore's corroboration gate for one-legal-step deltas.
        confirmed = fin.get("final_confirmed")
        if confirmed is None:
            confirmed = True
        res.update(method="end_menu_read", final_basis="end_menu", final_confirmed=bool(confirmed))
        sb = fin.get("scorebug_last")
        if sb:
            res["scorebug_last"] = {"home_score": sb["home"], "away_score": sb["away"], "quarter": sb["quarter"], "t_ms": sb["t_ms"]}
        if fin.get("notes"):
            res["notes"] = list(fin["notes"])
        if fin.get("delta_corroboration"):
            res["delta_corroboration"] = fin["delta_corroboration"]
    elif fin:
        res.update(method="agreed_scorebug_final", final_basis=fin["basis"], final_confirmed=False)
        res["notes"] = ["end-of-game menu score not read: scorebug final only, NOT confirmed"]
    else:
        res.update(method="last_scorebug_read_unconfirmed", final_confirmed=False)
    return res


TAIL_SECS = 240
TAIL_HZ = 2.0


def tail_final_check(bundle: Path, draft: dict, *, ffmpeg: str, secs: int = TAIL_SECS, hz: float = TAIL_HZ,
                     core_factory=None, frames=None) -> dict:
    """Live final missed: read the last ``secs`` of the recorded video (end-of-game menu) with the same reader,
    seeded with the live timeline, and confirm the final from recorded frames. Saves clips/final-recorded.mp4
    and final.tail.json. Checkpoints are untouched (the chain is sealed at stop); only the draft result changes."""
    live = [r for r in read_jsonl(bundle / "timeline.jsonl") if "obs" not in r and "home" in r]
    if not live:
        return draft
    vids = video_files(bundle)
    manifest = [m for m in read_jsonl(bundle / "manifest.jsonl") if m.get("type") == "segment"]
    if not vids or not manifest:
        return draft
    end_ms = int(manifest[-1]["wall_ms"])
    say(f"Final not seen live; checking the last {secs // 60} minutes of the recording for the end-of-game screen ...")
    events: list[dict] = []
    core = (core_factory or default_core_factory)(lambda b: events.append(b) or {})
    core.timeline = [dict(r) for r in live]
    try:
        from qorcap_reader import load_delta_corroboration_from_bundle
        # Preserve any corroboration the test/core_factory already set; else load from bundle.
        if not getattr(core, "delta_corroboration", None):
            core.delta_corroboration = load_delta_corroboration_from_bundle(bundle)
    except Exception:
        pass
    last_t = live[-1]["t_ms"]
    if frames is None:
        frames = decode_frames_tail(ffmpeg, vids[-1], secs, hz)
    pts = list(frames)
    if not pts:
        return draft
    span = pts[-1][0]
    for vms, img in pts:
        t = end_ms - span + vms
        if t <= last_t:
            continue
        core.on_frame(t, img)
        if getattr(core, "end_menu", None) is not None:
            break
    fin = core.final
    out = {"final": fin, "end_menu": getattr(core, "end_menu", None), "end_menu_rejected": getattr(core, "end_menu_rejected", None),
           "frames": core.stats, "window_secs": secs, "video": vids[-1].name,
           "method": "tail: QorCap end-of-game menu score reader (authoritative) + QorTime detect_final over the recorded last minutes, seeded with the live timeline"}
    (bundle / "final.tail.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    if not fin:
        say("  no end-of-game screen in the recording's last minutes; final stays NOT confirmed.")
        return draft
    if is_end_menu_final(fin):
        say(f"  FINAL (end-of-game menu, from recorded frames): AWAY {fin['away']} - HOME {fin['home']}")
        for n in fin.get("notes") or []:
            say(f"  note: {n}")
    else:
        say(f"  FINAL on the scorebug ({fin['basis']}, from recorded frames): AWAY {fin['away']} - HOME {fin['home']}; "
            "end-of-game menu score NOT read, so the final stays NOT confirmed.")
    clip = bundle / "clips" / "final-recorded.mp4"
    try:
        clip.parent.mkdir(exist_ok=True)
        subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-sseof", "-60", "-i", str(vids[-1]),
                        "-c", "copy", str(clip)], check=False, timeout=120)
        if clip.exists():
            say(f"  final clip saved: {clip}")
    except Exception as e:
        say(f"  (final clip not saved: {e})")
    return dict(draft, result=apply_final(draft["result"], fin))


def decode_frames_tail(ffmpeg: str, video: Path, secs: int, hz: float, size=(1280, 720)):
    """Like decode_frames, but only the last ``secs`` of the file (fast: input seek from the end)."""
    import numpy as np

    w, h = size
    p = subprocess.Popen([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-sseof", f"-{secs}", "-i", str(video),
                          "-an", "-vf", f"fps={hz},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                         stdout=subprocess.PIPE)
    n, fb = 0, w * h * 3
    try:
        while True:
            buf = p.stdout.read(fb)
            if len(buf) < fb:
                break
            yield int(n * 1000 / hz), np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        p.stdout.close()
        p.wait()


def finalize(bundle: Path, *, qt: Path, qorcap: str, ffmpeg: str, key_dir: Path, no_open: bool = True,
             core_factory=None, hz: float = FIN_HZ) -> int:
    bundle = bundle.resolve()
    vids = video_files(bundle)
    if not vids:
        say(f"No video.mp4 in {bundle}")
        return 2
    draft_p = bundle / "receipt.draft.json"
    if not draft_p.exists():
        say(f"{bundle} has no receipt.draft.json (was it recorded with --bundle?)")
        return 2
    draft = json.loads(draft_p.read_text(encoding="utf-8"))
    meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8")) if (bundle / "bundle.json").exists() else {}
    manifest = read_jsonl(bundle / "manifest.jsonl")
    qcps = read_jsonl(bundle / "checkpoints.jsonl")
    role = meta.get("role") or draft["players"][0]["role"]
    match_id = meta.get("match_id") or draft["match"]["match_id"]
    if match_id in ("", "unassigned"):
        match_id = f"local-{bundle.name.removeprefix('match-')}"
    started_ms = int(draft["match"]["started_ms"])
    device, referee = load_keys(key_dir)

    say(f"Reading the scorebug from {', '.join(v.name for v in vids)} ({hz:g} frames/s) ...")
    t0 = time.monotonic()
    events, stats, offset = [], {"frames": 0, "agreed": 0, "errors": 0, "paused": 0}, 0
    factory = core_factory or default_core_factory
    shared = {"core": None}

    def fac(post):  # one reader across parts so agreement/final state carries over
        if shared["core"] is None:
            shared["core"] = factory(post)
        else:
            shared["core"].post = post
        return shared["core"]

    def prog(vms, core):
        sys.stdout.write(f"\r  {vms // 60000:02d}:{vms // 1000 % 60:02d} of video read, {core.stats['agreed']} agreed reads ")
        sys.stdout.flush()

    for v in vids:
        last = [0]

        def frames(v=v):
            for vms, img in decode_frames(ffmpeg, v, hz):
                last[0] = vms
                yield offset + vms, img
        ev, st = run_offline_reader(frames(), started_ms, fac, prog)
        events += ev
        stats = dict(st)
        offset += last[0] + int(1000 / hz)
    say(f"\r  read {stats['frames']} frames in {time.monotonic() - t0:.0f}s: {stats['agreed']} agreed scorebug reads".ljust(78))

    with open(bundle / "timeline.offline.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            if e["type"] in ("score", "obs"):
                f.write(json.dumps(e) + "\n")
    scps = score_checkpoints(events, manifest, match_id, role)
    with open(bundle / "score_checkpoints.jsonl", "w", encoding="utf-8") as f:
        for c in scps:
            f.write(json.dumps(c) + "\n")
    finals = [e for e in events if e["type"] == "final"]
    fin = next((e for e in finals if is_end_menu_final(e)), finals[-1] if finals else None)
    reads = [e for e in events if e["type"] == "score"]
    (bundle / "final.offline.json").write_text(json.dumps(
        {"final": fin, "basis": fin and fin["basis"], "last_read": reads[-1] if reads else None, "frames": stats,
         "scorebug_profiles": getattr(getattr(shared["core"], "reader", None), "profile_counts", None),
         "score_checkpoints": len(scps), "score_checkpoint_head": scps[-1]["hash"] if scps else None,
         "method": "offline: QorTime ScorebugReader + AgreementWindow + detect_final over recorded video",
         "finalized_ms": int(time.time() * 1000)}, indent=2), encoding="utf-8")

    res = apply_final(draft["result"], fin, fallback=reads[-1] if reads else None)
    draft = dict(draft, result=res)
    rdr = getattr(shared["core"], "reader", None)
    if getattr(rdr, "profile_counts", None):  # v0.3.5: which scorebug layout was actually read
        draft["match"] = dict(draft["match"], scorebug_profile=rdr.active_profile)
        say(f"  scorebug profile: {rdr.active_profile} (agreed-quality reads per profile: {rdr.profile_counts})")
    say(f"  {len(reads)} score reads, {len(scps)} score checkpoints, final: "
        + (f"{fin['basis']} AWAY {fin['away']} - HOME {fin['home']}" if fin else "not detected"))
    for n in res.get("notes") or []:
        say(f"  note: {n}")
    if not fin:
        say("  The receipt uses the last score read: this is NOT a confirmed final.")

    live = read_jsonl(bundle / "countersigned.jsonl")
    if live and len(live) >= len(qcps) and all(a["hash"] == b["hash"] for a, b in zip(live, qcps)):
        cps, kind, note = live[:len(qcps)], REFEREE_KIND, "scores read after the fact; checkpoint signatures are the live ones from Start Match."
    else:
        cs = Countersigner(referee, match_id, role)
        cs.ts_of = lambda cp: int(cp["t_ms"])  # stamped with QorCap's recording clock, not observed live
        for c in qcps:
            cs.offer(c)
        cps, kind = cs.signed, REFEREE_KIND_OFFLINE
        note = ("no live countersignatures; the local referee signed QorCap's checkpoints after the fact, "
                "stamped with QorCap's recording times (referee key_kind local-self-attested-offline).")
    report_recording_audio(bundle, ffmpeg)
    say("\nSigning the receipt ...")
    return sign_and_report(bundle, draft, cps, device, referee, match_id=match_id, role=role, started_ms=started_ms,
                           qt=qt, qorcap=qorcap, no_open=no_open, referee_kind=kind, note=note)


def finalize_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="qorcap finalize", description="Offline: read scores from a recorded bundle and sign it")
    ap.add_argument("bundle", nargs="?", help="match bundle folder (default: newest in Documents/QorCap Matches)")
    ap.add_argument("--out", help="matches folder used to find the newest bundle")
    ap.add_argument("--qortime")
    ap.add_argument("--qorcap")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--key-dir")
    ap.add_argument("--hz", type=float, default=FIN_HZ)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)
    say("=" * 64)
    say("  QorCap  -  Finalize Match (offline)")
    say("=" * 64)
    root = Path(a.out) if a.out else Path.home() / "Documents" / "QorCap Matches"
    bundle = Path(a.bundle) if a.bundle else latest_bundle(root)
    if bundle is None or not bundle.exists():
        say(f"No recorded match found in {root}. Pass the bundle folder: qorcap finalize <folder>")
        return 2
    qt = find_qortime(a.qortime)
    sys.path.insert(0, str(qt))
    miss = check_python_deps(need_vision=True)
    if miss:
        say(f"Missing Python packages: {' '.join(miss)}\n  Fix: py -m pip install {' '.join(miss)}")
        return 3
    ff = a.ffmpeg or next((str(c) for c in [HERE.parent / ("ffmpeg.exe" if WIN else "ffmpeg")] if c.exists()), None) or shutil.which("ffmpeg") or "ffmpeg"
    return finalize(bundle, qt=qt, qorcap=find_qorcap(a.qorcap), ffmpeg=ff,
                    key_dir=Path(a.key_dir) if a.key_dir else data_dir() / "keys", no_open=a.no_open, hz=a.hz)


def run_main(argv=None) -> int:
    """Entry point for Start Match: never ends in a bare traceback; always a plain PROBLEM line."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        say("\nStopped before the match started. Nothing was signed.")
        return 130
    except Exception as e:
        import traceback
        say("")
        say(f"PROBLEM: QorCap hit an unexpected error: {type(e).__name__}: {e}")
        say("  Your recording (if any) is safe in Documents\\QorCap Matches. Double-click 'Finalize Last Match'")
        say("  to read the score and sign it now. Please send this window's text to Con.")
        traceback.print_exc()
        return 6


if __name__ == "__main__":
    sys.exit(run_main())
