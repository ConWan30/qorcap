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
``finalize --recover`` rebuilds receipt.draft.json and bundle.json from the files already on disk when the
recorder was killed before it could write them. Bytes after the last hashed segment are excluded.

Trust: both keys live on this PC, so the result is SELF-ATTESTED. The receipt's computed QorTime tier is
``single-attested`` and ``referee.key_kind`` is ``local-self-attested`` so any reader can see that the
"referee" is this machine, not an independent server. Keys are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

QORTIME_PIN = "0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2"
QORTIME_ZIP = f"https://github.com/ConWan30/qortime/archive/{QORTIME_PIN}.zip"
VERIFY_URL = "https://conwan30.github.io/qortime/verify/"
REFEREE_KIND = "local-self-attested"
REFEREE_KIND_OFFLINE = "local-self-attested-offline"
CAPTURE_APPS = ("obs64.exe", "obs32.exe", "obs.exe")
HERE = Path(__file__).resolve().parent
WIN = sys.platform == "win32"


def say(msg: str = "") -> None:
    from qorcap_console import emit
    emit(msg)


def data_dir() -> Path:
    from qorcap_paths import data_dir as _data_dir
    return _data_dir()


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
            from qorcap_paths import safe_extract_zip
            safe_extract_zip(r.read(), dst)
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
    from qorcap_paths import restrict_private
    restrict_private(p)


def load_keys(key_dir: Path):
    from qor_receipt.keys import FileKey  # QorTime's own persistent Ed25519 key (vendored Qoresence signing)

    from qorcap_paths import private_dir
    private_dir(key_dir)
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
FRAME_PERIOD_S = 0.25  # the frame loop aims at 4 Hz, subtracting the work already done
# OPTIONS presses ride this refresh. The end menu locks on 3 frames (~0.75 s at 4 Hz),
# so a 2 s period can accept a pause menu before the press is known. 0.5 s stays inside
# that window. A refresh with no new bytes is a short stat and read.
REFRESH_PERIOD_S = 0.5
READER_HEALTH_LOG_S = 30.0
SHUTDOWN_TIMEOUT_S = 900  # input proof decodes the whole video once; never kill QorCap mid-bundle


def _ms_stats(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0, "mean_ms": None, "max_ms": None}
    return {"n": len(samples), "mean_ms": int(round(sum(samples) / len(samples))), "max_ms": int(round(max(samples)))}


def reader_timing_row(t_ms: int, frame_periods_ms, refresh_ms, on_frame_ms, fetch_ms, open_health=None) -> dict:
    """One 30 s health line. Not a receipt field and not a timeline observation."""
    row = {
        "kind": "timing",
        "t_ms": int(t_ms),
        "frame_period_ms": _ms_stats(list(frame_periods_ms)),
        "refresh_ms": None if refresh_ms is None else int(round(float(refresh_ms))),
        "on_frame_ms": _ms_stats(list(on_frame_ms)),
        "fetch_ms": _ms_stats(list(fetch_ms)),
    }
    if open_health:
        row["open"] = open_health
    return row


def format_reader_timing(row: dict) -> str:
    frame = row.get("frame_period_ms") or {}
    work = row.get("on_frame_ms") or {}
    return (f"reader health: frame_period_ms mean={frame.get('mean_ms')} max={frame.get('max_ms')} "
            f"refresh_ms={row.get('refresh_ms')} on_frame_ms mean={work.get('mean_ms')} max={work.get('max_ms')}")


