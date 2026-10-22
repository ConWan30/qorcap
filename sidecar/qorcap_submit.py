"""Submit a recorded QorCap match bundle to the QorTime official referee for verification.

    qorcap submit [BUNDLE] [--referee URL] [--full] [--yes]
    python sidecar/qorcap_submit.py [BUNDLE] ...

What happens (nothing is trusted just because this PC says so):
  1. The bundle's small files (manifest, checkpoints, controller log, receipt.json, ...) are zipped and sent.
     The referee recomputes every hash chain and checks the laptop's own receipt before going further.
  2. The referee then picks which video segments it wants: the start (init), the recorded tail with the
     end-of-game menu, any challenge windows, and N random segments drawn only after step 1 committed the
     manifest. (--full sends every segment instead: 1-3 GB.) Each segment is hashed against the manifest.
  3. The referee decodes those segments itself, re-reads the final score, re-runs challenge patterns from the
     raw controller log, and only if every check passes countersigns with the official QorTime key.
  4. This PC's device key co-signs the same receipt. Result: receipt.official.json next to receipt.json.

Since QorCap 0.3.10 every submission is signed by this PC's install key, which is the same device key that recorded
the match. No account is needed. The client fetches a single-use nonce from /v1/submit/challenge, solves a small
proof-of-work (about 1 s), and signs {method, path, timestamp, nonce, pow, sha256(zip), sha256(manifest.jsonl)}.
The referee rejects replays, enforces per-key and global quotas, and requires the signer to be the bundle's
recorder key. QorCap 0.3.9 and older get HTTP 426 ("upgrade required").

The official private key never touches this PC. Stdlib only (+ QorTime's qor_receipt for the device signature).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

OFFICIAL_URL = os.getenv("QORTIME_REFEREE_URL", "https://qortime-referee-production.up.railway.app")
OFFICIAL_PUB = "a5aa0c9121db2c517284bf1c49eeec199829ca832a2b665f26e872e01192feea"
META_FILES = ("bundle.json", "manifest.jsonl", "checkpoints.jsonl", "countersigned.jsonl", "inputs.jsonl",
              "haptics.jsonl", "input_proof.json", "receipt.json", "receipt.draft.json", "timeline.jsonl",
              "challenges.jsonl", "official_challenges.jsonl", "anchors.jsonl", "referee.pub",
              "challenge_gates.jsonl", "challenge_results.jsonl", "pairing.json", "device_attest.v0.json")
HERE = Path(__file__).resolve().parent
CLIENT_VERSION = "0.3.28"
SUBMIT_TYPE = "qortime-submit/v1"   # = QorTime contracts compat.json submit_auth.request_type
POW_PREFIX = "qortime-pow/v1|"      # = compat.json submit_auth.pow_prefix
# Same wire value as qorcap_match.REFEREE_KIND_OFFLINE. A receipt carrying it was signed on this PC
# after the fact; it is not the live countersigned log.
OFFLINE_KEY_KIND = "local-self-attested-offline"


def say(msg: str = "") -> None:
    print(msg, flush=True)


class RefereeError(Exception):
    def __init__(self, code: int, body):
        super().__init__(f"HTTP {code}: {body}")
        self.code, self.body = code, body


class Client:
    def __init__(self, base: str, timeout: float = 120.0, opener=None) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        self._open = opener or urllib.request.urlopen

    def call(self, method: str, path: str, data: bytes | None = None, ctype: str = "application/json",
             timeout: float | None = None, headers: dict | None = None):
        h = {"Content-Type": ctype, "User-Agent": f"qorcap-submit/{CLIENT_VERSION}", **(headers or {})}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        rq = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with self._open(rq, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw).get("detail", raw.decode("utf-8", "replace"))
            except ValueError:
                body = raw.decode("utf-8", "replace")[:500]
            raise RefereeError(e.code, body) from None

    def post_json(self, path: str, body: dict, timeout: float | None = None):
        return self.call("POST", path, json.dumps(body).encode(), timeout=timeout)


def pow_ok(nonce: str, pub: str, pow_: str, bits: int) -> bool:
    d = hashlib.sha256(f"{POW_PREFIX}{nonce}|{pub}|{pow_}".encode()).digest()
    return bits <= 0 or int.from_bytes(d, "big") >> (256 - bits) == 0


def solve_pow(nonce: str, pub: str, bits: int) -> str:
    i = 0
    while not pow_ok(nonce, pub, str(i), bits):
        i += 1
    return str(i)


def submit_auth_header(device, *, path: str, body: bytes, bundle_head: str, nonce: str, pow_: str,
                       ts_ms: int | None = None) -> str:
    """X-Qor-Submit value: install (device) key signature over JCS of the request summary."""
    from qor_receipt.jcs import canonicalize

    ts = int(time.time() * 1000) if ts_ms is None else ts_ms
    msg = canonicalize({"type": SUBMIT_TYPE, "method": "POST", "path": path, "pub": device.pubkey_hex.lower(),
                        "ts_ms": ts, "nonce": nonce, "pow": pow_, "body_sha256": hashlib.sha256(body).hexdigest(),
                        "bundle_head": bundle_head})
    h = {"v": 1, "pub": device.pubkey_hex.lower(), "ts_ms": ts, "nonce": nonce, "pow": pow_, "bundle_head": bundle_head,
         "sig": device.sign(msg)}
    return base64.urlsafe_b64encode(json.dumps(h, separators=(",", ":")).encode()).decode().rstrip("=")


def official_key_status(info: dict) -> tuple[bool, str]:
    """Is the referee's signing key the official one? Either the root itself (legacy) or an online key with a valid,
    unrevoked delegation from the root (checked locally with QorTime's verifier code, not taken from the server)."""
    pub = str(info.get("pubkey") or "")
    if pub == OFFICIAL_PUB:
        return True, "official root key"
    cert = info.get("delegation")
    if not cert:
        return False, "not the official key and no delegation certificate"
    try:
        from qor_receipt import delegation as D
        from qor_receipt.trusted_referees import load_allowlist
    except ImportError:
        return False, "this QorTime copy cannot check delegations (update QorCap)"
    now = int(time.time() * 1000)
    ok, probs = D.check_delegation(cert, pub, now, allowlist=load_allowlist(), revocations=D.load_revocations(),
                                   now_ms=now)
    if ok:
        return True, f"online key {ok.get('id')} (delegation serial {ok.get('serial')} from {ok.get('delegated_by')})"
    return False, f"delegation not valid: {', '.join(probs)}"


def load_device(key_path: Path | None):
    from qor_receipt.keys import FileKey

    kp = key_path or (data_dir() / "keys" / "device.key")
    if not kp.exists():
        return None, kp
    return FileKey(kp), kp


def meta_zip(bundle: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in META_FILES:
            p = bundle / n
            if p.exists():
                z.write(p, n)
    return buf.getvalue()


def read_manifest(bundle: Path) -> list[dict]:
    out = []
    for line in (bundle / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "segment":
            out.append(m)
    return out


def segment_bytes(bundle: Path, m: dict) -> bytes:
    """Read one manifest segment. The file name must be a single file inside the bundle."""
    from qorcap_paths import bundle_child
    path = bundle_child(bundle, m.get("file"))
    if path is None:
        raise ValueError(f"segment file is not inside the bundle: {m.get('file')!r}")
    with open(path, "rb") as f:
        f.seek(int(m["offset"]))
        return f.read(int(m["bytes"]))


def latest_bundle(root: Path) -> Path | None:
    c = [d for d in root.glob("match-*") if (d / "receipt.json").exists()] if root.exists() else []
    return max(c, key=lambda d: d.stat().st_mtime) if c else None


def data_dir() -> Path:
    from qorcap_paths import data_dir as _data_dir
    return _data_dir()


def fmt_mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 10e6 else f"{n / 1e6:.1f} MB"


def print_checks(review: dict | None) -> None:
    for name, c in ((review or {}).get("checks") or {}).items():
        st = c.get("state", "?")
        d = c.get("detail")
        if isinstance(d, dict):
            d = d.get("notes") or d.get("note") or d.get("errors") or {k: d[k] for k in list(d)[:4]}
        say(f"    {st.upper():8s} {name}: {d}")


def _jsonl_dicts(path: Path) -> list[dict]:
    """JSONL objects, blank and corrupt lines skipped. Same inclusion rule as the referee's parse_jsonl."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def countersigned_mismatch(bundle: Path) -> dict | None:
    """First receipt checkpoint that is not the live countersigned log, or an offline re-sign of one.

    Port of qortime ``qor_receipt/review/bundle.py`` ``check_receipt_binding`` (the countersigned
    loop, pin 0204e6d lines 241-245). The referee zips ``receipt.checkpoints[role]`` with
    ``countersigned.jsonl`` and stops at the first pair whose ``hash`` or ``referee_sig`` differs.
    Role is ``bundle.json`` role, else the receipt player, else ``home`` (Bundle.role).

    Also refuses ``referee.key_kind == local-self-attested-offline`` when that live log exists,
    even if the checkpoint strings were copied across. There is no path here that rebuilds a
    live countersignature.

    Returns ``{checkpoint, field, error}`` or None. ``field`` is ``hash``, ``referee_sig``, or
    ``key_kind``.
    """
    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        receipt = {}
    if not isinstance(meta, dict):
        meta = {}
    role = str(meta.get("role") or (receipt.get("players") or [{}])[0].get("role") or "home")
    cps = (receipt.get("checkpoints") or {}).get(role) or []
    if not isinstance(cps, list):
        cps = []
    countersigned = _jsonl_dicts(bundle / "countersigned.jsonl")
    if not countersigned:
        return None
    for i, (x, y) in enumerate(zip(cps, countersigned)):
        # qor_receipt.review.bundle.check_receipt_binding:
        #   if x.get("hash") != y.get("hash") or x.get("referee_sig") != y.get("referee_sig"):
        #       errs.append(f"receipt checkpoint {i} differs from countersigned.jsonl")
        x = x if isinstance(x, dict) else {}
        y = y if isinstance(y, dict) else {}
        hash_differs = x.get("hash") != y.get("hash")
        sig_differs = x.get("referee_sig") != y.get("referee_sig")
        if hash_differs or sig_differs:
            field = "hash" if hash_differs else "referee_sig"
            return {"checkpoint": i, "field": field,
                    "error": f"receipt checkpoint {i} {field} differs from countersigned.jsonl"}
    kind = str((receipt.get("referee") or {}).get("key_kind") or "")
    if kind == OFFLINE_KEY_KIND:
        return {"checkpoint": 0, "field": "key_kind",
                "error": "offline-signed receipt (key_kind local-self-attested-offline) "
                         "while countersigned.jsonl exists"}
    return None


def submit(bundle: Path, *, base: str, full: bool = False, key_path: Path | None = None, client: Client | None = None,
           device=None, out_name: str = "receipt.official.json") -> int:
    bundle = bundle.resolve()
    for need in ("manifest.jsonl", "checkpoints.jsonl", "receipt.json", "bundle.json"):
        if not (bundle / need).exists():
            say(f"PROBLEM: {bundle} has no {need}. Run Start Match (or 'qorcap finalize') first so receipt.json exists.")
            return 2
    # Before any network call. A side-corrected or offline-resigned receipt is not_submitted
    # here; the referee would answer HTTP 422.
    mismatch = countersigned_mismatch(bundle)
    if mismatch is not None:
        say(f"NOT SUBMITTABLE: {mismatch['error']}")
        say("Nothing was sent to the referee.")
        log = {"referee": base, "bundle": str(bundle), "started": int(time.time()),
               "state": "not_submitted", "checkpoint": mismatch["checkpoint"], "field": mismatch["field"],
               "error": mismatch["error"]}
        (bundle / "submission.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
        return 4
    c = client or Client(base)
    log: dict = {"referee": base, "bundle": str(bundle), "started": int(time.time())}

    def save(extra: dict) -> None:
        log.update(extra)
        (bundle / "submission.json").write_text(json.dumps(log, indent=2), encoding="utf-8")

    try:
        info = c.call("GET", "/v1/referee", timeout=30)
    except Exception as e:
        say(f"PROBLEM: cannot reach the QorTime referee at {base} ({e}). Check the internet and try again.")
        return 3
    official, why = official_key_status(info)
    if not official:
        say(f"  note: this referee's key {str(info.get('pubkey'))[:16]}... is NOT the official QorTime key ({why}); "
            "its receipts will verify as SELF_ATTESTED at most.")
    say(f"Referee: {base}  (key {str(info.get('pubkey'))[:16]}..., {why if official else 'unofficial'})")
    if device is None:
        device, kp = load_device(key_path)
        if device is None:
            say(f"PROBLEM: device key not found at {kp} (it is created by the first Start Match on this PC)")
            return 2
    mode = "full" if full else "spot-check"
    path = f"/v1/submissions?mode={mode}"
    z = meta_zip(bundle)
    hdrs: dict = {}
    try:
        ch = c.call("GET", f"/v1/submit/challenge?pub={device.pubkey_hex}", timeout=30)
    except RefereeError as e:
        ch = None
        if e.code != 404:
            say(f"PROBLEM: the referee refused a submit nonce (HTTP {e.code}): {e.body}")
            return 3
    if ch:
        t0 = time.monotonic()
        pw = solve_pow(ch["nonce"], device.pubkey_hex, int(ch.get("pow_bits", 0)))
        head = hashlib.sha256((bundle / "manifest.jsonl").read_bytes()).hexdigest()
        hdrs["X-Qor-Submit"] = submit_auth_header(device, path=path, body=z, bundle_head=head, nonce=ch["nonce"], pow_=pw)
        say(f"     signed with this PC's install key {device.pubkey_hex[:16]}... (proof-of-work {ch.get('pow_bits')} bits, "
            f"{time.monotonic() - t0:.1f} s)")
    say(f"1/4  Sending match files (no video yet) for an independent re-check ... [{mode}]")
    try:
        sub = c.call("POST", path, z, "application/zip", timeout=300, headers=hdrs)
    except RefereeError as e:
        body = e.body if isinstance(e.body, dict) else {"errors": [str(e.body)]}
        say(f"  REJECTED by the referee (HTTP {e.code}). Nothing was signed. Reasons:")
        for x in (body.get("errors") or [])[:15]:
            say(f"    - {x}")
        if e.code == 429:
            say("  (rate limit: new installs get a few submissions per hour/day; try again later)")
        save({"state": "rejected", "http": e.code, "errors": body.get("errors")})
        return 2
    c.token = sub["upload_token"]
    save({"submission": sub["id"], "state": sub["state"], "commit": sub.get("commit"), "required": len(sub["required"])})
    man = read_manifest(bundle)
    need = sub.get("required")
    if not isinstance(need, list) or any(isinstance(s, bool) or not isinstance(s, int) or s < 0 or s >= len(man) for s in need):
        say("\n  PROBLEM: the referee asked for a segment that is not in this manifest. Nothing else was uploaded.")
        save({"state": "upload_failed", "error": "segment index outside the manifest"})
        return 2
    total = sum(int(man[s]["bytes"]) for s in need)
    say(f"  accepted (commit {str(sub.get('commit'))[:12]}). The referee asked for {len(need)} of {len(man)} video "
        f"segments ({fmt_mb(total)}).")
    say(f"2/4  Uploading those segments; each is hashed against the manifest ...")
    sent, t0 = 0, time.monotonic()
    for i, s in enumerate(need):
        if isinstance(s, bool) or not isinstance(s, int) or s < 0 or s >= len(man):
            say(f"\n  PROBLEM: the referee asked for segment {s!r}, which is not in this manifest. Nothing else was uploaded.")
            save({"state": "upload_failed", "error": "segment index outside the manifest"})
            return 2
        try:
            data = segment_bytes(bundle, man[s])
        except (ValueError, OSError) as e:
            say(f"\n  PROBLEM: {e}. Refusing to read a file outside the bundle.")
            save({"state": "upload_failed", "error": str(e)})
            return 2
        for attempt in range(4):
            try:
                r = c.call("PUT", f"/v1/submissions/{sub['id']}/segments/{s}", data, "application/octet-stream",
                           timeout=300)
                break
            except (RefereeError, OSError) as e:
                if isinstance(e, RefereeError) and e.code in (401, 404, 409):
                    say(f"\n  PROBLEM: segment {s}: {e}")
                    save({"state": "upload_failed", "error": str(e)})
                    return 2
                if attempt == 3:
                    say(f"\n  PROBLEM: segment {s} failed 4 times ({e}). Run 'qorcap submit' again.")
                    save({"state": "upload_failed", "error": str(e)})
                    return 3
                time.sleep(2 * (attempt + 1))
        if not r.get("ok"):
            say(f"\n  Segment {s}: {r.get('error')}  (the video file no longer matches what was recorded)")
        sent += len(data)
        rate = sent / max(0.1, time.monotonic() - t0)
        sys.stdout.write(f"\r  {i + 1}/{len(need)} segments, {fmt_mb(sent)} of {fmt_mb(total)} ({rate / 1e6:.1f} MB/s)   ")
        sys.stdout.flush()
    say("")
    say("3/4  Referee review: decoding the recorded tail, re-reading the final, re-running challenges ...")
    try:
        rv = c.call("POST", f"/v1/submissions/{sub['id']}/review", b"", timeout=900)
    except RefereeError as e:
        say(f"  PROBLEM: review failed: {e}")
        save({"state": "review_error", "error": str(e)})
        return 3
    print_checks(rv.get("review"))
    if rv.get("state") != "awaiting_player_signature":
        say("\n  NOT VERIFIED: the referee refused to countersign (see the FAIL lines above). Nothing was signed.")
        for x in ((rv.get("review") or {}).get("post_sign_errors") or [])[:10]:
            say(f"    - {x}")
        save({"state": rv.get("state"), "review": rv.get("review")})
        return 2
    rec = rv["receipt"]
    if rv.get("log_entry"):
        say(f"     logged in the referee's public transparency log (entry #{rv['log_entry'].get('seq')})")
        save({"log_entry": rv["log_entry"]})
    say("4/4  Co-signing the referee's receipt with this PC's device key ...")
    from qor_receipt import receipt as R

    if rec["players"][0]["pubkey"] != device.pubkey_hex:
        say("PROBLEM: the receipt's player key is not this PC's device key (bundle signed on another PC?).")
        return 2
    done = c.post_json(f"/v1/submissions/{sub['id']}/player-signature", {"sig": device.sign(R.signing_bytes(rec))})
    final = done["receipt"]
    (bundle / out_name).write_text(json.dumps(final, indent=2), encoding="utf-8")
    (bundle / "referee.official.pub").write_text(final["referee"]["pubkey"] + "\n", encoding="ascii")
    v = R.verify_receipt(final, referee_pub=final["referee"]["pubkey"])
    (bundle / "verify.official.txt").write_text(json.dumps(v, indent=2), encoding="utf-8")
    save({"state": "complete", "verify": v})
    res = final["result"]
    say("\n" + "=" * 64)
    say(f"  OFFICIAL RESULT: {v['status']}   (QorTime referee {v.get('referee_id') or final['referee']['pubkey'][:16]})")
    say("=" * 64)
    sc = "not read" if res.get("home_score") is None else f"AWAY {res['away_score']} - HOME {res['home_score']}"
    say(f"  Score:     {sc}  ({res.get('method')})")
    say(f"  Basis:     {(final.get('review') or {}).get('scope')}")
    if v.get("partial"):
        say(f"  Missing:   {', '.join(v['partial'][:6])}")
    if v.get("errors"):
        say(f"  Problems:  {', '.join(v['errors'][:6])}")
    say("  Not proven: " + "; ".join((final.get("review") or {}).get("not_proven") or []))
    say(f"  Saved:     {bundle / out_name}")
    say("  Check it:  https://conwan30.github.io/qortime/verify/  (open receipt.official.json; no key needed)")
    return {"VERIFIED": 0, "PARTIAL": 1, "SELF_ATTESTED": 1}.get(v["status"], 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qorcap submit", description="Submit a match bundle to the QorTime official referee")
    ap.add_argument("bundle", nargs="?", help="match bundle folder (default: newest in Documents/QorCap Matches)")
    ap.add_argument("--referee", default=OFFICIAL_URL, help="referee service URL")
    ap.add_argument("--full", action="store_true", help="upload every video segment (1-3 GB) instead of a spot-check")
    ap.add_argument("--out", help="matches folder used to find the newest bundle")
    ap.add_argument("--qortime", help="QorTime checkout (default: the one Start Match uses)")
    ap.add_argument("--key", help="device key file (default: QorCap data folder keys/device.key)")
    ap.add_argument("--yes", action="store_true", help="do not ask before uploading")
    ap.add_argument("--qorcap", help=argparse.SUPPRESS)  # passed by the launchers; unused here
    ap.add_argument("--ffmpeg", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))
    from qorcap_match import find_qortime  # same pinned QorTime as Start Match

    qt = find_qortime(a.qortime)
    sys.path.insert(0, str(qt))
    root = Path(a.out) if a.out else Path.home() / "Documents" / "QorCap Matches"
    bundle = Path(a.bundle) if a.bundle else latest_bundle(root)
    if bundle is None or not bundle.exists():
        say(f"No signed match found in {root}. Pass the bundle folder: qorcap submit <folder>")
        return 2
    say("=" * 64)
    say("  QorCap  -  Submit for Verification (QorTime official referee)")
    say("=" * 64)
    say(f"Match: {bundle}")
    if not a.yes:
        say("This uploads the match files and part of the video (or all of it with --full) to the QorTime referee.")
        if input("Continue? [Y/n] ").strip().lower() in ("n", "no"):
            return 1
    return submit(bundle, base=a.referee, full=a.full, key_path=Path(a.key) if a.key else None)


if __name__ == "__main__":
    sys.exit(main())


# ------------------------------------------------------------------ live helpers used by Start Match
class OfficialLive:
    """During Start Match: timestamp the video chain with the official referee (anchors) and, if enabled, fetch the
    three official challenge nonces. Network failures never disturb the recording.

    v0.3.11: a challenge is requested only at a pause-capable moment (``qorcap_playcall.PauseCapableGate``: after
    the kickoff of that half/quarter and a play call), never on a timer. Online Madden 27 cannot pause during a
    kickoff or a kickoff return, which is why every v0.3.10 ``kickoff`` challenge (issued ~10 s into the recording)
    failed. Phases: ``kickoff`` in Q1-Q2, ``halftime`` in Q3, ``end`` in Q4/OT; at most one open window at a time; a
    phase whose quarter has passed is skipped (logged), never issued late at a kickoff. Each opened window is logged
    in ``challenge_gates.jsonl`` with its gate basis."""

    ANCHOR_EVERY_S = 60.0
    RETRY_S = 15.0

    def __init__(self, base: str, bundle: Path, match_id: str, role: str, api, *, challenges: bool = False,
                 client: Client | None = None, clock=time.monotonic, wall=lambda: int(time.time() * 1000),
                 gate=None, beep=None) -> None:
        self.c = client or Client(base, timeout=8)
        self.bundle, self.match_id, self.role, self.api = bundle, match_id, role, api
        self.challenges = challenges
        self.clock, self.wall = clock, wall
        self.gate = gate or (lambda: None)
        self.beep = beep if beep is not None else _beep
        self.last_anchor = None
        self.issued: set[str] = set()
        self.skipped: set[str] = set()
        self.anchors = 0
        self.failures = 0
        self.prompts: list[str] = []
        self.open_until_ms = 0      # wall ms: deadline of the window currently open
        self.used_event_ms = 0      # gate events at or before this were already used (or failed)
        self.retry_at = 0.0

    def _append(self, name: str, obj: dict) -> None:
        with open(self.bundle / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")

    def _anchor_body(self, got: dict) -> dict:
        return {"chain_head": got.get("chain_head"), "segment_seq": max(0, int(got.get("segments") or 0) - 1),
                "checkpoint_head": got.get("checkpoint_head")}

    def step(self) -> None:
        try:
            got = self.api.get("/checkpoints?since=999999999", timeout=2)
        except Exception:
            return
        if not isinstance(got.get("chain_head"), str) or set(got["chain_head"]) == {"0"}:
            return
        now = self.clock()
        if self.last_anchor is None or now - self.last_anchor >= self.ANCHOR_EVERY_S:
            self.last_anchor = now
            try:
                a = self.c.post_json("/v1/anchors", {"match_id": self.match_id, "anchor": self._anchor_body(got)})
                self._append("anchors.jsonl", a)
                self.anchors += 1
            except Exception:
                self.failures += 1
        if self.challenges:
            due = self._due_phase()
            if due:
                self._challenge(due[0], got, due[1])

    def _due_phase(self):
        """(phase, gate event) when a pause-capable moment for a not-yet-issued phase has just happened."""
        gate = self.gate()
        if gate is None or self.wall() < self.open_until_ms or self.clock() < self.retry_at:
            return None
        ev = gate.latest(self.used_event_ms)
        if ev is None:
            return None
        self.used_event_ms = ev.t_ms
        if ev.phase in self.issued:
            return None
        order = ["kickoff", "halftime", "end"]
        for earlier in order[: order.index(ev.phase)]:
            if earlier not in self.issued and earlier not in self.skipped:
                self.skipped.add(earlier)
                self._append("challenge_gates.jsonl", {"phase": earlier, "skipped": True, "why":
                              f"no pause-capable moment seen before Q{ev.quarter}", "t_ms": ev.t_ms})
        return ev.phase, ev

    def _challenge(self, phase: str, got: dict, ev=None) -> None:
        try:
            t = self.c.post_json("/v1/challenges", {"phase": phase, "role": self.role, "match_id": self.match_id,
                                                    "anchor": self._anchor_body(got)})
        except Exception as e:
            self.failures += 1
            self.retry_at = self.clock() + self.RETRY_S
            say(f"\n  (official {phase} challenge unavailable: {e}; will retry at the next play call)")
            return
        self.issued.add(phase)
        self.open_until_ms = int(t.get("deadline_ms") or 0)
        self._append("official_challenges.jsonl", t)
        gate_line = {"phase": phase, "nonce_id": t["nonce"][:8], "issued_ms": t["issued_ms"], "deadline_ms": t["deadline_ms"],
                     "prompt_wall_ms": self.wall()}
        if ev is not None:
            gate_line["gate"] = ev.to_dict()
        self._append("challenge_gates.jsonl", gate_line)
        try:
            self.api.post("/challenge", {"nonce": t["nonce"], "phase": phase, "role": self.role, "id": t["nonce"][:8],
                                         "issued_ms": t["issued_ms"], "deadline_ms": t["deadline_ms"]}, timeout=3)
        except Exception as e:
            self.failures += 1
            say(f"  (QorCap did not record the {phase} challenge: {e})")
        secs = max(1, (int(t["deadline_ms"]) - int(t["issued_ms"])) // 1000)
        label = {"kickoff": "game start", "halftime": "second half", "end": "4th quarter"}.get(phase, phase)
        msg = (f"\n\n  >>> QORTIME CHALLENGE ({label}): at the line, before the snap, press OPTIONS, then "
               f"{' '.join(t['pattern'][1:])}, then resume. You have {secs} seconds; if the snap comes first, "
               f"do it at the next play. <<<\n")
        self.prompts.append(msg)
        say(msg)
        try:
            self.beep()
        except Exception:
            pass


def _beep() -> None:
    if os.name == "nt":
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)


def start_official_live(live: OfficialLive, stop, interval: float = 1.0):
    import threading

    def loop():
        while not stop.is_set():
            try:
                live.step()
            except Exception:
                live.failures += 1
            stop.wait(interval)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
