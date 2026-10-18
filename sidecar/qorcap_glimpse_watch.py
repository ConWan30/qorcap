"""QorCap Glimpse shadow watcher: a LIVE, ADVISORY_AI, shadow-only second opinion on the scorebug.

    python sidecar/qorcap_glimpse_watch.py --layout monday_night        (or regular | primetime | london)

QorCap's local scorebug reader stays in charge of every score lock. This watcher never feeds a lock, a receipt,
the timeline, or anything QorCap writes, and it never touches recording:

* Frames come only from QorCap's opt-in, read-only, rate-limited ``GET /frame/scorebug`` (start QorCap with
  ``QORCAP_GLIMPSE_FRAME=1`` or ``record --glimpse-frame``). That endpoint hands out, from memory, the snapshot the
  reader already pulled; it opens no file. This watcher never opens ``.qorcap-run/snapshot.jpg`` or ``inputs.jsonl``
  (an in-process audit hook refuses it), and never writes, renames or deletes anything in the match folder.
* The only match-folder file it reads is the append-only ``timeline.jsonl`` (open, read the new bytes, close).
* A frame is sent to Glimpse only when the reader reports it unknown: no fresh agreed score row for that frame
  (score unknown) or a row whose quarter is null (quarter unknown). Known frames are never sent.
* Glimpse gets the scorebug crop, the layout, and the last locked score. Nothing else.
* Strict JSON schema; a badly formatted reply is recorded as unknown. A stats box stays labelled as stats and
  never yields a score. A score is "plausible" only if it is a legal football change from the last lock with
  consistent sides (QorTime/Jev ``implausible_transition_reason`` rules, plus +1 only after a +6).
* Budget: hard $0.50 per game (local ledger, worst case checked before every call) and a $10/week limit
  (OpenRouter /key usage_weekly, local ledger, floor; +margin if /key is unreadable). At most one request per
  ``--min-interval`` seconds (default 6), a 20 s hold after Glimpse confirms the lock, similar frames deduped, call cap per game, circuit breaker.
* Output: its own folder (default %LOCALAPPDATA%\\QorCapGlimpse\\<match>\\): advisory.jsonl, REPORT.md, crops/.
  Every record is tagged ADVISORY_AI / AI-assisted, advisory_only, feeds_lock=false.

OPENROUTER_API_KEY is read from the environment only and is never printed or logged.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "glimpse-watch/0.1.1"
ROLE_VERSION = "glimpse-shadow/role-v1"
TAG = "ADVISORY_AI"
LABEL = "AI-assisted advisory (Glimpse shadow). Not a score lock; QorCap's local reader decides."
HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "glimpse"
MODEL = "elorian/glimpse"
URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_URL = "https://openrouter.ai/api/v1/key"
KEY_ENV = "OPENROUTER_API_KEY"
PRICE_PROMPT = 0.6e-6       # $/token, elorian/glimpse on OpenRouter (2026-10-17)
PRICE_COMPLETION = 3.0e-6
CROP_PROMPT_TOKENS = 1000   # measured on 162055: ~$0.00076 per crop call
CROP_MAX_TOKENS = 200
CROP_BOX = (640, 720, 300, 980)  # y0, y1, x0, x1 on a 1280x720 frame (standard, SNP, TNP, MNP bars)
LAYOUTS = {
    "regular": "regular Madden broadcast bar",
    "primetime": "primetime bar (Sunday Night / Thursday Night)",
    "monday_night": "Monday Night bar (teal away panel)",
    "london": "London game bar",
    "unknown": "not specified",
}
FOOTBALL_SCORE_DELTAS = frozenset({1, 2, 3, 6, 7, 8})  # qor_receipt digit_integrity / QorCap football_step
STALE_MS = 2_500        # no agreed row within this long before the frame -> the reader has no score for it
READ_GRACE_MS = 1_000   # the reader's row for this very frame may land just after the frame time
PROMPT_SHA256 = {       # LF-normalised; ROLE/CROP/schema are byte-identical to glimpse-shadow v0.1.0
    "ROLE_v1.md": "c2680bd1f91a6ad51d282048d4396ff1e8880f97a755e6009b8b5a38468c6ef2",
    "CROP_TASK_v1.md": "62ffbac60336b0121366abbd6ce13cd3e921d56fe94d5de6d507ca03df1cf021",
    "crop_v1.json": "add18684f295f1695656fa11d0a51979218033d0bfd142cf13324becfdd98663",
    "LIVE_CONTEXT_v1.md": "6945d05649bdc3c9c0b928037b5c21f0cccc249e3cdaac02fa44fb28c6f5734b",
}


def _prompt(name: str) -> str:
    t = (PROMPTS / name).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(t).hexdigest() != PROMPT_SHA256[name]:
        raise SystemExit(f"glimpse prompt {name} does not match its pinned sha256; refusing to run")
    return t.decode("utf-8")


# ---------------------------------------------------------------------------------------------------------------
# File-access guard: refuse QorCap's live snapshot/inputs and any write inside the match folder (in-process).
# ---------------------------------------------------------------------------------------------------------------
class AccessViolation(PermissionError):
    pass


class _Policy:
    match_dir: str | None = None
    out_dir: str | None = None
    violations: list = []
    active = False


POLICY = _Policy()
_HOOKED = False
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _norm(p) -> str | None:
    try:
        if isinstance(p, int):
            return None
        p = os.fsdecode(os.fspath(p))
        return os.path.normcase(os.path.abspath(p))
    except Exception:
        return None


def _under(p: str, root: str | None) -> bool:
    return bool(root) and (p == root or p.startswith(root.rstrip("\\/") + os.sep))


def forbidden_read(p: str) -> str | None:
    parts = [x.lower() for x in Path(p).parts]
    if ".qorcap-run" in parts:
        return "QorCap run dir (.qorcap-run) is off limits"
    name = parts[-1] if parts else ""
    if name in ("snapshot.jpg", "inputs.jsonl") or name.endswith(".inputs.jsonl"):
        return f"QorCap live file {name} is off limits"
    return None


def check_access(event: str, args) -> None:
    if not POLICY.active:
        return
    if event == "open":
        p = _norm(args[0])
        if p is None:
            return
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 else 0
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & _WRITE_FLAGS)
        why = forbidden_read(p)
        if why is None and _under(p, POLICY.match_dir):
            if writes:
                why = "no writes inside the match folder"
            elif os.path.basename(p).lower() != "timeline.jsonl" and p != POLICY.match_dir:
                why = "only timeline.jsonl may be read from the match folder"
        if why:
            POLICY.violations.append((event, p, why))
            raise AccessViolation(f"glimpse watcher refused {p}: {why}")
    elif event in ("os.rename", "os.replace", "os.remove", "os.rmdir", "os.mkdir", "os.truncate", "os.unlink",
                   "shutil.rmtree", "shutil.move", "shutil.copyfile", "os.chmod", "os.utime"):
        for a in args[:2]:
            p = _norm(a)
            if p and (_under(p, POLICY.match_dir) or forbidden_read(p)):
                POLICY.violations.append((event, p, "match folder / QorCap files are read-only"))
                raise AccessViolation(f"glimpse watcher refused {event} on {p}")


def install_guard(match_dir, out_dir) -> None:
    global _HOOKED
    POLICY.match_dir = _norm(match_dir) if match_dir else None
    POLICY.out_dir = _norm(out_dir) if out_dir else None
    if POLICY.out_dir and POLICY.match_dir and (_under(POLICY.out_dir, POLICY.match_dir) or _under(POLICY.match_dir, POLICY.out_dir)):
        raise SystemExit("refusing: the glimpse output folder must be outside the match folder")
    if POLICY.out_dir and forbidden_read(POLICY.out_dir + os.sep + "x"):
        raise SystemExit("refusing: the glimpse output folder must be outside QorCap's run dir")
    POLICY.active = True
    if not _HOOKED:
        sys.addaudithook(check_access)
        _HOOKED = True


# ---------------------------------------------------------------------------------------------------------------
# Strict reply validation (glimpse-shadow validator.py, unchanged logic)
# ---------------------------------------------------------------------------------------------------------------
class Rejected(Exception):
    pass


_T = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _T[t])


def check(v, s, path="$"):
    types = s.get("type")
    types = [types] if isinstance(types, str) else types
    if types and not any(_is(v, t) for t in types):
        raise Rejected(f"{path}: type {type(v).__name__} not in {types}")
    if "enum" in s and v not in s["enum"]:
        raise Rejected(f"{path}: {v!r} not in enum")
    if isinstance(v, dict):
        miss = [k for k in s.get("required", []) if k not in v]
        if miss:
            raise Rejected(f"{path}: missing {miss}")
        props = s.get("properties", {})
        if s.get("additionalProperties") is False:
            extra = [k for k in v if k not in props]
            if extra:
                raise Rejected(f"{path}: extra keys {extra}")
        for k, sub in props.items():
            if k in v:
                check(v[k], sub, f"{path}.{k}")
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            raise Rejected(f"{path}: too few items")
        if "maxItems" in s and len(v) > s["maxItems"]:
            raise Rejected(f"{path}: too many items")
        for i, x in enumerate(v):
            if "items" in s:
                check(x, s["items"], f"{path}[{i}]")
    if isinstance(v, str):
        if "maxLength" in s and len(v) > s["maxLength"]:
            raise Rejected(f"{path}: too long")
        if "pattern" in s and not re.search(s["pattern"], v):
            raise Rejected(f"{path}: pattern")
    if _is(v, "number"):
        if "minimum" in s and v < s["minimum"]:
            raise Rejected(f"{path}: < minimum")
        if "maximum" in s and v > s["maximum"]:
            raise Rejected(f"{path}: > maximum")


def parse_strict(text, schema):
    if not isinstance(text, str) or not text.strip():
        raise Rejected("empty reply")
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        raise Rejected("reply is not a bare JSON object (prose/markdown/fences rejected)")
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise Rejected(f"invalid JSON: {e.msg}")
    check(obj, schema)
    return obj


# ---------------------------------------------------------------------------------------------------------------
# Plausibility (Jev-style): legal football change from the last lock, consistent sides
# ---------------------------------------------------------------------------------------------------------------
def _last_change_was_td(hist, side):
    vals = [x[side] for x in hist]
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] != vals[i - 1]:
            return vals[i] - vals[i - 1] == 6
    return False


def plausible_score(lock, hist, away, home):
    """(ok, flags). lock = {"away","home"} last local-reader lock; hist = chronological distinct locks."""
    if lock is None:
        return False, ["no_reference_lock"]
    la, lh = lock["away"], lock["home"]
    if (away, home) == (la, lh):
        return True, ["agrees_with_lock"]
    if (away, home) == (lh, la):
        return False, ["sides_swapped_vs_lock"]
    da, dh = away - la, home - lh
    if da < 0 or dh < 0:
        return False, ["score_decrease_vs_lock"]
    if da and dh:
        return False, ["both_sides_changed_vs_lock"]
    side, d = ("away", da) if da else ("home", dh)
    if d not in FOOTBALL_SCORE_DELTAS:
        return False, [f"{side}_delta_{d}_not_a_football_score"]
    if d == 1 and not _last_change_was_td(hist, side):
        return False, [f"{side}_plus1_without_td"]
    return True, ["legal_change_vs_lock"]


QUARTER_STALE_MS = 5 * 60 * 1000  # the reader's last real quarter is this old: a jump of more than one is allowed


def plausible_quarter(q, max_quarter, max_quarter_age_ms=None):
    """A quarter never goes backwards. It moves up one at a time, unless the reader's own quarter is stale (it
    stopped reading quarters long ago, e.g. 162055 read 1ST and then null for the rest of the game)."""
    if q is None:
        return False, []
    if max_quarter is not None and q < max_quarter:
        return False, ["quarter_backwards_vs_reader"]
    if max_quarter is not None and q > max_quarter + 1:
        if max_quarter_age_ms is not None and max_quarter_age_ms > QUARTER_STALE_MS:
            return True, ["quarter_jump_reader_quarter_stale"]
        return False, ["quarter_skips_vs_reader"]
    return True, []


def assess(obj, lock, hist, max_quarter, max_quarter_age_ms=None):
    """obj = validated crop/v1 reply or None. Returns dict(region, advisory_score, advisory_quarter, stat, flags)."""
    out = {"region": "UNKNOWN", "advisory_score": None, "advisory_quarter": None, "advisory_clock": None,
           "plausible": False, "stat": None, "flags": []}
    if obj is None:
        out["flags"] = ["rejected_or_unknown"]
        return out
    region = out["region"] = obj["region"]
    if region != "SCOREBUG":
        if any(obj[k] is not None for k in ("away_score", "home_score", "quarter", "clock")):
            out["flags"].append("score_fields_on_non_scorebug_suppressed")
        if region == "STATS_OVERLAY":
            out["flags"].append("stats_overlay_no_score")
            out["stat"] = {"title": obj["stat_title"], "values": obj["stat_values"]}
        return out
    if obj["stat_title"] is not None or obj["stat_values"] is not None:
        out["flags"].append("scorebug_with_stat_title_suppressed")
        out["stat"] = {"title": obj["stat_title"], "values": obj["stat_values"]}
        return out
    a, h = obj["away_score"], obj["home_score"]
    if a is None or h is None:
        out["flags"].append("score_not_read")
    else:
        ok, why = plausible_score(lock, hist, a, h)
        out["flags"] += why
        if ok:
            out["advisory_score"], out["plausible"] = {"away": a, "home": h}, True
    qok, qwhy = plausible_quarter(obj["quarter"], max_quarter, max_quarter_age_ms)
    out["flags"] += qwhy
    if qok and out["plausible"]:
        out["advisory_quarter"] = obj["quarter"]
    if out["plausible"]:
        out["advisory_clock"] = obj["clock"]
    return out


# ---------------------------------------------------------------------------------------------------------------
# Budget + OpenRouter client (glimpse-shadow budget.py / client.py, with injectable clock and in-memory images)
# ---------------------------------------------------------------------------------------------------------------
class BudgetRefused(Exception):
    pass


def worst_case(prompt_tokens_est=CROP_PROMPT_TOKENS, max_tokens=CROP_MAX_TOKENS):
    return prompt_tokens_est * PRICE_PROMPT * 1.25 + max_tokens * PRICE_COMPLETION


class Budget:
    def __init__(self, ledger_path, match_id, per_game_cap=0.50, weekly_limit=10.0, weekly_floor=0.0,
                 unknown_usage_margin=0.50, key_check_every_s=120, key_reader=None):
        self.path, self.match_id = Path(ledger_path), match_id
        self.per_game_cap, self.weekly_limit, self.weekly_floor = per_game_cap, weekly_limit, weekly_floor
        self.margin, self.every = unknown_usage_margin, key_check_every_s
        self._lock = threading.Lock()
        self._remote, self._remote_t = None, 0.0
        self.key_reader = key_reader or self._read_key_usage
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _ledger(self):
        if not self.path.exists():
            return []
        rows = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        return rows

    def game_spent(self):
        return sum(r.get("cost", 0) or 0 for r in self._ledger() if r.get("match_id") == self.match_id)

    def week_spent_local(self):
        cut = time.time() - 7 * 86400
        return sum(r.get("cost", 0) or 0 for r in self._ledger() if r.get("t", 0) >= cut)

    @staticmethod
    def _read_key_usage():
        k = os.environ.get(KEY_ENV)
        if not k:
            return None
        req = urllib.request.Request(KEY_URL, headers={"Authorization": "Bearer " + k})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())["data"]
        return float(d.get("usage_weekly") if d.get("usage_weekly") is not None else d.get("usage", 0))

    def remote_weekly(self):
        if self._remote is not None and time.time() - self._remote_t < self.every:
            return self._remote
        try:
            self._remote = self.key_reader()
            self._remote_t = time.time()
        except Exception:
            self._remote = None
        return self._remote

    def week_spent(self):
        remote = self.remote_weekly()
        week = max(self.weekly_floor, self.week_spent_local(), remote if remote is not None else 0.0)
        return week + (self.margin if remote is None else 0.0), remote

    def authorise(self, worst):
        with self._lock:
            g = self.game_spent()
            if g + worst > self.per_game_cap:
                raise BudgetRefused(f"per-game cap: spent ${g:.4f} + worst ${worst:.4f} > ${self.per_game_cap:.2f}")
            week, remote = self.week_spent()
            if week + worst > self.weekly_limit:
                raise BudgetRefused(f"weekly limit: ${week:.4f} + worst ${worst:.4f} > ${self.weekly_limit:.2f}")
            return {"game_spent": g, "week_spent": week, "remote_weekly": remote}

    def record(self, cost, gen_id, kind):
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "match_id": self.match_id, "cost": cost, "gen_id": gen_id, "kind": kind}) + "\n")


def openrouter_post(body: dict, timeout: float) -> dict:
    key = os.environ.get(KEY_ENV)
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + (key or ""), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Client:
    def __init__(self, budget: Budget, timeout_s=12.0, breaker_failures=3, breaker_cooldown_s=300, max_calls=500,
                 post=None, clock=time):
        self.b, self.timeout, self.max_calls = budget, timeout_s, max_calls
        self.breaker_failures, self.cooldown = breaker_failures, breaker_cooldown_s
        self.post, self.clock = post or openrouter_post, clock
        self._fails, self._open_until, self.calls = 0, 0.0, 0

    def ask(self, system, task, image_jpeg: bytes, schema, kind="crop_live"):
        """dict(text, cost, latency_s, gen_id, error, refused). Never raises; a failure is 'unknown'."""
        out = {"text": None, "cost": 0.0, "latency_s": None, "gen_id": None, "error": None, "refused": None}
        if self.clock.time() < self._open_until:
            out["refused"] = "circuit_open"
            return out
        if self.calls >= self.max_calls:
            out["refused"] = "max_calls"
            return out
        if not os.environ.get(KEY_ENV) and self.post is openrouter_post:
            out["refused"] = "no_key"
            return out
        try:
            self.b.authorise(worst_case())
        except BudgetRefused as e:
            out["refused"] = f"budget: {e}"
            return out
        sch = {k: v for k, v in schema.items() if k != "$id"}
        body = {"model": MODEL, "temperature": 0, "max_tokens": CROP_MAX_TOKENS, "reasoning": {"enabled": False},
                "usage": {"include": True},
                "response_format": {"type": "json_schema", "json_schema": {"name": "crop", "strict": True, "schema": sch}},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": [{"type": "text", "text": task},
                                                          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode()}}]}]}
        self.calls += 1
        t = time.perf_counter()
        try:
            d = self.post(body, self.timeout)
            out["latency_s"] = round(time.perf_counter() - t, 3)
            u = d.get("usage") or {}
            out["cost"] = float(u.get("cost") or 0.0)
            out["gen_id"] = d.get("id")
            ch = d.get("choices") or []
            out["text"] = ch[0]["message"].get("content") if ch else None
            self.b.record(out["cost"], out["gen_id"], kind)
            self._fails = 0
            return out
        except urllib.error.HTTPError as e:
            out["error"] = f"HTTP {e.code}"  # body intentionally not stored
        except Exception as e:  # timeout, DNS, reset
            out["error"] = type(e).__name__
        out["latency_s"] = round(time.perf_counter() - t, 3)
        self._fails += 1
        if self._fails >= self.breaker_failures:
            self._open_until = self.clock.time() + self.cooldown
            out["error"] += " (circuit opened)"
        # Failed requests are not billed; /key usage_weekly catches any discrepancy. Worst case is still booked
        # against the game so a flapping provider cannot quietly exceed the cap.
        self.b.record(worst_case(), None, kind + ":failed_worst_case")
        return out


# ---------------------------------------------------------------------------------------------------------------
# Sources: QorCap's read-only frame tap and the append-only timeline
# ---------------------------------------------------------------------------------------------------------------
class FrameTap:
    def __init__(self, base="http://127.0.0.1:8788", timeout=2.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    def get(self):
        """(code, jpeg|None, headers dict lower-case). code 0 = QorCap not reachable."""
        try:
            with urllib.request.urlopen(self.base + "/frame/scorebug", timeout=self.timeout) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            return e.code, None, {k.lower(): v for k, v in (e.headers or {}).items()}
        except Exception:
            return 0, None, {}


class TimelineTail:
    """Reads only the bytes appended since the last poll; opens and closes the file each time (no held handle)."""

    def __init__(self, path):
        self.path, self.off, self.buf = Path(path), 0, b""
        self.scores: list[dict] = []
        self.locks: list[dict] = []
        self.max_quarter = None
        self.max_quarter_t_ms = None

    def poll(self):
        try:
            size = os.stat(self.path).st_size
        except OSError:
            return 0
        if size < self.off:  # replaced/truncated: start over
            self.__init__(self.path)
        if size == self.off:
            return 0
        with open(self.path, "rb") as f:
            f.seek(self.off)
            data = f.read(min(size - self.off, 4 << 20))
        self.off += len(data)
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        n = 0
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "obs" in r or not isinstance(r.get("home"), int) or not isinstance(r.get("away"), int):
                continue
            n += 1
            self.scores.append(r)
            if not self.locks or (self.locks[-1]["away"], self.locks[-1]["home"]) != (r["away"], r["home"]):
                self.locks.append({"away": r["away"], "home": r["home"], "t_ms": r.get("t_ms")})
            q = r.get("quarter")
            if isinstance(q, int) and not isinstance(q, bool) and q >= 1:
                self.max_quarter = q if self.max_quarter is None else max(self.max_quarter, q)
                self.max_quarter_t_ms = r.get("t_ms")
        return n

    def state_at(self, frame_ms):
        """('known'|'score_unknown'|'quarter_unknown', last row at/before the frame, locks up to the frame)."""
        rows = [r for r in self.scores if (r.get("t_ms") or 0) <= frame_ms + READ_GRACE_MS]
        last = rows[-1] if rows else None
        locks = [x for x in self.locks if (x.get("t_ms") or 0) <= frame_ms + READ_GRACE_MS]
        if last is None or frame_ms - (last.get("t_ms") or 0) > STALE_MS:
            return "score_unknown", last, locks
        if last.get("quarter") is None:
            return "quarter_unknown", last, locks
        return "known", last, locks


def find_match_dir(header_bundle=None, explicit=None):
    for c in (explicit, header_bundle):
        if c and Path(c).is_dir():
            return Path(c)
    root = Path.home() / "Documents" / "QorCap Matches"
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and (d / "timeline.jsonl").exists()]
    except OSError:
        return None
    return max(dirs, key=lambda d: (d / "timeline.jsonl").stat().st_mtime) if dirs else None


def default_out_root():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "QorCapGlimpse"


# ---------------------------------------------------------------------------------------------------------------
# Image helpers (cv2 + numpy only: both ship in QorCap's venv)
# ---------------------------------------------------------------------------------------------------------------
def crop_scorebug(jpeg: bytes):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, None
    f = cv2.resize(img, (1280, 720))
    y0, y1, x0, x1 = CROP_BOX
    c = f[y0:y1, x0:x1]
    up = cv2.resize(c, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    ok, enc = cv2.imencode(".jpg", up, [cv2.IMWRITE_JPEG_QUALITY, 95])
    thumb = cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), (170, 20), interpolation=cv2.INTER_AREA).astype("float32")
    return (enc.tobytes() if ok else None), thumb


def thumb_diff(a, b) -> float:
    import numpy as np
    return float(np.mean(np.abs(a - b)))


# ---------------------------------------------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------------------------------------------
class Watcher:
    def __init__(self, a, tap=None, clock=time, post=None, key_reader=None, printer=print):
        self.a, self.tap, self.clock, self._print = a, tap or FrameTap(a.qorcap), clock, printer
        self.post, self.key_reader = post, key_reader
        self.system = _prompt("ROLE_v1.md")
        self.task = _prompt("CROP_TASK_v1.md")
        self.ctx = _prompt("LIVE_CONTEXT_v1.md")
        self.schema = json.loads(_prompt("crop_v1.json"))
        self.prompt_sha = hashlib.sha256((self.system + self.task + self.ctx).encode()).hexdigest()[:16]
        self.layout = a.layout
        self.match_dir = self.tail = self.out = self.budget = self.client = None
        self.match_id = None
        self.last_seq = None
        self.last_call_t = -1e18
        self.last_thumb = None
        self.last_thumb_t = -1e18
        self.other_streak = 0
        self.confirmed = None  # (lock, t) of Glimpse's last read that agreed with the lock and read a quarter
        self.c = {k: 0 for k in ("frames_seen", "known_skipped", "score_unknown", "quarter_unknown", "sent", "rate_skipped",
                                 "dup_skipped", "hold_skipped", "refused", "errors", "rejected", "scorebug", "stats_overlay", "other",
                                 "plausible", "agrees_with_lock", "legal_change", "implausible", "tap_off", "tap_429",
                                 "tap_503", "tap_down")}
        self.regions_seen: list = []
        self.week = None
        self.started = clock.time()

    def say(self, msg):
        self._print(msg)
        if self.out is not None:  # detached runs: the same lines in the watcher's own folder
            try:
                with open(self.out / "watch.log", "a", encoding="utf-8") as f:
                    f.write(f"{_dt.datetime.now().strftime('%H:%M:%S')} {msg}\n")
            except OSError:
                pass

    # -- setup once the match folder is known
    def _open_match(self, hdr_bundle):
        md = find_match_dir(hdr_bundle, self.a.match_dir)
        if md is None:
            return False
        self.match_dir = md.resolve()
        self.match_id = self.a.match_id or self.match_dir.name
        out_root = Path(self.a.out).resolve() if self.a.out else default_out_root().resolve()
        self.out = out_root / self.match_id
        install_guard(self.match_dir, self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        if self.a.save_crops:
            (self.out / "crops").mkdir(exist_ok=True)
        self.tail = TimelineTail(self.match_dir / "timeline.jsonl")
        self.budget = Budget(out_root / "ledger.jsonl", self.match_id, per_game_cap=self.a.game_cap, weekly_limit=self.a.weekly_limit,
                             weekly_floor=self.a.weekly_floor, key_reader=self.key_reader)
        self.client = Client(self.budget, timeout_s=self.a.timeout, max_calls=self.a.max_calls, post=self.post, clock=self.clock)
        self.say(f"[glimpse] {LABEL}")
        self.say(f"[glimpse] match {self.match_id}; layout {self.layout}; output {self.out}")
        self.say(f"[glimpse] reading only {self.match_dir / 'timeline.jsonl'} (append-only) and GET /frame/scorebug")
        return True

    def _emit(self, rec):
        with open(self.out / "advisory.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def run(self):
        end = self.clock.time() + self.a.max_minutes * 60
        last_ok = None
        while self.clock.time() < end:
            code, jpeg, hdr = self.tap.get()
            now = self.clock.time()
            if code == 200 and jpeg:
                last_ok = now
                try:
                    self.on_frame(jpeg, hdr)
                except AccessViolation:
                    raise
                except Exception as e:  # never die on one frame
                    self.c["errors"] += 1
                    self.say(f"[glimpse] frame skipped: {type(e).__name__}")
            elif code == 404:
                self.c["tap_off"] += 1
                if self.c["tap_off"] in (1, 30) or self.c["tap_off"] % 300 == 0:
                    self.say("[glimpse] QorCap's frame tap is off. Start QorCap with QORCAP_GLIMPSE_FRAME=1 (needs a QorCap build with GET /frame/scorebug).")
            elif code == 429:
                self.c["tap_429"] += 1
            elif code == 0:
                self.c["tap_down"] += 1
                ref = last_ok if last_ok is not None else self.started
                if now - ref > self.a.idle_exit_s:
                    self.say(f"[glimpse] QorCap not reachable for {self.a.idle_exit_s:.0f} s; stopping.")
                    break
            else:
                self.c["tap_503"] += 1
            if self.out is not None and self.c["refused"] and self.budget is not None and self._budget_exhausted():
                self.say("[glimpse] budget reached; stopping (recording is unaffected).")
                break
            self.clock.sleep(self.a.poll)
        self.write_report(final=True)
        return self.c

    def _budget_exhausted(self):
        return self.budget.game_spent() + worst_case() > self.a.game_cap

    def on_frame(self, jpeg, hdr):
        seq = hdr.get("x-qorcap-frame-seq")
        if seq is not None and seq == self.last_seq:
            return
        self.last_seq = seq
        try:
            frame_ms = int(hdr.get("x-qorcap-frame-wall-ms"))
        except (TypeError, ValueError):
            frame_ms = int(self.clock.time() * 1000)
        if self.match_dir is None and not self._open_match(hdr.get("x-qorcap-bundle")):
            return
        self.c["frames_seen"] += 1
        self.clock.sleep(self.a.read_grace)  # let the reader's row for this frame land
        self.tail.poll()
        state, last, locks = self.tail.state_at(frame_ms)
        if state == "known":
            self.c["known_skipped"] += 1
            return
        self.c[state] += 1
        now = self.clock.time()
        # Three "no scorebug" replies in a row (replay, menu, pause): slow down until the picture changes state.
        interval = self.a.min_interval if self.other_streak < 3 else max(self.a.min_interval, self.a.other_backoff)
        # Glimpse just confirmed the reader's lock: hold off until the lock changes or the hold runs out. A frame
        # whose state is unknown all game (162055: quarter null from 8:12) would otherwise cost a call every few s.
        lock_now = (locks[-1]["away"], locks[-1]["home"]) if locks else None
        if self.confirmed is not None and self.confirmed[0] == lock_now and now - self.confirmed[1] < self.a.confirm_hold:
            self.c["hold_skipped"] += 1
            return
        if now - self.last_call_t < interval:
            self.c["rate_skipped"] += 1
            return
        crop, thumb = crop_scorebug(jpeg)
        if crop is None:
            self.c["errors"] += 1
            return
        if self.last_thumb is not None and now - self.last_thumb_t < self.a.dedupe_window and thumb_diff(thumb, self.last_thumb) < self.a.dedupe_threshold:
            self.c["dup_skipped"] += 1
            return
        lock = locks[-1] if locks else None
        task = self.task + self.ctx.format(layout=LAYOUTS.get(self.layout, self.layout),
                                           last_lock=(f"AWAY {lock['away']} - HOME {lock['home']}" if lock else "none yet"))
        self.last_call_t = now
        r = self.client.ask(self.system, task, crop, self.schema)
        if r["refused"]:
            self.c["refused"] += 1
        else:
            self.last_thumb, self.last_thumb_t = thumb, now
            self.c["sent"] += 1
        if r["error"]:
            self.c["errors"] += 1
        obj, rej = None, None
        if r["text"] is not None:
            try:
                obj = parse_strict(r["text"], self.schema)
            except Rejected as e:
                rej = str(e)
                self.c["rejected"] += 1
        qage = None if self.tail.max_quarter_t_ms is None else frame_ms - self.tail.max_quarter_t_ms
        res = assess(obj, lock, locks, self.tail.max_quarter, qage)
        if not r["refused"]:
            self.other_streak = self.other_streak + 1 if res["region"] == "OTHER" else 0
            k = {"SCOREBUG": "scorebug", "STATS_OVERLAY": "stats_overlay", "OTHER": "other"}.get(res["region"])
            if k:
                self.c[k] += 1
            agree = res["plausible"] and "agrees_with_lock" in res["flags"] and res["advisory_quarter"] is not None
            self.confirmed = (lock_now, now) if agree else None
            if res["plausible"]:
                self.c["plausible"] += 1
                self.c["agrees_with_lock" if "agrees_with_lock" in res["flags"] else "legal_change"] += 1
            elif res["region"] == "SCOREBUG" and obj and obj["away_score"] is not None and obj["home_score"] is not None:
                self.c["implausible"] += 1
        item = f"seq{seq}_{frame_ms}"
        if self.a.save_crops and not r["refused"]:
            (self.out / "crops" / f"{item}.jpg").write_bytes(crop)
        g = self.budget.game_spent()
        rec = {"tag": TAG, "ai_assisted": True, "advisory_only": True, "feeds_lock": False, "label": LABEL,
               "role_version": ROLE_VERSION, "tool_version": VERSION, "prompt_sha": self.prompt_sha, "model": MODEL,
               "kind": "crop_live", "item_id": item, "frame_wall_ms": frame_ms, "frame_seq": seq, "t_wall": time.time(),
               "match_id": self.match_id, "layout": self.layout, "local_state": state,
               "local_last_row": last, "reference_lock": lock,
               "valid": obj is not None, "reject_reason": rej, "model_reply": obj, **res,
               "cost": r["cost"], "game_spent": round(g, 6), "latency_s": r["latency_s"], "gen_id": r["gen_id"],
               "error": r["error"], "refused": r["refused"], "raw": (r["text"] or "")[:600]}
        self._emit(rec)
        self.regions_seen.append(rec)
        sug = res["advisory_score"]
        self.say(f"[glimpse] {_hhmmss(frame_ms)} {state:<15} -> {res['region']:<13} "
                 f"{('suggest ' + str(sug['away']) + '-' + str(sug['home'])) if sug else ','.join(res['flags'])[:60]:<40} "
                 f"Q{res['advisory_quarter'] if res['advisory_quarter'] else '?'}  game ${g:.4f}/{self.a.game_cap:.2f}")
        self.write_report()

    def write_report(self, final=False):
        if self.out is None:
            return
        g = self.budget.game_spent()
        try:
            week, remote = self.budget.week_spent()
        except Exception:
            week, remote = None, None
        c = self.c
        lines = [f"# Glimpse shadow watch: {self.match_id}", "",
                 f"**{LABEL}** Shadow-only: nothing here feeds QorCap's locks, receipt, timeline or bundle.", "",
                 f"- Tool: {VERSION}, model `{MODEL}`, prompt sha `{self.prompt_sha}`, layout `{self.layout}`",
                 f"- Updated: {_dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}{' (final)' if final else ' (live)'}",
                 f"- **Cost this game: ${g:.4f}** of ${self.a.game_cap:.2f} cap; week ${week if week is None else round(week, 4)} of ${self.a.weekly_limit:.2f}"
                 f"{'' if remote is not None else ' (OpenRouter /key unreadable: local ledger + margin)'}", "",
                 "| Frames from the tap | Known (skipped) | Score unknown | Quarter unknown | Sent to Glimpse | Rate-skipped | Deduped | Held (just confirmed) | Refused | Errors | Bad replies |",
                 "|---|---|---|---|---|---|---|---|---|---|---|",
                 f"| {c['frames_seen']} | {c['known_skipped']} | {c['score_unknown']} | {c['quarter_unknown']} | {c['sent']} | {c['rate_skipped']} | {c['dup_skipped']} | {c['hold_skipped']} | {c['refused']} | {c['errors']} | {c['rejected']} |", "",
                 f"Regions: scorebug {c['scorebug']}, stats box {c['stats_overlay']}, other {c['other']}. "
                 f"Plausible scores {c['plausible']} ({c['agrees_with_lock']} agree with the lock, {c['legal_change']} legal changes); implausible {c['implausible']}.", "",
                 "## Latest advisory reads", "", "| Frame time | Reader state | Region | Glimpse read | Suggestion | Flags |", "|---|---|---|---|---|---|"]
        for rec in self.regions_seen[-25:]:
            m = rec["model_reply"] or {}
            read = f"{m.get('away_score')}-{m.get('home_score')} Q{m.get('quarter')} {m.get('clock')}" if rec["region"] == "SCOREBUG" else (
                f"stat {m.get('stat_title')} {m.get('stat_values')}" if rec["region"] == "STATS_OVERLAY" else "")
            s = rec["advisory_score"]
            lines.append(f"| {_hhmmss(rec['frame_wall_ms'])} | {rec['local_state']} | {rec['region']} | {read} | "
                         f"{(str(s['away']) + '-' + str(s['home'])) if s else ''} | {', '.join(rec['flags'])} |")
        try:
            tmp = self.out / "REPORT.md.tmp"
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.out / "REPORT.md")
        except OSError:
            pass  # e.g. REPORT.md open in an editor; try again next time


def _hhmmss(ms):
    try:
        return _dt.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")
    except Exception:
        return "?"


def lower_priority():
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        else:
            os.nice(19)
    except Exception:
        pass
    try:
        import cv2
        cv2.setNumThreads(1)
    except Exception:
        pass


def build_parser():
    ap = argparse.ArgumentParser(prog="qorcap_glimpse_watch", description="Live Glimpse shadow watcher (ADVISORY_AI, shadow-only)")
    ap.add_argument("--layout", choices=sorted(LAYOUTS), default="unknown", help="scorebug layout shown to Glimpse")
    ap.add_argument("--qorcap", default="http://127.0.0.1:8788", help="QorCap control API")
    ap.add_argument("--match-dir", help="match folder (default: from QorCap, else newest in Documents/QorCap Matches)")
    ap.add_argument("--match-id", help="ledger/report id (default: match folder name)")
    ap.add_argument("--out", help="output root (default %%LOCALAPPDATA%%\\QorCapGlimpse); must be outside the match folder")
    ap.add_argument("--game-cap", type=float, default=0.50)
    ap.add_argument("--weekly-limit", type=float, default=10.0)
    ap.add_argument("--weekly-floor", type=float, default=0.67, help="known spend this week if /key is unreadable")
    ap.add_argument("--min-interval", type=float, default=6.0, help="seconds between Glimpse requests")
    ap.add_argument("--confirm-hold", type=float, default=20.0, help="after Glimpse agrees with the lock (with a quarter), seconds to wait unless the lock changes")
    ap.add_argument("--poll", type=float, default=2.5, help="seconds between frame-tap polls")
    ap.add_argument("--read-grace", type=float, default=0.8, help="wait before checking the reader's row for a frame")
    ap.add_argument("--dedupe-threshold", type=float, default=2.0, help="mean abs grey diff (0-255) below which a crop is a repeat")
    ap.add_argument("--dedupe-window", type=float, default=30.0, help="seconds a repeat stays suppressed")
    ap.add_argument("--other-backoff", type=float, default=15.0, help="seconds between requests after 3 'no scorebug' replies in a row")
    ap.add_argument("--max-calls", type=int, default=500)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--max-minutes", type=float, default=180)
    ap.add_argument("--idle-exit-s", type=float, default=300, help="stop when QorCap has been unreachable this long")
    ap.add_argument("--no-save-crops", dest="save_crops", action="store_false")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    lower_priority()
    if not os.environ.get(KEY_ENV):
        print(f"[glimpse] {KEY_ENV} is not set; nothing will be sent (frames are still counted).")
    Watcher(a).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