def append_reader_health(path: Path, row: dict) -> None:
    """Append one diagnostics line beside the bundle. Not a receipt field."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def open_reader_health(core) -> dict:
    """Spans that are still open, so a stall shows up before the span closes."""
    out = {}
    for name, span in (("reader_lag", getattr(core, "_lag", None)), ("score_unagreed", getattr(core, "_unagreed", None))):
        if not span:
            continue
        item = {k: span[k] for k in ("t_ms", "until_ms", "obs", "n", "max_gap_ms") if k in span}
        if span.get("reasons"):
            item["reasons"] = dict(span["reasons"])
        out[name] = item
    return out


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


def receipt_schema_path() -> Path:
    """receipt.v0.json beside sidecar/. The Windows zip has to ship contracts/ for this to exist."""
    return HERE.parent / "contracts" / "receipt.v0.json"


def _closed_props(section: str) -> frozenset:
    """Property names receipt.v0.json allows. The object sets additionalProperties to false."""
    path = receipt_schema_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise SystemExit(
            "QorCap cannot sign this match: the receipt schema is missing "
            f"({path}). The install must include contracts/receipt.v0.json next to the sidecar folder."
        )
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"QorCap cannot sign this match: {path} is not valid JSON.")
    node = schema.get("properties", {}).get(section) if isinstance(schema, dict) else None
    props = node.get("properties") if isinstance(node, dict) else None
    if not isinstance(node, dict) or node.get("additionalProperties") is not False or not isinstance(props, dict):
        raise SystemExit(f"QorCap cannot sign this match: {path.name} has no closed {section} object.")
    return frozenset(props)


def closed_result(result: dict) -> dict:
    """Drop fields receipt.v0.json rejects. Corroboration and Glimpse stay off the receipt."""
    if not isinstance(result, dict):
        return {}
    allowed = _closed_props("result")
    return {k: result[k] for k in result if k in allowed}


def closed_match(match: dict) -> dict:
    if not isinstance(match, dict):
        return {}
    allowed = _closed_props("match")
    return {k: match[k] for k in match if k in allowed}


def build_receipt(draft: dict, cps: list[dict], device, referee, *, match_id: str, role: str, started_ms: int,
                  referee_kind: str = REFEREE_KIND) -> dict:
    """QorTime's receipt.build + sign_referee + sign_player on what QorCap observed."""
    from qor_receipt import receipt as R

    m = dict(closed_match(draft["match"]), match_id=match_id, started_ms=started_ms,
             ended_ms=max(cps[-1]["referee_ts_ms"] if cps else started_ms, started_ms))
    player = dict(draft["players"][0], role=role, pubkey=device.pubkey_hex, key_kind=device.kind)
    rec = R.build(match=m, result=closed_result(draft["result"]), players=[player],
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
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    sys.path.insert(0, str(HERE))
    from qorcap_endmenu import EndMenuReader
    from qorcap_reader import ReaderCore, madden_final_screen, note_options_from_inputs, prep
    from qorcap_standard import standard_reader

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
    from qorcap_playcall import PauseCapableGate, PlayCallDetector
    core = ReaderCore(post, AutoProfileReader(standard_reader(), prep=prep), AgreementWindow(), PauseDetector(), detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=EndMenuReader(),
                      playcall=PlayCallDetector(), gate=PauseCapableGate())
    if bundle is not None:
        from qorcap_reanchor_notes import append_note
        core.reanchor_note = lambda row, bundle=bundle: append_note(bundle, row)
    state["core"] = core
    state["gate"] = core.gate
    try:  # Glimpse second opinion on held touchdown jumps: shadow log only unless QORCAP_GLIMPSE_TD_VETO=1
        from qorcap_reader import wire_touchdown_glimpse
        mode = wire_touchdown_glimpse(core, bundle.name if bundle is not None else None)
        if mode == "enforcing":
            print("  Glimpse touchdown veto: ENFORCING (QORCAP_GLIMPSE_TD_VETO=1; can only cancel or delay a fast-path lock)")
    except Exception as e:  # never fatal
        print(f"touchdown shadow log not started: {type(e).__name__}: {e}", file=sys.stderr)
    if bundle is not None:
        try:
            core.delta_corroboration = load_delta_corroboration_from_bundle(bundle)
        except Exception:
            core.delta_corroboration = []

    health_path = (bundle / "reader_health.jsonl") if bundle is not None else None

    def _write_health(row):
        if health_path is None:
            return
        try:
            append_reader_health(health_path, row)
        except Exception as e:
            print(f"reader health write failed: {type(e).__name__}: {e}", file=sys.stderr)

    core.health_write = _write_health

    def _refresh_corr():
        """Pull score-change clip hashes from /timeline + tail haptics/inputs from the live bundle."""
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
        # Publish a new list. The frame thread keeps whatever list it already loaded.
        core.delta_corroboration = merged

    def refresh():
        t0 = time.perf_counter()
        try:
            _refresh_corr()
        except Exception as e:
            state["corr_error"] = f"{type(e).__name__}: {e}"
        if bundle is not None:
            try:
                note_options_from_inputs(core, bundle / "inputs.jsonl")
            except Exception as e:
                state["options_error"] = f"{type(e).__name__}: {e}"
        state["refresh_ms"] = (time.perf_counter() - t0) * 1000.0

    def refresh_worker():
        # The first refresh already ran on the starter thread. Later ones stay off the frame thread.
        while not stop.wait(REFRESH_PERIOD_S):
            refresh()

    def loop():
        last, last_t = None, 0.0
        prev_tick = None
        periods, ons, fetches = [], [], []
        window_start = time.monotonic()
        try:
            while not stop.is_set():
                tick = time.perf_counter()
                if prev_tick is not None:
                    periods.append((tick - prev_tick) * 1000.0)
                prev_tick = tick
                try:
                    tf = time.perf_counter()
                    with urllib.request.urlopen(api.base + "/snapshot.jpg", timeout=3) as r:
                        data = r.read()
                    fetches.append((time.perf_counter() - tf) * 1000.0)
                except Exception:
                    if stop.wait(0.5):
                        return
                    continue
                h = hashlib.sha256(data).digest()
                # A perfectly static screen (end-of-game menu) can produce byte-identical snapshots; still feed
                # it about once a second so time-based spans (final_screen >= 3 s) can grow.
                if h != last or time.monotonic() - last_t >= STATIC_FRAME_S:
                    last, last_t = h, time.monotonic()
                    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        try:
                            t_on = time.perf_counter()
                            core.on_frame(int(time.time() * 1000), img)
                            ons.append((time.perf_counter() - t_on) * 1000.0)
                        except Exception as e:  # never let one bad frame kill the reader thread
                            state["reader_error"] = f"{type(e).__name__}: {e}"
                now = time.monotonic()
                if now - window_start >= READER_HEALTH_LOG_S:
                    row = reader_timing_row(int(time.time() * 1000), periods, state.get("refresh_ms"), ons, fetches,
                                            open_reader_health(core))
                    print(format_reader_timing(row), file=sys.stderr)
                    _write_health(row)
                    periods, ons, fetches = [], [], []
                    window_start = now
                remain = FRAME_PERIOD_S - (time.perf_counter() - tick)
                if remain > 0 and stop.wait(remain):
                    return
        finally:
            # tail_final_check reuses this process's bundle tail. Wait until the refresh
            # thread has left it, but don't hang shutdown on a stuck read.
            stop.set()
            refresh_thread.join(timeout=5.0)

    refresh()
    refresh_thread = threading.Thread(target=refresh_worker, name="qorcap-refresh", daemon=True)
    refresh_thread.start()
    t = threading.Thread(target=loop, name="qorcap-reader", daemon=True)
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
DIGITAL_SILENCE_DB = -88.0  # max level at/below this = all-zero samples (no +-1 LSB noise either)
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
    # Pure digital silence (every sample ~0, about -90 dB): the card is receiving an HDMI audio stream that carries
    # no PCM sound, which is what a Bitstream (Dolby/DTS) Audio Format looks like to a stereo-PCM USB capture card.
    digital = not res.get("no_audio_stream") and isinstance(res.get("max_db"), (int, float)) and res["max_db"] <= DIGITAL_SILENCE_DB
    first = (["  This is pure digital silence (all samples zero). If the PS5 already outputs to HDMI, check FIRST:",
              "   PS5 Settings > Sound > Audio Output > Audio Format (Priority) = Linear PCM",
              "   (Bitstream / Dolby / DTS reaches the capture card as silence)."] if digital else [])
    return ["",
            "  !" * 21,
            f"  WARNING: the capture card's audio is SILENT {when} ({lvl}).",
            *first,
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


# Windows process flags. DETACHED_PROCESS: qorcap has no console, so CTRL_CLOSE from this window
# does not reach it. CREATE_NEW_PROCESS_GROUP: Ctrl+C stays in this script. CREATE_BREAKAWAY_FROM_JOB:
# leave a kill-on-close job when the job allows it.
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def qorcap_spawn_flags() -> int:
    if not WIN:
        return 0
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    return DETACHED_PROCESS | group | CREATE_BREAKAWAY_FROM_JOB


def spawn_qorcap(cmd: list[str], stdout):
    """Start qorcap. If the launcher job refuses breakaway, start again without that flag."""
    flags = qorcap_spawn_flags()
    try:
        return subprocess.Popen(cmd, stdout=stdout, stderr=subprocess.STDOUT, creationflags=flags)
    except OSError:
        if not flags or not (flags & CREATE_BREAKAWAY_FROM_JOB):
            raise
        say("QorCap stays in this window's job. If the window is killed before the bundle is written, "
            "run finalize --recover on the match folder.")
        flags &= ~CREATE_BREAKAWAY_FROM_JOB
        return subprocess.Popen(cmd, stdout=stdout, stderr=subprocess.STDOUT, creationflags=flags)


def shutdown_body(reason: str) -> dict:
    return {"reason": reason}


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

# ---- v0.3.26 side pick ---------------------------------------------------------------------------------------------
# The side (home | away) is committed in every checkpoint (ext.role) from the first one the referee countersigns, so it
# must be right before qorcap.exe starts: a later correction re-signs the checkpoints and the referee then refuses the
# receipt (match-20261017-021003). Order: --side/--role flag > one-shot side file > the prompt. Never a silent default.
SIDE_FILE_NAME = "next-side.txt"
SIDE_FILE_MAX_AGE_S = 4 * 3600
SIDE_REMIND_S = 60

_BIG = {  # 5-row block letters for the prompt banner
    "H": ["#   #", "#   #", "#####", "#   #", "#   #"], "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"], "E": ["#####", "#    ", "#### ", "#    ", "#####"],
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"], "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "], "=": ["     ", "#####", "     ", "#####", "     "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def big_text(text: str) -> list[str]:
    return ["  ".join(_BIG[c][r] for c in text.upper()).rstrip() for r in range(5)]


def side_prompt_lines() -> list[str]:
    return ["", "=" * 64, "  WHICH SIDE ARE YOU?      press  H  or  A", "=" * 64, "",
            *("  " + l for l in big_text("H = HOME")), "",
            *("  " + l for l in big_text("A = AWAY")), "",
            "  AWAY = the LEFT team, listed FIRST on the Madden scorebug.",
            "  HOME = the RIGHT team, listed second.",
            "  Example: scorebug 'CHI 7  ATL 3'  ->  CHI is AWAY, ATL is HOME.",
            "",
            "  Nothing is recorded until you choose. Then press Enter to start.", "=" * 64]


def take_side_file(path: Path, *, now: float | None = None) -> tuple[str | None, str]:
    """Read and consume a one-shot side file (``home`` or ``away``). The file is renamed, never deleted, so a stale
    file cannot pick the side of a later match. Returns (side or None, note)."""
    if not path.exists():
        return None, ""
    now = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8-sig").strip().lower()
        age = now - path.stat().st_mtime
    except OSError as e:
        return None, f"side file {path} unreadable ({e}); asking instead"
    side = raw.split()[0] if raw else ""
    ok = side in ("home", "away") and age <= SIDE_FILE_MAX_AGE_S
    tag = "used" if ok else "ignored"
    stamp, n = time.strftime('%Y%m%d-%H%M%S'), 0
    dest = path.with_name(f"next-side.{tag}-{stamp}.txt")
    while dest.exists():  # never overwrite an earlier renamed file
        n += 1
        dest = path.with_name(f"next-side.{tag}-{stamp}-{n}.txt")
    try:
        path.replace(dest)
    except OSError:
        pass
    if not ok:
        why = f"'{raw[:20]}' is not home/away" if side not in ("home", "away") else f"it is {int(age // 60)} min old"
        return None, f"side file ignored ({why}); asking instead"
    return side, f"side from {path.name}: {side.upper()}"


def prompt_side(read_key, out=None, *, clock=time.monotonic, remind_s: float = SIDE_REMIND_S) -> str | None:
    """Show the side prompt and wait (no timeout) for H or A, then Enter. Re-shows the prompt with a beep every
    ``remind_s`` seconds. ``read_key(timeout)`` returns one character, "\n" for Enter, "" on timeout, None on EOF.
    Returns "home" | "away", or None when input is closed (no console)."""
    out = out or sys.stdout
    def show():
        out.write("\a" + "\n".join(side_prompt_lines()) + "\n")
        out.flush()
    show()
    choice, next_remind = None, clock() + remind_s
    while True:
        k = read_key(min(1.0, max(0.05, next_remind - clock())))
        if k is None:
            return None
        if k == "":
            if clock() >= next_remind:
                show()
                if choice:
                    out.write(f"  Picked {choice.upper()}. Press Enter to start recording (or the other letter to switch).\n")
                next_remind = clock() + remind_s
            continue
        c = k.lower()
        if c in ("h", "a"):
            choice = "home" if c == "h" else "away"
            label = "HOME (right team)" if choice == "home" else "AWAY (left / first-listed team)"
            out.write(f"\n  You picked {label}.\n  Press Enter to start recording, or press {'A' if c == 'h' else 'H'} to switch.\n")
            out.flush()
        elif c in ("\r", "\n") and choice:
            out.write("\n" + "\n".join("  " + l for l in big_text(choice)) + "\n\n")
            out.flush()
            return choice


def console_key_reader():
    """read_key for prompt_side: single keys on a Windows console, lines elsewhere (first char; empty = Enter)."""
    if WIN and sys.stdin and sys.stdin.isatty():
        import msvcrt
        def rk(timeout):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):  # arrow/function key prefix
                        msvcrt.getwch()
                        continue
                    return "\n" if ch in ("\r", "\n") else ch
                time.sleep(0.03)
            return ""
        return rk
    import select
    def rk(timeout):
        try:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
        except (OSError, ValueError):
            r = [sys.stdin]
        if not r:
            return ""
        line = sys.stdin.readline()
        if line == "":
            return None
        line = line.strip()
        return line[0] if line else "\n"
    return rk


def drain_console_keys() -> None:
    if WIN and sys.stdin and sys.stdin.isatty():
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getwch()


def resolve_side(a, *, side_file: Path, read_key=None, out=None) -> tuple[str | None, str | None]:
    """(side, source). source: flag | file | prompt; (home, None) is the legacy default for direct CLI runs without
    --side (tests); (None, None) = no answer possible (no console)."""
    if a.side in ("home", "away"):
        return a.side, "flag"
    if a.role in ("home", "away"):
        return a.role, "flag"
    if a.side == "ask":
        side, note = take_side_file(side_file)
        if note:
            say(note)
        if side:
            return side, "file"
        side = prompt_side(read_key or console_key_reader(), out)
        drain_console_keys()
        return (side, "prompt") if side else (None, None)
    return "home", None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "finalize" in argv:  # `qorcap_match.py [--qorcap X ...] finalize [BUNDLE] [opts]` (launcher puts its opts first)
        argv.remove("finalize")
        return finalize_main(argv)
    if "highlights" in argv:  # `qorcap_match.py highlights [BUNDLE] [--pre 8 --post 4]` (post-match only, opt-in)
        argv.remove("highlights")
        sys.path.insert(0, str(HERE))
        from qorcap_highlights import main as highlights_main
        return highlights_main(argv)
    if "psn-names" in argv:  # `qorcap_match.py psn-names [BUNDLE] [--samples 60]` (post-match only, on-screen evidence)
        argv.remove("psn-names")
        sys.path.insert(0, str(HERE))
        from qorcap_psn import main as psn_main
        return psn_main(argv)
    if "submit" in argv:  # `qorcap_match.py [--qorcap X ...] submit [BUNDLE] [--full]` -> QorTime official referee
        argv.remove("submit")
        from qorcap_submit import main as submit_main
        return submit_main(argv)
    if "pair" in argv:  # `qorcap_match.py pair create|join` -> pairing.json beside the receipt
        argv.remove("pair")
        from qorcap_pairing import main as pair_main
        return pair_main(argv)
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
    ap.add_argument("--side", choices=["home", "away", "ask"], default=None,
                    help="your side: home or away (AWAY = left / first-listed team on the Madden scorebug); 'ask' shows the H/A prompt unless a fresh one-shot next-side.txt is in the QorCap data folder. Start Match.cmd passes 'ask' unless --side is given.")
    ap.add_argument("--role", choices=["home", "away"], default=None, help="same as --side home|away (older name)")
    ap.add_argument("--player-id", default="local:player")
    ap.add_argument("--encoder")
    ap.add_argument("--res", default="1280x720")
    ap.add_argument("--fps", default="30")
    ap.add_argument("--input-format", default="mjpeg", help="mjpeg works on USB3.0 Video at 720p30; 'auto' probes the card")
    ap.add_argument("--audio", default="auto", help="card audio device ('auto'), a device name, or 'none'")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--plain", action="store_true",
                    help="classic one-line status and the one-line challenge prompt. Also QORCAP_PLAIN=1. "
                         "Logs are unchanged either way. NO_COLOR keeps the new layout without color.")
    ap.add_argument("--referee-url", default=None, help="QorTime official referee (default: the public service)")
    ap.add_argument("--no-anchors", action="store_true", help="do not timestamp the video chain with the official referee")
    ap.add_argument("--official-challenges", action="store_true",
                    help="fetch official challenge nonces at the first pause-capable moment (after a kickoff and a play call) "
                         "in Q1, Q3 and Q4 (press the shown pattern; needed for VERIFIED). Start Match.cmd passes this "
                         "unless --no-official-challenges is set.")
    ap.add_argument("--no-official-challenges", action="store_true",
                    help="do not fetch official challenge nonces (overrides --official-challenges)")
    ap.add_argument("--submit", action="store_true", help="submit for verification at the end without asking")
    ap.add_argument("--no-submit-prompt", action="store_true", help="do not offer 'Submit for Verification' at the end")
    ap.add_argument("--auto-highlights", action="store_true",
                    help="after the receipt is signed, cut highlight clips and attach content credentials. "
                         "Start Match passes this. A failure does not change the receipt or the submission.")
    ap.add_argument("--no-auto-highlights", action="store_true",
                    help="do not cut highlight clips after the match (overrides --auto-highlights)")
    pair = ap.add_mutually_exclusive_group()
    pair.add_argument("--pair", choices=["create"], default=None,
                      help="ask the referee for a pairing token (POST /v1/pairings), write pairing.json "
                           "beside the receipt, and print a code the other player joins with. "
                           "Corroboration does not change the verdict.")
    pair.add_argument("--pair-code", default=None,
                      help="join the other player's pairing code. Writes the same pairing.json. "
                           "Does not modify receipt.json.")
    a = ap.parse_args(argv)
    if a.no_official_challenges:
        a.official_challenges = False

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

    side, side_source = resolve_side(a, side_file=data_dir() / SIDE_FILE_NAME)
    if side is None:
        say("No side chosen and no keyboard to ask on. Start again with --side home or --side away"
            f" (or put home/away in {data_dir() / SIDE_FILE_NAME}).")
        return 6
    a.role = side
    say(f"Side: {side.upper()}" + (f"  (from {side_source})" if side_source else "  (default; pass --side home|away)"))

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

    if a.pair or a.pair_code:
        from qorcap_pairing import apply_pairing
        from qorcap_submit import OFFICIAL_URL
        paired = apply_pairing(bundle, create=a.pair == "create", code=a.pair_code,
                               base=a.referee_url or OFFICIAL_URL)
        if paired != 0:
            return paired

    cmd = [qorcap, "--port", str(a.port)] + (["--ffmpeg", a.ffmpeg] if a.ffmpeg else []) + [
        "record", "--device", "testpattern" if a.test_pattern else a.device, "--res", a.res, "--fps", a.fps,
        "--bundle", str(bundle), "--replay", "90", "--match-id", match_id, "--role", a.role, "--player-id", a.player_id]
    if side_source:
        cmd += ["--side-source", side_source]
        (bundle / "side_choice.json").write_text(json.dumps({"role": side, "source": side_source,
                                                             "chosen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2), encoding="utf-8")
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
    # Detach from this console and, when the job allows it, from the launcher job. Closing the
    # PowerShell window then does not deliver CTRL_CLOSE to qorcap. Enter still stops the match:
    # this process watches the keyboard and posts /shutdown.
    proc = spawn_qorcap(cmd, log)
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
    ui = None
    try:
        from qorcap_console import MatchConsole
        ui = MatchConsole(plain=bool(a.plain), out=sys.stdout,
                          status_getter=lambda: api.get("/status", timeout=0.25))
    except Exception:
        ui = None
    if not a.no_anchors or a.official_challenges:
        try:
            from qorcap_submit import OFFICIAL_URL, OfficialLive, start_official_live
            live = OfficialLive(a.referee_url or OFFICIAL_URL, bundle, match_id, a.role, api, challenges=a.official_challenges,
                                gate=lambda: state.get("gate"), console=ui)
            if a.official_challenges:
                say("Official QorTime challenges are ON (3 per game: game start, second half, 4th quarter).")
                say("A CHALLENGE line appears only when Madden lets you pause: after the kickoff, once a play is called.")
                say("Then, at the line before the snap, press OPTIONS and the shown buttons (90 seconds; if the snap")
                say("comes first, do it at the next play). Kickoffs never count: you cannot pause during them.\n")
        except Exception as e:  # never block a recording on the network helper
            say(f"(official referee link unavailable: {e})")
            live = None
    if ui is not None:
        try:
            ui.start()
        except Exception:
            ui.disable()
    if live is not None:
        try:
            start_official_live(live, stop)
        except Exception as e:
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
    stop_reason = "final"
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
            line = status_line(st, info, len(cs.signed))
            if ui is not None and ui.branded:
                try:
                    from qorcap_console import console_snapshot
                    ui.update(console_snapshot(st, info, len(cs.signed), live))
                    held = ((st.get("input") or {}).get("buttons")) or []
                    ui.observe_buttons(held, trusted=False)
                except Exception:
                    ui.disable()
                    sys.stdout.write("\r" + line.ljust(110))
                    sys.stdout.flush()
            else:
                sys.stdout.write("\r" + line.ljust(110))
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
                stop_reason = "enter"
                say("\n\nStopping (you pressed a key) ...")
                break
            if final_seen_at and time.monotonic() - final_seen_at > (4 if state.get("end_menu_seen") else END_MENU_WAIT_S):
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_reason = "ctrl_c"
        say("\n\nStopping (Ctrl+C) ...")
    finally:
        if ui is not None:
            try:
                ui.close()
            except Exception:
                pass
    stop.set()
    ignore_ctrl_c()  # from here on, a Ctrl+C must not abort signing (it caused 'receipt failed' + Y/N prompt)
    say("\n\nStopping QorCap: sealing and countersigning the final checkpoints ...")
    sweep()
    try:
        api.post("/shutdown", shutdown_body(stop_reason))
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
    # Same abstain rule as offline finalize. An end-menu final is kept. The rule is on, not behind a flag.
    live_fin = state.get("final")
    unresolved = _grammar_unresolved(state.get("core"))
    if live_fin and is_end_menu_final(live_fin):
        draft = apply_stopped_match_final(draft, live_fin, grammar_unresolved=unresolved)
    elif unresolved:
        draft = apply_stopped_match_final(draft, None, grammar_unresolved=True)
    if not a.test_pattern and draft["result"].get("method") != "end_menu_read" and a.ffmpeg:
        draft = tail_final_check(bundle, draft, ffmpeg=a.ffmpeg, grammar_unresolved=unresolved)
    elif not a.test_pattern and draft["result"].get("method") == "end_menu_read" and draft["result"].get("final_confirmed") is not True and a.ffmpeg:
        # Stop-time fallback: re-read with bundle haptics/clips so a live-uncorroborated delta can still confirm.
        draft = tail_final_check(bundle, draft, ffmpeg=a.ffmpeg, grammar_unresolved=unresolved)
    from qorcap_reanchor_notes import record_unresolved_final
    record_unresolved_final(bundle, draft.get("result"))
    if not a.test_pattern and a.audio != "none":
        report_recording_audio(bundle, a.ffmpeg or next((str(c) for c in [HERE.parent / ("ffmpeg.exe" if WIN else "ffmpeg")]
                                                         if c.exists()), None) or shutil.which("ffmpeg"))
    code = sign_and_report(bundle, draft, cs.signed, device, referee, match_id=match_id, role=a.role,
                           started_ms=started_ms, qt=qt, qorcap=qorcap, no_open=a.no_open)
    if live is not None:
        say(f"  Official anchors: {live.anchors} (link failures: {live.failures})")
    ff = a.ffmpeg or next((str(c) for c in [HERE.parent / ("ffmpeg.exe" if WIN else "ffmpeg")] if c.exists()), None) or shutil.which("ffmpeg") or "ffmpeg"

    def _highlights():
        # After sign_and_report only. The recorder has stopped; a fresh marker still skips this.
        post_match_highlights(bundle, ff, qorcap=qorcap, qortime=qt)

    def _submit():
        if code != 2 and (a.submit or not a.no_submit_prompt) and not a.test_pattern:
            offer_submit(bundle, a.referee_url, auto=a.submit, ask=ask_with_timeout)

    return after_receipt(code, auto_highlights=a.auto_highlights and not a.no_auto_highlights,
                         highlights=_highlights, submit=_submit)


SUBMIT_PROMPT_TIMEOUT_S = 600


def after_receipt(code: int, *, auto_highlights: bool, highlights, submit) -> int:
    """Highlights run after the receipt exists. They cannot change ``code`` or skip submission.

    ``highlights`` and ``submit`` are callables. ``submit`` may be a no-op when this match is not offered
    for upload. A highlights failure is printed and swallowed.
    """
    if auto_highlights and highlights is not None:
        try:
            highlights()
        except Exception as e:  # noqa: BLE001
            say(f"  highlights failed (receipt and submission are unaffected): {e}")
    if submit is not None:
        submit()
    return code


def ask_with_timeout(prompt: str, timeout_s: float = SUBMIT_PROMPT_TIMEOUT_S, *, stdin=None, clock=time.monotonic) -> str:
    """input() that gives up after ``timeout_s`` and answers "" (= No).

    v0.3.23: after match-20261018-084523 the Start Match sidecar sat at "Submit now? [y/N]" for over an hour
    after the match ended (qorcap.exe and the Glimpse watcher had exited). An unattended prompt should not
    keep the process alive or wait for a stray "y". Timeout means not submitted."""
    stdin = stdin or sys.stdin
    sys.stdout.write(prompt)
    sys.stdout.flush()
    end = clock() + timeout_s
    if WIN and stdin is sys.stdin and sys.stdin.isatty():
        import msvcrt
        buf = ""
        while clock() < end:
            if msvcrt.kbhit():
                ch = msvcrt.getwche()
                if ch in ("\r", "\n"):
                    sys.stdout.write("\n")
                    return buf
                if ch == "\b":
                    buf = buf[:-1]
                else:
                    buf += ch
            else:
                time.sleep(0.05)
    else:
        import select
        try:
            while clock() < end:
                r, _, _ = select.select([stdin], [], [], min(0.5, max(0.0, end - clock())))
                if r:
                    line = stdin.readline()
                    if line == "":
                        raise EOFError
                    return line.rstrip("\r\n")
        except (OSError, ValueError, TypeError):
            return input() if stdin is sys.stdin else stdin.readline().rstrip("\r\n")
    sys.stdout.write("\n")
    say(f"No answer in {int(timeout_s // 60)} min.")
    return ""


def bundle_submittable(bundle: Path) -> bool:
    """False only when bundle.json records ``submittable: false`` (a side correction).

    qorcap-bundle.v0.json allows extra fields, so the mark lives on bundle.json. A missing file
    or a bundle that never recorded the field is still offered.
    """
    try:
        meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(meta, dict) or meta.get("submittable") is not False


def set_bundle_submittable(bundle: Path, submittable: bool) -> None:
    """Write or clear ``submittable`` on bundle.json. No rewrite when the file already says that."""
    p = bundle / "bundle.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(meta, dict):
        return
    if submittable:
        if "submittable" not in meta:
            return
        meta.pop("submittable", None)
    else:
        if meta.get("submittable") is False:
            return
        meta["submittable"] = False
    p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def offer_submit(bundle: Path, referee_url: str | None, *, auto: bool = False, ask=input) -> int | None:
    """End-of-match 'Submit for Verification' prompt. Uploading needs an explicit yes (or --submit).

    A side-corrected bundle is not offered: its receipt was re-signed offline, and the live
    countersignatures still cover the recorded side.
    """
    from qorcap_submit import OFFICIAL_URL, submit

    if not bundle_submittable(bundle):
        say("NOT SUBMITTABLE")
        return None
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


def fill_challenge_results(bundle: Path, draft: dict, *, ffmpeg: str | None = None, decode: bool = True) -> dict:
    """v0.3.11: re-run every challenge on this bundle before signing.

    v0.3.14 uses the recorder's capture-path pause bound (see ``qorcap_challenge``). v0.3.15 stamps the
    pause frame from the fragment PTS, not the segment close. The official referee uses that same
    clock only when ``bundle.json`` ``challenge_rule`` is 2 (rule v2: 0..2500 ms and ±300 ms
    consistency). A bundle without that field stays on the referee's rule v1, 600 ms and
    ``segment_ranges``. Nothing here is submitted to it.

    QorCap <= 0.3.10 always signed ``result: null``, so a correctly answered challenge still verified as
    ``challenge_unanswered`` (match 2026-10-17 #4, 44b8d01f). An ok result goes into the receipt
    (``reverified_by: recorder``); a miss stays null and its reason is written to challenge_results.jsonl and shown."""
    chs = list(draft.get("challenges") or [])
    if not chs:
        return draft
    ff = ffmpeg or os.getenv("QORTIME_FFMPEG") or next(
        (str(c) for c in [HERE.parent / ("ffmpeg.exe" if WIN else "ffmpeg")] if c.exists()), None) or shutil.which("ffmpeg")
    old_env = os.environ.get("QORTIME_FFMPEG")
    try:
        if ff:
            os.environ["QORTIME_FFMPEG"] = ff
        from qorcap_challenge import challenge_results
        outs = challenge_results(bundle, chs, decode=decode)
    except Exception as e:  # never block signing; unanswered is the honest fallback
        say(f"  Challenges:   could not re-check ({type(e).__name__}: {e}); left unanswered")
        return draft
    finally:
        if old_env is None:
            os.environ.pop("QORTIME_FFMPEG", None)
        else:
            os.environ["QORTIME_FFMPEG"] = old_env
    new = []
    with open(bundle / "challenge_results.jsonl", "w", encoding="utf-8") as f:
        for c, o in zip(chs, outs):
            f.write(json.dumps(o["local"]) + "\n")
            new.append(dict(c, result=o["result"]) if o["result"] is not None else c)
            loc = o["local"]
            tag = "PASSED" if o["result"] is not None else f"not passed ({loc.get('reason')})"
            lat = f", pause on video {loc['latency_ms']} ms after OPTIONS" if loc.get("latency_ms") is not None else ""
            say(f"  Challenge {c.get('phase')} {str(c.get('id'))[:8]}: {tag}{lat}" + (f"  [{loc['note']}]" if loc.get("note") else ""))
    return dict(draft, challenges=new)


CHALLENGE_RULE_V2 = 2


def opt_in_challenge_rule(bundle: Path, rule: int = CHALLENGE_RULE_V2) -> None:
    """Ask the official referee to measure this bundle with challenge rule v2.

    Rule v1 (no field, or anything other than the integer 2) dates frames from the segment close
    stamps and requires the pause menu within 600 ms. Rule v2 dates the pause from the fragment
    PTS, accepts 0..2500 ms, and requires the successful latencies within ±300 ms of their median.
    An old recording opts in when it is signed again. The score, the draft hash, and the video
    are unchanged.
    """
    p = Path(bundle) / "bundle.json"
    if not p.exists():
        return
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return
    if not isinstance(meta, dict) or meta.get("challenge_rule") == rule:
        return
    meta["challenge_rule"] = rule
    p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def explain_local_partials(partial) -> list[str]:
    """Player-facing lines for the local ``qortime verify`` summary.

    A recorder result carries no ``rule`` field, so the verifier keeps the rule-1
    ceiling of 600 ms. An honest pause on this capture path is about 1.6 s and the
    recorder already accepted it, which is why the summary used to say
    ``Not checked: away:kickoff:challenge_latency_out_of_range`` after a pass.
    The official referee applies its own rule. This text does not change the receipt.
    """
    if not isinstance(partial, list):
        return []
    latency, other = [], []
    mark = "challenge_latency_out_of_range"
    for item in partial:
        if not isinstance(item, str):
            continue
        if item == mark or item.endswith(":" + mark):
            latency.append(item)
        else:
            other.append(item)
    lines = []
    if other:
        lines.append("  Not checked:  " + ", ".join(other[:6]))
    if latency:
        labels = []
        for item in latency[:6]:
            bits = item.split(":")
            labels.append(f"{bits[0]} {bits[1]}" if len(bits) >= 3 else item)
        lines.append("  Local challenge check: " + ", ".join(labels) + ".")
        lines.append("  The recorder on this PC accepted the pause. This receipt check still uses the 600 ms ceiling,")
        lines.append("  because the recorder does not stamp a rule on its own result. The official referee decides with its own rule.")
    return lines


def sign_and_report(bundle: Path, draft: dict, cps: list[dict], device, referee, *, match_id: str, role: str,
                    started_ms: int, qt: Path, qorcap: str, no_open: bool, referee_kind: str = REFEREE_KIND,
                    note: str | None = None, submittable: bool = True) -> int:
    """Shared by Start Match and finalize: build + sign receipt.json, run both verifiers, print the summary."""
    opt_in_challenge_rule(bundle)
    draft = fill_challenge_results(bundle, draft)
    draft = with_observed_handles(bundle, draft)
    no_final = "final_basis" not in draft["result"] or draft["result"].get("final_basis") is None
    if no_final:
        draft = dict(draft, result={k: v for k, v in draft["result"].items() if k != "final_basis"})
    rec = build_receipt(draft, cps, device, referee, match_id=match_id, role=role, started_ms=started_ms,
                        referee_kind=referee_kind)
    (bundle / "receipt.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    (bundle / "referee.pub").write_text(referee.pubkey_hex + "\n", encoding="ascii")
    try:
        from qorcap_score_timeline import write_score_timeline
        write_score_timeline(bundle, device, rec)
    except Exception as e:  # noqa: BLE001
        say(f"  score timeline sidecar failed (receipt is unaffected): {e}")

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
    for line in explain_local_partials(v.get("partial")):
        say(line)
    say("  Trust level:  SELF-ATTESTED. Signed by this PC's device key and a LOCAL referee key. It proves the")
    say("                video was not changed after recording; it does not prove who was playing.")
    if submittable:
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
    """video.mp4 then any reopen parts (video.part2.mp4 ...), in manifest order.

    A segment ``file`` that leaves the bundle is ignored. The score is read from the
    video in the bundle, not from a path the manifest points at outside it.
    """
    from qorcap_paths import bundle_child
    seen: list[str] = []
    files: list[Path] = []
    for m in read_jsonl(bundle / "manifest.jsonl"):
        f = m.get("file")
        if m.get("type") != "segment" or not f or f in seen:
            continue
        seen.append(f)
        child = bundle_child(bundle, f)
        if child is not None and child.is_file():
            files.append(child)
    if files:
        return files
    fallback = bundle_child(bundle, "video.mp4")
    return [fallback] if fallback is not None and fallback.is_file() else []


def hashed_decode_limit(video: Path, max_bytes: int | None) -> int | None:
    """Byte cap for a video that is longer than the hashed prefix. None means decode the whole file."""
    if max_bytes is None:
        return None
    try:
        size = video.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return max_bytes
    return None


def decode_frames(ffmpeg: str, video: Path, hz: float = FIN_HZ, size=(1280, 720), max_bytes: int | None = None):
    """Yields (video_ms, BGR ndarray) at `hz`, full 1280x720 (v0.3.5; ReaderCore downsizes for the 640x360 readers).

    ``max_bytes`` limits decoding to the hashed prefix. Bytes after that are an excluded tail and are not frames.
    """
    import numpy as np

    w, h = size
    cap = hashed_decode_limit(video, max_bytes)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", "pipe:0" if cap is not None else str(video),
           "-an", "-vf", f"fps={hz},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if cap is not None else None, stdout=subprocess.PIPE)
    if cap is not None:
        def feed(proc=p, path=video, n=cap):
            try:
                with path.open("rb") as f:
                    left = n
                    while left:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        proc.stdin.write(chunk)
                        left -= len(chunk)
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        threading.Thread(target=feed, daemon=True).start()
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


# Keys copied into checkpoint evidence. A re-anchor flag is not one of them, so frames_hash stays put.
_SCORE_EVIDENCE_KEYS = ("home", "away", "quarter", "clock_s", "clock_red")


def score_evidence(event: dict) -> dict:
    """The score object a checkpoint commits. Re-anchor flags are left out."""
    return {k: event[k] for k in _SCORE_EVIDENCE_KEYS}


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
            extra = {"score": score_evidence(e), "prev_score": prev_score}
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
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    sys.path.insert(0, str(HERE))
    from qorcap_endmenu import EndMenuReader
    from qorcap_mnp import AutoProfileReader
    from qorcap_reader import ReaderCore, madden_final_screen, prep
    from qorcap_standard import standard_reader

    from qorcap_playcall import PauseCapableGate, PlayCallDetector
    return ReaderCore(post, AutoProfileReader(standard_reader(), prep=prep), AgreementWindow(), PauseDetector(), detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=EndMenuReader(),
                      playcall=PlayCallDetector(), gate=PauseCapableGate())


END_MENU_WAIT_S = 120  # after a scorebug final, keep recording this long for the end-of-game menu (authoritative)


def result_winner(home, away) -> str:
    """``result.winner``: "home" / "away", "tie" on equal scores, "unknown" when no score was read (QorTime
    receipt.v0 from 2026-10-17; before that a tie or a no-read 0-0 was drafted as an "away" win)."""
    if home is None or away is None:
        return "unknown"
    return "home" if home > away else "away" if away > home else "tie"


def is_end_menu_final(f: dict | None) -> bool:
    return bool(f) and (f.get("basis") == "end_menu" or f.get("method") == "end_menu_read")


def _as_quarter(q):
    """A posted quarter, or None. Bool is an int subclass and is not a quarter."""
    if isinstance(q, bool) or not isinstance(q, int):
        return None
    return q if q >= 1 else None


def last_reliable_read(reads: list[dict]) -> dict | None:
    """Last score event. A trailing lock with a null quarter keeps the last quarter that was actually read."""
    if not reads:
        return None
    last = dict(reads[-1])
    if _as_quarter(last.get("quarter")) is None:
        for e in reversed(reads):
            q = _as_quarter(e.get("quarter"))
            if q is not None:
                last["quarter"] = q
                break
    return last


# One side, by a football increment. A drop, both sides, or any other jump is not a step.
_FOOTBALL_DELTAS = {1, 2, 3, 6, 7, 8}
# Match 20261017-162055: the scorebug was gone for 93s (end screen, not recognized), then a fresh 0-0.
SESSION_BREAK_MS = 20_000


def _score_pair(e: dict) -> tuple[int, int] | None:
    if "away" not in e or "home" not in e:
        return None
    try:
        return int(e["away"]), int(e["home"])
    except (TypeError, ValueError):
        return None


def _legal_step(prev: tuple[int, int], new: tuple[int, int]) -> bool:
    da, dh = new[0] - prev[0], new[1] - prev[1]
    if da == 0 and dh == 0:
        return True
    if da < 0 or dh < 0 or (da and dh):
        return False
    return (da or dh) in _FOOTBALL_DELTAS


def _obs_kind(e: dict) -> str | None:
    if e.get("type") == "obs" or ("obs" in e and "home" not in e):
        kind = e.get("obs")
        return kind if isinstance(kind, str) else None
    return None


def game_segment_score(events: list[dict]) -> dict | None:
    """Last legal score of the game segment.

    A backwards score, or any other illegal change, after a long scorebug-absent stretch or an
    end-of-game screen is a new session (a rematch, a demo, a fresh 0-0). It is excluded. The
    receipt keeps the last legal score from before that reset and says so. A legal scoring play
    after the same stretch is still this game (a timeout, then a touchdown). With no end-menu
    final the result stays unconfirmed.
    """
    kept: list[dict] = []
    pending_break = False
    note = None
    for e in events:
        kind = _obs_kind(e)
        if kind is not None:
            if kind == "final_screen":
                pending_break = True
            elif kind == "scorebug_absent":
                dur = int(e.get("until_ms") or 0) - int(e.get("t_ms") or 0)
                if dur >= SESSION_BREAK_MS:
                    pending_break = True
            continue
        if e.get("type") not in (None, "score"):
            continue
        pair = _score_pair(e)
        if pair is None:
            continue
        if not kept:
            kept.append(e)
            pending_break = False
            continue
        prev = _score_pair(kept[-1])
        if prev is not None and _legal_step(prev, pair):
            kept.append(e)
            pending_break = False
            continue
        if pending_break and prev is not None:
            note = (f"post-game score reset to away {pair[0]} - home {pair[1]} after the scorebug left; "
                    f"kept the last legal score away {prev[0]} - home {prev[1]}")
            break
        kept.append(e)
        pending_break = False
    last = last_reliable_read(kept)
    if last is not None and note:
        last = dict(last, notes=[note])
    return last


def keep_recovery_notes(res: dict, draft: dict) -> dict:
    """Keep the recover notes after the score read replaces ``result``. A normal draft is unchanged."""
    if not draft.get("recovered"):
        return res
    kept = [n for n in ((draft.get("result") or {}).get("notes") or []) if isinstance(n, str)]
    notes = list(res.get("notes") or [])
    for n in kept:
        if n not in notes:
            notes.append(n)
    if not notes:
        return res
    return dict(res, notes=notes)


def _grammar_unresolved(core) -> bool:
    """True when a standard-bar grammar on this reader must not name the winner."""
    reader = getattr(core, "reader", None)
    found = []
    if reader is None:
        return False
    own = getattr(reader, "grammar", None)
    if own is not None and hasattr(own, "allows_final_fallback"):
        found.append(own)
    for child in getattr(reader, "readers", ()) or ():
        g = getattr(child, "grammar", None)
        if g is not None and hasattr(g, "allows_final_fallback"):
            found.append(g)
    return any(not g.allows_final_fallback() for g in found)


def apply_stopped_match_final(draft: dict, live_fin, *, grammar_unresolved: bool = False) -> dict:
    """Live Start Match final. A disagreement or an unconfirmed re-anchor abstains.

    A gated end-menu final is kept. This is the same rule as offline finalize, and it is on.
    """
    if grammar_unresolved and not is_end_menu_final(live_fin):
        return dict(draft, result=apply_final(draft["result"], None, grammar_unresolved=True))
    if live_fin and is_end_menu_final(live_fin):
        return dict(draft, result=apply_final(draft["result"], live_fin, grammar_unresolved=grammar_unresolved))
    return draft


def apply_final(res: dict, fin: dict | None, *, fallback: dict | None = None, grammar_unresolved: bool = False) -> dict:
    """Result fields from a reader final. Only an end-of-game-menu final is ``final_confirmed``; a scorebug final
    (clock_zero / scorebug_gone / final_screen) keeps its basis but stays unconfirmed; no final -> last read.

    ``grammar_unresolved`` means the score grammar was still disagreeing, or its last re-anchor was
    not confirmed by the agreement window. The final then abstains: null scores, winner unknown.
    It does not publish the last timeline score. An end-of-game menu final is a separate read and
    is kept.
    """
    if grammar_unresolved and not is_end_menu_final(fin):
        notes = [n for n in (res.get("notes") or []) if isinstance(n, str)]
        note = "score grammar disagreed or the last re-anchor was unconfirmed; the final score abstains"
        if note not in notes:
            notes.append(note)
        out = {k: v for k, v in res.items() if k not in ("final_basis", "final_confirmed", "scorebug_last", "notes")}
        out.update(home_score=None, away_score=None, quarter=None, winner="unknown",
                   method="last_scorebug_read_unconfirmed", final_confirmed=False, notes=notes)
        return out
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
        if src and src.get("notes"):
            res["notes"] = [n for n in src["notes"] if isinstance(n, str)]
    return res


TAIL_SECS = 240
TAIL_HZ = 2.0


def tail_final_check(bundle: Path, draft: dict, *, ffmpeg: str, secs: int = TAIL_SECS, hz: float = TAIL_HZ,
                     core_factory=None, frames=None, grammar_unresolved: bool = False) -> dict:
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
    from qorcap_reanchor_notes import append_note
    core.reanchor_note = lambda row, bundle=bundle: append_note(bundle, row)
    core.timeline = [dict(r) for r in live]
    try:
        from qorcap_reader import note_options_from_inputs
        note_options_from_inputs(core, bundle / "inputs.jsonl")
    except Exception:
        pass
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
        if grammar_unresolved and draft.get("result", {}).get("method") != "end_menu_read":
            return dict(draft, result=apply_final(draft["result"], None, grammar_unresolved=True))
        return draft
    still = bool(grammar_unresolved) or _grammar_unresolved(core)
    if is_end_menu_final(fin):
        say(f"  FINAL (end-of-game menu, from recorded frames): AWAY {fin['away']} - HOME {fin['home']}")
        for n in fin.get("notes") or []:
            say(f"  note: {n}")
    elif still:
        say("  score grammar is unresolved; the final score abstains.")
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
    # The tail reader starts with a fresh grammar, so a live disagreement stays in force.
    # An end-menu final is kept inside apply_final. A scorebug final does not clear it.
    return dict(draft, result=apply_final(draft["result"], fin, grammar_unresolved=still))


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


_LOG_REASONS = (
    ("pipe_broken", ("recorder pipe broken", "os error 232", "stop reason: pipe_broken")),
    ("ctrl_c", ("ctrl+c", "stop reason: ctrl_c")),
    ("console_close", ("stop reason: console_close", "console_close")),
    ("launcher_gone", ("launcher process", "stop reason: launcher_gone")),
    ("disk_full", ("disk space critically low", "stop reason: disk_full")),
    ("duration", ("--duration reached", "stop reason: duration")),
)
_GENESIS = "0" * 64
_PLAYER_DEFAULT = "local:player"


def stop_reason_from_log(text: str | None) -> str:
    """The earliest stop cause actually written in qorcap.log. No log line means the cause was not recorded."""
    if not text:
        return "not_recorded"
    low = text.lower()
    found_at = None
    found = None
    for reason, needles in _LOG_REASONS:
        for needle in needles:
            i = low.find(needle)
            if i >= 0 and (found_at is None or i < found_at):
                found, found_at = reason, i
    return found or "not_recorded"


def _int_pair(obj: dict):
    """(home, away, quarter or None, t_ms or None) from an on-disk object. Booleans are not scores."""
    if not isinstance(obj, dict):
        return None
    h, a = obj.get("home"), obj.get("away")
    if isinstance(h, bool) or isinstance(a, bool) or not isinstance(h, int) or not isinstance(a, int):
        return None
    q = obj.get("quarter")
    q = q if isinstance(q, int) and not isinstance(q, bool) else None
    t = obj.get("t_ms")
    t = t if isinstance(t, int) and not isinstance(t, bool) else None
    return h, a, q, t


def _winner(home, away) -> str:
    if home is None or away is None:
        return "unknown"
    if home > away:
        return "home"
    if away > home:
        return "away"
    return "tie"


def _challenge_row(c: dict, role_fallback: str) -> dict | None:
    nonce = c.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
        return None
    phase = c.get("phase")
    role = c.get("role") or role_fallback
    pattern = c.get("pattern")
    if phase not in ("kickoff", "halftime", "end") or role not in ("home", "away"):
        return None
    if not isinstance(pattern, list) or len(pattern) < 2 or not all(isinstance(x, str) for x in pattern):
        return None
    issued, deadline = c.get("issued_ms"), c.get("deadline_ms")
    if isinstance(issued, bool) or isinstance(deadline, bool) or not isinstance(issued, int) or not isinstance(deadline, int):
        return None
    cid = c.get("id") if isinstance(c.get("id"), str) and c.get("id") else nonce[:8]
    return {"id": cid, "role": role, "phase": phase, "nonce": nonce, "pattern": pattern,
            "issued_ms": issued, "deadline_ms": deadline, "result": None}


def recover_bundle(bundle: Path) -> dict | None:
    """Rebuild receipt.draft.json and bundle.json from the files the recorder already wrote.

    Scores, challenges, match id and the stop reason come only from those files. A video tail
    past the last hashed segment is recorded and excluded. No segment is invented for it.
    """
    import hashlib

    from qorcap_submit import CLIENT_VERSION
    from qor_receipt.jcs import sha256_hex

    bundle = bundle.resolve()
    manifest = read_jsonl(bundle / "manifest.jsonl")
    cps = read_jsonl(bundle / "checkpoints.jsonl")
    timeline = read_jsonl(bundle / "timeline.jsonl")
    challenges = read_jsonl(bundle / "challenges.jsonl")
    official = read_jsonl(bundle / "official_challenges.jsonl")
    inputs = read_jsonl(bundle / "inputs.jsonl")
    segs = sorted((m for m in manifest if m.get("type") == "segment"), key=lambda m: m.get("seq") if isinstance(m.get("seq"), int) else 0)
    if not segs and not cps:
        say(f"{bundle} has no manifest.jsonl segments or checkpoints to recover")
        return None

    match_id = role = device = device_id = None
    for c in cps:
        q = c.get("qorcap") if isinstance(c.get("qorcap"), dict) else {}
        if match_id is None and isinstance(q.get("match_id"), str) and q["match_id"]:
            match_id = q["match_id"]
        if role is None and q.get("role") in ("home", "away"):
            role = q["role"]
    for m in segs:
        if device is None and isinstance(m.get("device"), str):
            device = m["device"]
        if device_id is None and isinstance(m.get("device_id"), str):
            device_id = m["device_id"]
    notes = ["player_id was not on disk; recorder default local:player"]
    if match_id is None:
        match_id = "unassigned"
        notes.append("match_id was not on disk; recorder default unassigned")
    if role is None:
        role = "home"
        notes.append("role was not on disk; recorder default home")

    times = []
    for m in segs:
        if isinstance(m.get("wall_ms"), int) and not isinstance(m.get("wall_ms"), bool):
            times.append(m["wall_ms"])
    for c in cps:
        if isinstance(c.get("t_ms"), int) and not isinstance(c.get("t_ms"), bool):
            times.append(c["t_ms"])
    started = min(times) if times else 0
    ended = max(times) if times else started

    fin = None
    for c in cps:
        q = c.get("qorcap") if isinstance(c.get("qorcap"), dict) else {}
        if q.get("trigger") == "final" and isinstance(q.get("final"), dict):
            fin = q["final"]
    last = None
    for t in timeline:
        if _int_pair(t):
            last = t
    cp_score = None
    for c in reversed(cps):
        q = c.get("qorcap") if isinstance(c.get("qorcap"), dict) else {}
        if _int_pair(q.get("score") if isinstance(q.get("score"), dict) else None):
            cp_score = q["score"]
            break

    confirmed = False
    basis = None
    if fin and _int_pair(fin):
        home, away, quarter, final_t = _int_pair(fin)
        final_t = final_t if final_t is not None else ended
        basis = fin.get("basis") if fin.get("basis") in ("clock_zero", "scorebug_gone", "final_screen", "end_menu") else None
        if basis == "end_menu":
            method = "end_menu_read"
            confirmed = fin.get("final_confirmed") is True
        elif basis:
            method = "agreed_scorebug_final"
        else:
            method = "last_scorebug_read_unconfirmed"
            notes.append("final checkpoint had no basis; score is unconfirmed")
    elif last and _int_pair(last):
        home, away, quarter, final_t = _int_pair(last)
        final_t = final_t if final_t is not None else ended
        method = "last_scorebug_read_unconfirmed"
        notes.append("final not on disk; last timeline score is unconfirmed")
    elif cp_score and _int_pair(cp_score):
        home, away, quarter, final_t = _int_pair(cp_score)
        final_t = ended
        method = "last_scorebug_read_unconfirmed"
        notes.append("no timeline score; last checkpoint score is unconfirmed")
    else:
        home = away = quarter = None
        final_t = ended
        method = "last_scorebug_read_unconfirmed"
        notes.append("no score on disk; scores left null")

    chs = []
    seen = set()
    for src in (challenges, official):
        for c in src:
            if not isinstance(c, dict):
                continue
            row = _challenge_row(c, role)
            if row is None or row["id"] in seen:
                continue
            chs.append(row)
            seen.add(row["id"])
    unusable = 0
    for src in (challenges, official):
        for c in src:
            if isinstance(c, dict) and _challenge_row(c, role) is None:
                unusable += 1
    if unusable:
        notes.append(f"{unusable} challenge record(s) skipped: required fields were not on disk")

    log_p = bundle / "qorcap.log"
    stop_reason = stop_reason_from_log(log_p.read_text(encoding="utf-8", errors="replace") if log_p.exists() else None)
    notes.append(f"recording stopped: {stop_reason}")
    notes.append("recovered from on-disk evidence; unhashed video bytes are excluded")

    covered: dict[str, int] = {}
    for m in segs:
        f = m.get("file")
        if not isinstance(f, str):
            continue
        off = m.get("offset") if isinstance(m.get("offset"), int) and not isinstance(m.get("offset"), bool) else 0
        n = m.get("bytes") if isinstance(m.get("bytes"), int) and not isinstance(m.get("bytes"), bool) else 0
        covered[f] = max(covered.get(f, 0), off + n)
    tails = []
    from qorcap_paths import bundle_child
    for f, end in covered.items():
        p = bundle_child(bundle, f)
        if p is None or not p.is_file():
            continue
        real = p.stat().st_size
        if real > end:
            with p.open("rb") as fh:
                fh.seek(end)
                tail = fh.read()
            tails.append({"file": f, "covered_bytes": end, "tail_bytes": real - end,
                          "tail_sha256": hashlib.sha256(tail).hexdigest()})
            notes.append(f"unhashed tail excluded: {f} {real - end} bytes after byte {end}")

    chunks = [r for r in inputs if isinstance(r, dict) and r.get("type") == "chunk"]
    if chunks:
        notes.append(f"inputs.jsonl: {len(chunks)} chunk(s) on disk")

    result = {"home_score": home, "away_score": away, "quarter": quarter, "final_t_ms": final_t,
              "winner": _winner(home, away), "method": method, "final_confirmed": confirmed, "notes": notes}
    if basis:
        result["final_basis"] = basis
    draft = {
        "schema": "qor-receipt/v0",
        "match": {"match_id": match_id, "game": "madden27", "platform": "ps5", "scorebug_profile": "madden27_standard",
                  "mode": "single", "started_ms": started, "ended_ms": ended},
        "result": result,
        "players": [{"role": role, "player_id": _PLAYER_DEFAULT, "pubkey": _GENESIS, "key_kind": "software"}],
        "referee": {"pubkey": _GENESIS, "key_id": "0" * 16, "key_kind": "unset"},
        "challenges": chs,
        "checkpoints": {},
        "agreement": {"timelines_agree": None, "mismatches": []},
        "signals": {"motion_permille": {role: None}, "stop_reason": stop_reason, "recovered": True},
        "trust_tier": "single-attested",
        "signatures": {"players": {}, "referee": None},
        "stop_reason": stop_reason,
        "recovered": True,
    }
    clips = [m for m in manifest if m.get("type") == "clip"]
    bundle_doc = {
        "schema": "qorcap-bundle/v0",
        "qorcap_version": CLIENT_VERSION,
        "match_id": match_id,
        "role": role,
        "device": device,
        "device_id": device_id,
        "segments": len(segs),
        "chain_head": segs[-1].get("chain_head") if segs else None,
        "checkpoints": len(cps),
        "checkpoint_head": cps[-1].get("hash") if cps else None,
        "challenges": len(chs),
        "clips": clips,
        "final": fin,
        "final_confirmed": confirmed,
        "receipt_draft_sha256": sha256_hex(draft),
        "recovered": True,
        "stop_reason": stop_reason,
        "inputs_on_disk": {"chunks": len(chunks), "head": chunks[-1].get("head") if chunks else None},
        "claims": "Proves the integrity and timing of these video bytes (hash chain + checkpoints, referee co-signable). Does NOT prove who was playing.",
    }
    if tails:
        bundle_doc["excluded_tails"] = tails
    (bundle / "receipt.draft.json").write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    (bundle / "bundle.json").write_text(json.dumps(bundle_doc, indent=2) + "\n", encoding="utf-8")
    say(f"recovered bundle ({stop_reason}); {len(segs)} hashed segment(s), {len(tails)} excluded tail(s)")
    return draft


def recorded_role(qcps: list[dict], meta: dict, draft: dict) -> str:
    """The side QorCap was set to while recording: the hash-chained checkpoints carry it (qorcap.role), and the live
    countersignatures and official challenge tokens were signed for it. Falls back to the bundle metadata."""
    for c in qcps:
        q = c.get("qorcap") if isinstance(c.get("qorcap"), dict) else {}
        if q.get("role") in ("home", "away"):
            return q["role"]
    corr = meta.get("role_correction") if isinstance(meta.get("role_correction"), dict) else {}
    return corr.get("from") or meta.get("role") or draft["players"][0]["role"]


def correct_role(bundle: Path, meta: dict, *, recorded: str, role: str, reason: str) -> dict:
    """Side correction after recording (the recorder was set to the wrong side). Recorded evidence is never edited: the
    checkpoints, challenges and video keep the recorded side. bundle.json gets the corrected ``role`` plus a
    ``role_correction`` record (from, to, reason, when) so the change is visible; receipt.json is then built and signed
    again for the corrected side by finalize. Setting the recorded side again removes the record."""
    if role not in ("home", "away"):
        raise ValueError(f"role must be home or away, not {role!r}")
    meta = dict(meta)
    if role == recorded:
        if "role_correction" not in meta and meta.get("role") == role:
            return meta
        meta.pop("role_correction", None)
    else:
        prev = meta.get("role_correction") if isinstance(meta.get("role_correction"), dict) else {}
        if meta.get("role") == role and prev.get("to") == role and prev.get("reason") == reason:
            return meta
        meta["role_correction"] = {"from": recorded, "to": role, "reason": reason, "corrected_ms": int(time.time() * 1000)}
    meta["role"] = role
    (bundle / "bundle.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def finalize(bundle: Path, *, qt: Path, qorcap: str, ffmpeg: str, key_dir: Path, no_open: bool = True,
             core_factory=None, hz: float = FIN_HZ, recover: bool = False, role: str | None = None,
             role_reason: str | None = None) -> int:
    bundle = bundle.resolve()
    vids = video_files(bundle)
    if not vids:
        say(f"No video.mp4 in {bundle}")
        return 2
    if recover and recover_bundle(bundle) is None:
        return 2
    draft_p = bundle / "receipt.draft.json"
    if not draft_p.exists():
        say(f"{bundle} has no receipt.draft.json (was it recorded with --bundle?)")
        return 2
    draft = json.loads(draft_p.read_text(encoding="utf-8"))
    meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8")) if (bundle / "bundle.json").exists() else {}
    manifest = read_jsonl(bundle / "manifest.jsonl")
    qcps = read_jsonl(bundle / "checkpoints.jsonl")
    rec_role = recorded_role(qcps, meta, draft)
    if role is not None:
        meta = correct_role(bundle, meta, recorded=rec_role, role=role, reason=role_reason or "set with finalize --role")
    role = meta.get("role") or draft["players"][0]["role"]
    if role != rec_role:
        corr = meta.get("role_correction") if isinstance(meta.get("role_correction"), dict) else {}
        say(f"  Side corrected after recording: QorCap was set to {rec_role}, signing as {role}"
            + (f" ({corr['reason']})" if corr.get("reason") else ""))
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
            from qorcap_reanchor_notes import append_note
            shared["core"].reanchor_note = lambda row, bundle=bundle: append_note(bundle, row)
            try:
                from qorcap_reader import note_options_from_inputs
                note_options_from_inputs(shared["core"], bundle / "inputs.jsonl")
            except Exception:
                pass
        else:
            shared["core"].post = post
        return shared["core"]

    def prog(vms, core):
        sys.stdout.write(f"\r  {vms // 60000:02d}:{vms // 1000 % 60:02d} of video read, {core.stats['agreed']} agreed reads ")
        sys.stdout.flush()

    caps = {}
    for t in meta.get("excluded_tails") or []:
        if isinstance(t, dict) and isinstance(t.get("file"), str) and isinstance(t.get("covered_bytes"), int):
            caps[t["file"]] = t["covered_bytes"]
            say(f"  excluding {t.get('tail_bytes')} unhashed bytes of {t['file']} (after byte {t['covered_bytes']})")
    for v in vids:
        last = [0]

        def frames(v=v, cap=caps.get(v.name)):
            for vms, img in decode_frames(ffmpeg, v, hz, max_bytes=cap):
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

    unresolved = _grammar_unresolved(shared["core"])
    res = apply_final(draft["result"], fin, fallback=game_segment_score(events),
                      grammar_unresolved=unresolved)
    from qorcap_reanchor_notes import record_unresolved_final
    record_unresolved_final(bundle, res)
    res = keep_recovery_notes(res, draft)
    if role != rec_role:
        corr = meta.get("role_correction") if isinstance(meta.get("role_correction"), dict) else {}
        res = dict(res, notes=list(res.get("notes") or []) + [
            f"side corrected after recording: QorCap was set to {rec_role}, signed as {role}"
            + (f" ({corr['reason']})" if corr.get("reason") else "")
            + f"; checkpoints and challenges keep the recorded side {rec_role}"])
        sig = dict(draft.get("signals") or {})
        mp = dict(sig.get("motion_permille") or {})
        if rec_role in mp and role not in mp:
            mp[role] = mp.pop(rec_role)
        draft = dict(draft, signals=dict(sig, motion_permille=mp or {role: None}))
    draft = dict(draft, result=res)
    rdr = getattr(shared["core"], "reader", None)
    if getattr(rdr, "profile_counts", None):  # v0.3.5: which scorebug layout was actually read
        draft["match"] = dict(draft["match"], scorebug_profile=rdr.active_profile)
        say(f"  scorebug profile: {rdr.active_profile} (agreed-quality reads per profile: {rdr.profile_counts})")
    rejected = getattr(shared["core"], "end_menu_rejected", None)
    if stats.get("end_menu_reads") and not is_end_menu_final(fin):
        why = (rejected or {}).get("why") or "readable end-menu frames never agreed"
        say(f"  END MENU rejected: {why}")
    say(f"  {len(reads)} score reads, {len(scps)} score checkpoints, final: "
        + (f"{fin['basis']} AWAY {fin['away']} - HOME {fin['home']}" if fin else "not detected"))
    for n in res.get("notes") or []:
        say(f"  note: {n}")
    if not fin:
        say("  The receipt uses the last score read: this is NOT a confirmed final.")

    live = read_jsonl(bundle / "countersigned.jsonl")
    side_changed = role != rec_role
    if side_changed and live:
        say(f"  The live countersignatures were signed for side {rec_role}; re-signing the checkpoints for {role} offline.")
        say("  Those live signatures cover the recorded side. This receipt is not sent to the referee.")
    if live and not side_changed and len(live) >= len(qcps) and all(a["hash"] == b["hash"] for a, b in zip(live, qcps)):
        cps, kind, note = live[:len(qcps)], REFEREE_KIND, "scores read after the fact; checkpoint signatures are the live ones from Start Match."
    else:
        # Offline re-sign only. Nothing here rebuilds a live countersignature on the recorded side.
        cs = Countersigner(referee, match_id, role)
        cs.ts_of = lambda cp: int(cp["t_ms"])  # stamped with QorCap's recording clock, not observed live
        for c in qcps:
            cs.offer(c)
        cps, kind = cs.signed, REFEREE_KIND_OFFLINE
        note = (f"the live countersignatures are bound to the recorded side {rec_role}; " if side_changed and live
                else "no live countersignatures; ") + (
                "the local referee signed QorCap's checkpoints after the fact, "
                "stamped with QorCap's recording times (referee key_kind local-self-attested-offline).")
    if side_changed:
        say("NOT SUBMITTABLE")
    report_recording_audio(bundle, ffmpeg)
    say("\nSigning the receipt ...")
    code = sign_and_report(bundle, draft, cps, device, referee, match_id=match_id, role=role, started_ms=started_ms,
                           qt=qt, qorcap=qorcap, no_open=no_open, referee_kind=kind, note=note,
                           submittable=not side_changed)
    # After sign_and_report: it may rewrite bundle.json (challenge rule). The mark is the last write.
    # Restoring the recorded side clears it and reuses the live signatures already on disk.
    set_bundle_submittable(bundle, not side_changed)
    return code


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
    ap.add_argument("--plain", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--recover", action="store_true",
                    help="rebuild receipt.draft.json and bundle.json from manifest, checkpoints, inputs, challenges and timeline")
    ap.add_argument("--role", choices=["home", "away"],
                    help="correct the side after recording (QorCap was set to the wrong side): bundle.json records the "
                         "correction and the receipt is signed for this side; the recorded evidence is not edited")
    ap.add_argument("--role-reason", help="why the side was corrected (stored in bundle.json role_correction)")
    ap.add_argument("--psn-names", action="store_true",
                    help="opt-in: before signing, read the PSN names Madden showed (online games) into the receipt as "
                         "match.observed_handles (on-screen evidence, not account proof)")
    ap.add_argument("--highlights", action="store_true",
                    help="after signing, cut highlight clips around each locked score change (highlights/). The panel title is SCOREBUG LOCK.")
    ap.add_argument("--auto-highlights", action="store_true",
                    help="same clip step as --highlights, then attach content credentials when the receipt verifies. "
                         "Start Match and Finalize Last Match pass this.")
    ap.add_argument("--no-auto-highlights", action="store_true",
                    help="do not cut highlight clips after signing")
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
    if a.psn_names:
        post_match_psn_names(bundle, ff)
    qorcap = find_qorcap(a.qorcap)
    rc = finalize(bundle, qt=qt, qorcap=qorcap, ffmpeg=ff,
                  key_dir=Path(a.key_dir) if a.key_dir else data_dir() / "keys", no_open=a.no_open, hz=a.hz,
                  recover=a.recover, role=a.role, role_reason=a.role_reason)
    if (a.auto_highlights or a.highlights) and not a.no_auto_highlights:
        # Receipt and rc are already settled. Highlights cannot change either.
        try:
            post_match_highlights(bundle, ff, qorcap=qorcap, qortime=qt)
        except Exception as e:  # noqa: BLE001
            say(f"  highlights failed (receipt is unaffected): {e}")
    return rc


def post_match_psn_names(bundle: Path, ffmpeg: str) -> None:
    """Opt-in post-match step (before signing). Never raises: a name problem must not change the finalize result; the
    receipt then simply carries no observed_handles. Refuses while the recorder still writes the bundle."""
    try:
        sys.path.insert(0, str(HERE))
        from qorcap_psn import Refuse, read_bundle, summary
        say("\nReading the PSN names Madden showed on screen (on-screen evidence, not account proof) ...")
        doc = read_bundle(bundle, ffmpeg=ffmpeg)
        (bundle / "psn_names.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
        for line in summary(doc):
            say(line)
    except Refuse as e:
        say(f"  PSN names skipped: {e}")
    except Exception as e:  # noqa: BLE001
        say(f"  PSN names failed (the receipt is unaffected): {e}")


def with_observed_handles(bundle: Path, draft: dict) -> dict:
    """Copy psn_names.json into the receipt draft as match.observed_handles when it is bound to this bundle's video
    chain (a stale or copied file is ignored). Signed with the rest of the receipt."""
    try:
        sys.path.insert(0, str(HERE))
        from qorcap_psn import load_bound, observed_handles
        doc = load_bound(bundle)
    except Exception:  # noqa: BLE001
        return draft
    if not doc:
        return draft
    return dict(draft, match=dict(draft["match"], observed_handles=observed_handles(doc)))


def post_match_highlights(bundle: Path, ffmpeg: str, *, qorcap: str | None = None, qortime: Path | None = None) -> None:
    """After the receipt is signed. Never raises, never runs while a recorder is writing, never rewrites receipt.json.

    Highlight clips are cut either way. Content credentials are attached only when the receipt verifies.
    A receipt that does not verify leaves the clips unsigned (verified stays false). The panel title stays
    SCOREBUG LOCK. Submission and the finalize exit code are not this function's to change.
    """
    receipt_p = bundle / "receipt.json"
    before = receipt_p.read_bytes() if receipt_p.exists() else None
    try:
        sys.path.insert(0, str(HERE))
        from qorcap_highlights import Refuse, attach_credentials, make_highlights, recorder_running
        if recorder_running(bundle):
            say("  highlights skipped: QorCap is still recording")
            return
        say("\nCutting highlight clips ...")
        man = make_highlights(bundle, ffmpeg=ffmpeg)
        say(f"  {len(man['clips'])} clip(s) in {bundle / 'highlights'} (manifest.json links each to its lock and the receipt)")
        if recorder_running(bundle):
            say("  content credentials skipped: QorCap is still recording")
            return
        try:
            cred = attach_credentials(bundle, bundle / "highlights" / "manifest.json", qorcap=qorcap, qortime=qortime)
            n = (cred.get("content_credentials") or {}).get("attached_clips", 0)
            say(f"  content credentials on {n} clip(s). A local receipt is self-attested, not verified.")
        except Exception as e:  # noqa: BLE001
            say(f"  content credentials skipped (clips and receipt are unaffected): {e}")
    except Refuse as e:
        say(f"  highlights skipped: {e}")
    except Exception as e:  # noqa: BLE001
        say(f"  highlights failed (receipt is unaffected): {e}")
    finally:
        if before is not None:
            try:
                now = receipt_p.read_bytes() if receipt_p.exists() else b""
            except OSError:
                now = b""
            if now != before:
                receipt_p.write_bytes(before)
                say("  highlights restored receipt.json")


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
