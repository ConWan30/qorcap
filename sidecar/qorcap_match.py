"""One-click QorCap match: record + scorebug + live local countersign + signed QorTime receipt.

    python sidecar/qorcap_match.py [--test-pattern] [--qortime DIR] [--qorcap PATH] [--out DIR]

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

QORTIME_PIN = "f0d532ae31e0d3ad442d3b8bea0e34bbc2298f68"
QORTIME_ZIP = f"https://github.com/ConWan30/qortime/archive/{QORTIME_PIN}.zip"
VERIFY_URL = "https://conwan30.github.io/qortime/verify/"
REFEREE_KIND = "local-self-attested"
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

    def __init__(self, key, match_id: str, role: str, clock=lambda: int(time.time() * 1000)) -> None:
        self.key, self.match_id, self.role, self.clock = key, match_id, role, clock
        self.signed: list[dict] = []

    def offer(self, cp: dict) -> bool:
        from qor_receipt.checkpoints import body_of, countersign

        if cp["seq"] != len(self.signed):
            return False
        body = body_of(cp)
        body["hash"] = cp["hash"]
        self.signed.append(countersign(self.key, self.match_id, self.role, body, self.clock()))
        return True


def build_receipt(draft: dict, cps: list[dict], device, referee, *, match_id: str, role: str, started_ms: int) -> dict:
    """QorTime's receipt.build + sign_referee + sign_player on what QorCap observed."""
    from qor_receipt import receipt as R

    m = dict(draft["match"], match_id=match_id, started_ms=started_ms,
             ended_ms=max(cps[-1]["referee_ts_ms"] if cps else started_ms, started_ms))
    player = dict(draft["players"][0], role=role, pubkey=device.pubkey_hex, key_kind=device.kind)
    rec = R.build(match=m, result=draft["result"], players=[player],
                  referee={"pubkey": referee.pubkey_hex, "key_id": referee.key_id, "key_kind": REFEREE_KIND},
                  challenges=draft.get("challenges", []), checkpoints={role: cps},
                  agreement=draft.get("agreement") or {"timelines_agree": None, "mismatches": []},
                  signals=draft.get("signals") or {"motion_permille": {role: None}})
    rec = R.sign_referee(rec, referee)
    return R.sign_player(rec, role, device)


# ------------------------------------------------------------------ scorebug sources
def start_reader(api: Api, stop: threading.Event, state: dict) -> threading.Thread:
    import hashlib

    import cv2
    import numpy as np
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    sys.path.insert(0, str(HERE))
    from qorcap_reader import ReaderCore, prep

    def post(b):
        try:
            r = api.post("/events", b)
        except Exception:
            r = {}
        if b.get("type") == "final":
            state["final"] = b
        return r

    core = ReaderCore(post, ScorebugReader(), AgreementWindow(), PauseDetector(), detect_final)

    def loop():
        last = None
        while not stop.is_set():
            try:
                with urllib.request.urlopen(api.base + "/snapshot.jpg", timeout=3) as r:
                    data = r.read()
            except Exception:
                time.sleep(0.5)
                continue
            h = hashlib.sha256(data).digest()
            if h != last:
                last = h
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    core.on_frame(int(time.time() * 1000), prep(img))
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


# ------------------------------------------------------------------ console
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
    state = {"capturing": "REC", "waiting_for_device": "NO SIGNAL (check card/PS5)", "reopening": "reconnecting"}.get(
        st.get("state"), st.get("state", "?"))
    return (f"[{state}] {int(st.get('recording_secs') or 0) // 60:02d}:{int(st.get('recording_secs') or 0) % 60:02d}"
            f" | {score} | segments {cps.get('segments', 0)} | signed {signed} | clips {cps.get('clips', 0)}")


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


def overall(qt_status: str, bundle_ok: bool) -> str:
    if not bundle_ok or qt_status == "FAIL":
        return "FAIL"
    return qt_status


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
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
    ap.add_argument("--no-open", action="store_true")
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

    cmd = [qorcap, "--port", str(a.port)] + (["--ffmpeg", a.ffmpeg] if a.ffmpeg else []) + [
        "record", "--device", "testpattern" if a.test_pattern else a.device, "--res", "1280x720", "--fps", "30",
        "--bundle", str(bundle), "--replay", "90", "--match-id", match_id, "--role", a.role, "--player-id", a.player_id]
    if a.encoder or a.test_pattern:
        cmd += ["--encoder", a.encoder or "libx264"]
    if a.test_pattern:
        cmd += ["--audio", "none"]
    log = open(bundle / "console.log", "w", encoding="utf-8")
    started_ms = int(time.time() * 1000)
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            api.get("/status", timeout=1)
            break
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    if proc.poll() is not None:
        say(f"QorCap could not start. Details: {bundle / 'console.log'}")
        say("Common fixes: plug the capture card into a blue USB 3 port, close OBS/Camera apps, run Start Match again.")
        return 5

    say(f"Recording to: {bundle}")
    say("Play your game. Recording stops by itself at the final whistle.")
    say("Press ENTER (or Q, or Ctrl+C) to stop early.\n")
    stop, state = threading.Event(), {}
    cs = Countersigner(referee, match_id, a.role)
    if a.test_pattern:
        start_fake_scores(api, stop, state, a.test_secs)
    else:
        start_reader(api, stop, state)
    key_watcher(stop, state)

    def sweep():
        try:
            got = api.get(f"/checkpoints?since={len(cs.signed)}")
        except Exception:
            return None
        for c in got.get("checkpoints", []):
            cs.offer(c)
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
            if state.get("final") and final_seen_at is None:
                f = state["final"]
                final_seen_at = time.monotonic()
                say(f"\n\nFINAL ({f['basis']}): AWAY {f['away']} - HOME {f['home']}. Saving final clip ...")
            if state.get("user_stop"):
                say("\n\nStopping (you pressed a key) ...")
                break
            if final_seen_at and time.monotonic() - final_seen_at > 4:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("\n\nStopping (Ctrl+C) ...")
    stop.set()
    sweep()
    try:
        api.post("/shutdown")
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
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
    no_final = "final_basis" not in draft["result"]
    rec = build_receipt(draft, cs.signed, device, referee, match_id=match_id, role=a.role, started_ms=started_ms)
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
    say(f"  Score:        AWAY {r['away_score']} - HOME {r['home_score']}" + ("   (stopped early: NOT a confirmed final)" if no_final else f"   (final via {r.get('final_basis')})"))
    say(f"  Video check:  {'OK - every video byte matches the hash chain' if bcode == 0 else 'FAILED - see verify.txt'}")
    say(f"  Signatures:   {v['status']}  (QorTime verify; tier {v.get('trust_tier')})")
    if v.get("errors"):
        say(f"  Problems:     {', '.join(v['errors'][:6])}")
    if v.get("partial"):
        say(f"  Not checked:  {', '.join(v['partial'][:6])}")
    say("  Trust level:  SELF-ATTESTED. Signed by this PC's device key and a LOCAL referee key")
    say("                (no independent QorTime referee server exists yet). It proves the video was")
    say("                not changed after recording; it does not prove who was playing.")
    say(f"\n  Folder:       {bundle}")
    say("  View online:  open " + VERIFY_URL)
    say("                click 'Choose file' and pick receipt.json from the folder above")
    say("                (or paste its text), paste the text of referee.pub into the key box, click Verify.")
    say("                It runs in your browser; nothing is uploaded.")
    if not a.no_open:
        open_folder(bundle)
    return {"VERIFIED": 0, "PARTIAL": 0, "FAIL": 2}[result]


if __name__ == "__main__":
    sys.exit(main())
