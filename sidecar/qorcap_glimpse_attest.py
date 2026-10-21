"""Post-match Glimpse observer attestation, with clip audit as a later state.

Cheap tier: sign the sidecar ($0). Clip audit is its own flag and stays off unless
``QORCAP_GLIMPSE_CLIP_AUDIT=1``. Nothing here writes ``receipt.json``, highlight bytes,
or a touchdown verdict file. The signature uses
``%LOCALAPPDATA%\\QorCap\\keys\\glimpse-observer.key`` and refuses the device or referee key.

States: REFUSE_RECORDING, NEED_RECEIPT, PIN, CLIP_OFF or CLIP_NEXT / CLIP_RESERVE / CLIP_CALL /
CLIP_COMPARE, SEAL, SIGN, COPY, DONE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import qorcap_glimpse_side as side

SCHEMA = "qorcap-glimpse-attestation/v0"
KEY_KIND = "glimpse-observer"
FORBIDDEN = frozenset({"away_score", "home_score", "winner", "trust_tier", "signatures"})
LOCK_FRESH_S = 30.0
WALL_S = 600.0
MAX_CLIPS = 15
FRAMES_PER_CLIP = 1


def default_key_dir() -> Path:
    """Same directory as the device and referee keys, so a copied seed is refused."""
    from qorcap_paths import key_dir
    return key_dir()


def observer_key_path(key_dir=None) -> Path:
    return Path(key_dir) / "glimpse-observer.key" if key_dir else default_key_dir() / "glimpse-observer.key"


def jcs(obj) -> bytes:
    """RFC 8785 JSON Canonicalization (no floats; object keys ordered by UTF-16 code units)."""
    return _jcs(obj).encode("utf-8")


def _jcs(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        raise ValueError("attestation JCS refuses floats")
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, list):
        return "[" + ",".join(_jcs(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted((str(k) for k in v.keys()), key=lambda s: s.encode("utf-16-be"))
        return "{" + ",".join(_jcs(k) + ":" + _jcs(v[k]) for k in keys) + "}"
    raise TypeError(type(v).__name__)


def _seed(path: Path):
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return bytes.fromhex(raw)
    return None


def _pubkey(seed: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _sign(seed: bytes, payload: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(seed).sign(payload)


def _verify(pub: bytes, payload: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = Ed25519PublicKey.from_public_bytes(pub)
    try:
        key.verify(sig, payload)
        return True
    except InvalidSignature:
        return False


def init_key(path: Path) -> int:
    """Create the observer seed. Does not sign anything and does not overwrite an existing key."""
    path = Path(path)
    from qorcap_paths import private_dir
    private_dir(path.parent)
    if path.exists():
        print(f"[glimpse-attest] observer key already exists at {path}")
        return 0
    path.write_text(os.urandom(32).hex() + "\n", encoding="utf-8")
    from qorcap_paths import restrict_private
    restrict_private(path)
    print(f"[glimpse-attest] wrote observer key {path} (key_kind {KEY_KIND})")
    return 0


def _collides(observer: Path, key_dir: Path) -> bool:
    try:
        op = observer.resolve()
    except OSError:
        op = observer
    oseed = _seed(observer) if observer.is_file() else None
    opub = _pubkey(oseed) if oseed else None
    for name in ("device.key", "referee.key"):
        other = key_dir / name
        if not other.exists():
            continue
        try:
            if other.resolve() == op:
                return True
        except OSError:
            pass
        aseed = _seed(other)
        if oseed and aseed and oseed == aseed:
            return True
        if opub and aseed:
            try:
                if opub == _pubkey(aseed):
                    return True
            except Exception:
                pass
    return False


def _sha_file(path: Path):
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(name: str, path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"name": name, "sha256": None, "bytes": 0, "present": False}
    b = path.read_bytes()
    return {"name": name, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b), "present": True}


def _chain_head(match: Path):
    for name in ("checkpoints.jsonl", "score_checkpoints.jsonl"):
        p = match / name
        if not p.is_file():
            continue
        try:
            last = None
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    last = json.loads(line)
            if isinstance(last, dict) and isinstance(last.get("hash"), str):
                return last["hash"]
            return "unreadable"
        except (OSError, ValueError):
            return "unreadable"
    return None


def _lock_fresh(match: Path, now: float) -> bool:
    p = match / ".qorcap-run" / "recording.lock"
    try:
        return p.is_file() and (now - p.stat().st_mtime) < LOCK_FRESH_S
    except OSError:
        return False


def _clips(manifest: dict) -> list:
    out = []
    for c in manifest.get("clips") or []:
        if not isinstance(c, dict):
            continue
        lock = c.get("lock") or {}
        score = lock.get("score_after") or {}
        out.append({"file": c.get("file"), "sha256": c.get("sha256"), "away": score.get("away"), "home": score.get("home")})
    return out


def _compare(obj, away, home) -> str:
    if not obj or obj.get("region") != "SCOREBUG" or obj.get("away_score") is None or obj.get("home_score") is None:
        return "no_read"
    if (obj["away_score"], obj["home_score"]) == (away, home):
        return "agree"
    return "disagree"


def verify_attestation(doc: dict, receipt: bytes) -> bool:
    """True when status observed, the signature checks, and the receipt pin matches these bytes."""
    if not isinstance(doc, dict) or doc.get("status") != "observed" or doc.get("signed") is not True:
        return False
    if doc.get("receipt_sha256") != hashlib.sha256(receipt).hexdigest():
        return False
    sig = doc.get("signature")
    if not isinstance(sig, dict) or sig.get("key_kind") != KEY_KIND:
        return False
    body = {k: v for k, v in doc.items() if k != "signature"}
    if FORBIDDEN & body.keys():
        return False
    try:
        pub = bytes.fromhex(sig.get("public_key") or "")
        raw = bytes.fromhex(sig.get("sig") or "")
    except ValueError:
        return False
    return _verify(pub, jcs(body), raw)


def run_attest(match_dir, out_dir, *, key_dir=None, now=None, post=None, extract_frame=None, key_reader=None) -> int:
    """Run the state machine. Returns 3 when a fresh recording lock refuses every write, else 0."""
    match = Path(match_dir)
    out = Path(out_dir)
    now_s = time.time() if now is None else float(now)
    deadline = time.monotonic() + WALL_S
    want_sign = side.env_on(side.ATTEST_ENV)
    want_clip = side.env_on(side.CLIP_ENV)
    state = "REFUSE_RECORDING"
    receipt_raw = None
    chain = None
    partial = False
    audit_path = out / "highlights_audit.jsonl"
    att_path = out / "glimpse_attestation.json"

    while state != "DONE":
        if state == "REFUSE_RECORDING":
            if _lock_fresh(match, now_s):
                return 3
            state = "NEED_RECEIPT"
        elif state == "NEED_RECEIPT":
            rp = match / "receipt.json"
            try:
                receipt_raw = rp.read_bytes()
                json.loads(receipt_raw.decode("utf-8"))
            except (OSError, UnicodeError, ValueError):
                out.mkdir(parents=True, exist_ok=True)
                att_path.write_text(json.dumps({"schema": SCHEMA, "status": "receipt_absent", "signed": False}) + "\n",
                                    encoding="utf-8")
                return 0
            state = "PIN"
        elif state == "PIN":
            chain = _chain_head(match)
            state = "CLIP_NEXT" if want_clip else "CLIP_OFF"
        elif state == "CLIP_OFF":
            state = "SEAL"
        elif state == "CLIP_NEXT":
            if time.monotonic() > deadline:
                partial = True
                state = "SEAL"
                continue
            state = "CLIP_RESERVE"
        elif state in ("CLIP_RESERVE", "CLIP_CALL", "CLIP_COMPARE"):
            _run_clips(match, out, audit_path, post=post, extract_frame=extract_frame, key_reader=key_reader, deadline=deadline)
            if time.monotonic() > deadline:
                partial = True
            state = "SEAL"
        elif state == "SEAL":
            out.mkdir(parents=True, exist_ok=True)
            body = _body(match, out, receipt_raw, chain, partial, audit_path)
            state = "SIGN"
            _sign_body(body, observer_key_path(key_dir), Path(key_dir) if key_dir else default_key_dir(), want_sign)
            if FORBIDDEN & body.keys():
                raise RuntimeError("attestation body carried a forbidden key")
            att_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
            state = "COPY"
        elif state == "COPY":
            if side.env_on(side.COPY_ENV):
                target = match / "glimpse_attestation.json"
                target.write_bytes(att_path.read_bytes())
            state = "DONE"
        else:
            raise RuntimeError(f"unknown attest state {state}")
    return 0


def _body(match, out, receipt_raw, chain, partial, audit_path) -> dict:
    manifest = match / "highlights" / "manifest.json"
    return {
        "schema": SCHEMA,
        "status": "unsigned",
        "signed": False,
        "match_id": match.name,
        "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
        "observer_key_id": None,
        "created_ms": int(time.time() * 1000),
        "partial": bool(partial),
        "chain": chain,
        "artifacts": [
            _artifact("advisory.jsonl", out / "advisory.jsonl"),
            _artifact("burst.jsonl", out / "burst.jsonl"),
            _artifact("highlights_audit.jsonl", audit_path),
            _artifact("highlights/manifest.json", manifest),
        ],
    }


def _sign_body(body: dict, key_path: Path, key_dir: Path, want_sign: bool) -> None:
    if not want_sign:
        body["reason"] = "attest_flag_off"
        return
    seed = _seed(key_path) if key_path.is_file() else None
    if seed is None:
        body["reason"] = "no_observer_key"
        return
    if _collides(key_path, key_dir):
        body["reason"] = "key_collides_with_authority"
        return
    pub = _pubkey(seed)
    body["signed"] = True
    body["status"] = "observed"
    body["observer_key_id"] = hashlib.sha256(pub).hexdigest()[:16]
    payload = jcs(body)
    sig = _sign(seed, payload)
    body["signature"] = {"alg": "Ed25519", "key_kind": KEY_KIND, "public_key": pub.hex(), "sig": sig.hex()}


def _run_clips(match, out, audit_path, *, post, extract_frame, key_reader, deadline) -> None:
    import qorcap_glimpse_watch as W
    manifest_path = match / "highlights" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    clips = _clips(manifest)[:MAX_CLIPS]
    if not clips:
        return
    out.mkdir(parents=True, exist_ok=True)
    game = side.resolve_money(side.GAME_ENV, 0.50)
    week = side.resolve_money(side.WEEK_ENV, 10.0)
    s_clip = side.resolve_money(side.CLIP_CAP_ENV, side.S_CLIP_DEFAULT)
    caps_ok = side.finite_positive(game) and side.finite_positive(week) and side.finite_positive(s_clip) and s_clip <= game
    budget = client = None
    if caps_ok:
        budget = W.Budget(out.parent / "ledger.jsonl", match.name, per_game_cap=game, weekly_limit=week,
                          weekly_floor=0.0, key_reader=key_reader or (lambda: 0.0))
        client = W.Client(budget, timeout_s=12.0, post=post, clock=time)
    system = W._prompt("ROLE_v1.md")
    task_base = W._prompt("CROP_TASK_v1.md")
    ctx = W._prompt("LIVE_CONTEXT_v1.md")
    schema = json.loads(W._prompt("crop_v1.json"))
    layout = W.LAYOUTS.get("unknown", "not specified")
    task = task_base + ctx.format(layout=layout, last_lock=side.LOCK_WITHHELD)
    stopped = not caps_ok
    reason = None if caps_ok else "cap_unconfigured"
    extract = extract_frame or _extract_ffmpeg
    with open(audit_path, "a", encoding="utf-8") as fh:
        for clip in clips:
            if time.monotonic() > deadline:
                stopped, reason = True, "wall_clock"
            if stopped:
                fh.write(json.dumps(_audit_row(clip, "not_run", reason, None, None)) + "\n")
                continue
            lock = {"away": clip["away"], "home": clip["home"]} if isinstance(clip["away"], int) else None
            if not side.task_withholds_lock(task, lock):
                fh.write(json.dumps(_audit_row(clip, "no_read", "prompt_guard", None, None)) + "\n")
                continue
            frame = None
            try:
                from qorcap_paths import bundle_child
                clip_path = bundle_child(match / "highlights", clip.get("file"))
                frame = extract(clip_path) if clip_path is not None else None
            except Exception:
                frame = None
            if not frame:
                fh.write(json.dumps(_audit_row(clip, "no_read", "extract_failed", None, None)) + "\n")
                continue
            calls = 0
            last = None
            while calls < FRAMES_PER_CLIP:
                r = client.ask(system, task, frame, schema, kind="clip_audit", stratum="clip_audit", stratum_cap=s_clip)
                calls += 1
                last = r
                if r.get("refused"):
                    break
            if last and isinstance(last.get("refused"), str) and (last["refused"] == "cap_unconfigured" or last["refused"].startswith("budget")):
                why = "cap_unconfigured" if last["refused"] == "cap_unconfigured" else "budget"
                fh.write(json.dumps(_audit_row(clip, "not_run", why, None, last.get("refused"))) + "\n")
                stopped, reason = True, why
                continue
            obj, rej = None, None
            text = (last or {}).get("text")
            if text:
                try:
                    obj = W.parse_strict(text, schema)
                except W.Rejected as e:
                    rej = str(e)
            verdict = "no_read" if obj is None else _compare(obj, clip["away"], clip["home"])
            fh.write(json.dumps(_audit_row(clip, verdict, rej or ("extract_failed" if not frame else None), obj, None)) + "\n")


def _audit_row(clip, verdict, reason, obj, refused) -> dict:
    return {"tag": "ADVISORY_AI", "ai_assisted": True, "advisory_only": True, "feeds_lock": False,
            "kind": "clip_audit", "status": "not_run" if verdict == "not_run" else "read",
            "file": clip.get("file"), "clip_sha256": clip.get("sha256"),
            "verdict": verdict, "reason": reason, "model_reply": obj, "refused": refused,
            "reference_lock": {"away": clip.get("away"), "home": clip.get("home")}}


def _extract_ffmpeg(path: Path):
    import subprocess
    import tempfile
    if not path.is_file():
        return None
    fd, name = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        proc = subprocess.run(["ffmpeg", "-y", "-i", str(path), "-frames:v", "1", name],
                              capture_output=True, timeout=60)
        if proc.returncode != 0:
            return None
        data = Path(name).read_bytes()
        return data if data.startswith(b"\xff\xd8") else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.remove(name)
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qorcap_glimpse_attest", description="Sign a Glimpse observer sidecar after the match")
    ap.add_argument("--match-dir")
    ap.add_argument("--out", help="Glimpse folder for this match (default: QorCapGlimpse/<match>)")
    ap.add_argument("--key-dir", help="directory that holds glimpse-observer.key (and the authority keys to refuse)")
    ap.add_argument("--init-key", action="store_true", help="create the observer key and exit; does not sign")
    a = ap.parse_args(argv)
    if a.init_key:
        return init_key(observer_key_path(a.key_dir))
    if not side.env_on(side.ATTEST_ENV) and not side.env_on(side.CLIP_ENV):
        print("[glimpse-attest] off (QORCAP_GLIMPSE_ATTEST and QORCAP_GLIMPSE_CLIP_AUDIT are not 1); nothing written")
        return 0
    if not a.match_dir:
        print("[glimpse-attest] --match-dir is required")
        return 2
    match = Path(a.match_dir)
    out = Path(a.out) if a.out else (Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "QorCapGlimpse" / match.name)
    return run_attest(match, out, key_dir=a.key_dir)


if __name__ == "__main__":
    raise SystemExit(main())
