#!/usr/bin/env python3
"""PWEC-2 Carbon reader. Info only. jev is false.

One generateContent call per window, model gemini-4-carbon. Neon is not used.
generationConfig is fixed: maxOutputTokens 2000, temperature 1.0,
thinkingConfig.thinkingBudget 1024. The key is read from GEMINI_API_KEY and
sent only in the x-goog-api-key header. It is never placed in a URL, a file,
or a log line.

Spend is ESTIMATED from billed usageMetadata at $2.00 per 1M input tokens and
$10.00 per 1M output tokens. Thinking tokens are output. Before every
generateContent call the worst case is (countTokens + 512) at the input price
plus (2000 + 1024) at the output price. The running total must not go above
$10.00. A missing usageMetadata is UNMEASURED and is charged at that worst case.

Retry once (a1) only on MAX_TOKENS or a response that does not parse. If that
retry fails the same way, the read is UNMEASURED. There is no second retry.
HTTP 429 stops the run (FREEZE). HTTP 5xx stops the run. HTTP 400 stops the
run. A 400 that names thinkingConfig does not drop that field.

Inputs: --data, a directory with MANIFEST.json and the packaged frames.
Outputs: --out. Ledger lines are appended. Raw responses and reads are written
once. Calibration frames are not sent. This file makes no call on import.

Vocabulary: MEASURED, ESTIMATED, UNMEASURED, UNKNOWN.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import cv2
import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import side_hsv  # noqa: E402

MODEL = "gemini-4-carbon"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
PRICE_IN = Decimal("2.00")
PRICE_OUT = Decimal("10.00")
CAP_USD = Decimal("10.00")
INPUT_SLACK = 512
MAX_OUTPUT_TOKENS = 2000
THINKING_BUDGET = 1024
OUTPUT_WORST = MAX_OUTPUT_TOKENS + THINKING_BUDGET
JPEG_QUALITY = 95
GUTTER = 8
TIMEOUT_S = 180.0

_QUANT = Decimal("0.000001")
_SCRUB = re.compile(r"AIza[0-9A-Za-z_\-]{8,}|key=[^\s&\"']+", re.IGNORECASE)


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1)
    day = 1 + (6 - first.weekday()) % 7 + (n - 1) * 7
    return datetime(year, month, day)


def _chicago_dst_utc(year: int) -> tuple[datetime, datetime]:
    """UTC instants when US DST starts and ends. Rules in force since 2007."""
    start = _nth_sunday(year, 3, 2).replace(hour=2) + timedelta(hours=6)
    end = _nth_sunday(year, 11, 1).replace(hour=2) + timedelta(hours=5)
    return start, end


class _Chicago(tzinfo):
    """America/Chicago without the tzdata package, so Windows can import this."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=-6)
        wall = dt.replace(tzinfo=None)
        start, end = _chicago_dst_utc(wall.year)
        as_dst = wall + timedelta(hours=5)
        as_std = wall + timedelta(hours=6)
        dst_ok = start <= as_dst < end
        std_ok = not (start <= as_std < end)
        if dst_ok and std_ok:
            return timedelta(hours=-6) if dt.fold else timedelta(hours=-5)
        if dst_ok:
            return timedelta(hours=-5)
        return timedelta(hours=-6)

    def dst(self, dt: datetime | None) -> timedelta:
        offset = self.utcoffset(dt)
        return timedelta(hours=1) if offset == timedelta(hours=-5) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "CDT" if self.dst(dt) else "CST"

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc requires this tzinfo")
        utc = dt.replace(tzinfo=None)
        start, end = _chicago_dst_utc(utc.year)
        if start <= utc < end:
            wall = utc + timedelta(hours=-5)
            fold = 0
        else:
            wall = utc + timedelta(hours=-6)
            # The first hour after the fall-back is the second 1:00.
            fold = 1 if end <= utc < end + timedelta(hours=1) else 0
        return wall.replace(tzinfo=self, fold=fold)


_CST = _Chicago()

ENUMS = {
    "target_present": {"yes", "no", "unsure"},
    "side": {"enemy", "teammate", "none", "unsure"},
    "tracked": {"yes", "no", "unsure"},
    "shot_at": {"yes", "no", "unsure"},
    "hitmarker_seen": {"yes", "no", "unsure"},
    "kill_confirmed": {"yes", "no", "unsure"},
    "escaped": {"yes", "no", "unsure"},
}
READ_KEYS = ("n", "target_present", "side", "tracked", "shot_at", "hitmarker_seen", "kill_confirmed", "escaped")
ROLE_ORDER = {"PRE": 0, "ADS": 1, "FIRE": 2, "PEAK": 3, "RELEASE": 4, "POST": 5}


class StopRun(Exception):
    """Stop the run. code 4 is FREEZE, 5 is STOP, 3 is the cap, 6 is the package."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def scrub(text: str, secret: str | None = None) -> str:
    """Remove key material from anything that might be printed or written."""
    cleaned = _SCRUB.sub("[redacted]", text)
    if secret and secret in cleaned:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(_QUANT, rounding=ROUND_HALF_UP)


def generation_config() -> dict:
    """The only generateContent config. Callers must not delete thinkingConfig."""
    return {
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "temperature": 1.0,
        "thinkingConfig": {"thinkingBudget": THINKING_BUDGET},
        "responseMimeType": "application/json",
    }


def endpoint(method: str) -> str:
    return f"{API_ROOT}/{MODEL}:{method}"


def worst_case_usd(counted: int) -> Decimal:
    """(countTokens + 512) at $2/1M plus (2000 + 1024) at $10/1M."""
    if type(counted) is not int or counted < 0:
        raise ValueError("counted tokens must be an integer >= 0")
    raw = (Decimal(counted + INPUT_SLACK) * PRICE_IN + Decimal(OUTPUT_WORST) * PRICE_OUT) / Decimal(1_000_000)
    return _q(raw)


def would_exceed(running: Decimal, counted: int) -> bool:
    return running + worst_case_usd(counted) > CAP_USD


def billed_usd(usage: object) -> tuple[Decimal | None, str]:
    """Spend from usageMetadata. Thinking is billed as output. Mark ESTIMATED.

    Returns (None, UNMEASURED) when the billed fields are missing.
    """
    if not isinstance(usage, dict):
        return None, "UNMEASURED"
    if "promptTokenCount" not in usage or "candidatesTokenCount" not in usage:
        return None, "UNMEASURED"
    prompt = usage["promptTokenCount"]
    candidates = usage["candidatesTokenCount"]
    thoughts = usage.get("thoughtsTokenCount", 0)
    if thoughts is None:
        thoughts = 0
    if any(type(part) is not int or part < 0 for part in (prompt, candidates, thoughts)):
        return None, "UNMEASURED"
    raw = (Decimal(prompt) * PRICE_IN + Decimal(candidates + thoughts) * PRICE_OUT) / Decimal(1_000_000)
    return _q(raw), "ESTIMATED"


def now_cst(when: datetime | None = None) -> str:
    """Ledger clock. America/Chicago wall time, labeled CST or CDT from tzname()."""
    now = when.astimezone(_CST) if when is not None else datetime.now(_CST)
    zone = now.tzname() or ("CDT" if now.dst() else "CST")
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {zone}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prompt_text() -> str:
    colour = (_HERE / "prompts" / "colour_cues.txt").read_text(encoding="utf-8")
    window = (_HERE / "prompts" / "read_window.txt").read_text(encoding="utf-8")
    return colour.rstrip() + "\n\n" + window.rstrip() + "\n"


def check_expected_shas(specs: list[str]) -> None:
    """Refuse to call when a pinned sha does not match this checkout."""
    known = {
        "colour_cues.txt": _HERE / "prompts" / "colour_cues.txt",
        "read_window.txt": _HERE / "prompts" / "read_window.txt",
        "run_carbon_pwec2.py": _HERE / "run_carbon_pwec2.py",
    }
    for spec in specs:
        if "=" not in spec:
            raise StopRun("expect-sha must look like name=hex", 2)
        name, want = spec.split("=", 1)
        path = known.get(name)
        if path is None:
            raise StopRun("expect-sha name is not a pinned file", 2)
        got = file_sha256(path)
        if got != want.strip().lower():
            raise StopRun(f"sha mismatch for {name}", 2)


def compose_panel(bgr: np.ndarray) -> np.ndarray:
    """Left: minimap tile. Right: centre crop. 8 px black gutter. No burned-in text.

    The centre crop is a copy. The kill-feed residual, weapon overlap, and
    compass leak are zeroed the same way classify_frame zeros them, before
    the crop is stitched, so a red plate in those bands never reaches the
    panel Carbon sees.
    """
    side_hsv._require_frame(bgr)
    mx0, my0, mx1, my1 = side_hsv.MINIMAP_TILE
    cx0, cy0, cx1, cy1 = side_hsv.CENTRE_CROP
    mini = bgr[my0:my1, mx0:mx1]
    crop = bgr[cy0:cy1, cx0:cx1].copy()
    side_hsv._zero(crop, side_hsv.CROP_FEED_MASK)
    side_hsv._zero(crop, side_hsv.CROP_WEAPON)
    side_hsv._zero(crop, side_hsv.CROP_COMPASS_LEAK)
    panel = np.zeros((crop.shape[0], mini.shape[1] + GUTTER + crop.shape[1], 3), np.uint8)
    panel[0:mini.shape[0], 0:mini.shape[1]] = mini
    panel[:, mini.shape[1] + GUTTER:] = crop
    return panel


def _jpeg(panel: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", panel, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise StopRun("could not encode a panel", 6)
    return buf.tobytes()


def parse_read(text: str, expected_n: int) -> dict | None:
    """Constrained JSON. None means no-parse."""
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or set(obj) != {"windows"}:
        return None
    windows = obj["windows"]
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
        return None
    window = windows[0]
    if set(window) != set(READ_KEYS):
        return None
    if type(window["n"]) is not int or window["n"] != expected_n:
        return None
    for key, allowed in ENUMS.items():
        if window[key] not in allowed:
            return None
    if not _prompt_invariants_hold(window):
        return None
    return obj


def _prompt_invariants_hold(window: dict) -> bool:
    """read_window.txt invariants.

    side is none iff target_present is no; otherwise side is enemy,
    teammate, or unsure. kill_confirmed=yes and escaped=yes cannot
    stand together (escaped is no when a red X is shown).
    """
    present = window["target_present"]
    side = window["side"]
    if present == "no":
        if side != "none":
            return False
    elif side not in {"enemy", "teammate", "unsure"}:
        return False
    if window["kill_confirmed"] == "yes" and window["escaped"] == "yes":
        return False
    return True


def _under(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise StopRun("a frame path escapes the data directory", 6)
    return path


def load_windows(data: Path) -> list[tuple[int, str, list[dict]]]:
    manifest_path = data / "MANIFEST.json"
    if not manifest_path.is_file():
        raise StopRun("MANIFEST.json is missing", 6)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StopRun("MANIFEST.json does not parse", 6) from exc
    if not isinstance(manifest, dict):
        raise StopRun("MANIFEST.json is not an object", 6)
    frames = manifest.get("frames")
    if frames is None:
        frames = []
    if not isinstance(frames, list):
        raise StopRun("MANIFEST.json frames is not a list", 6)
    groups: dict[str, list[dict]] = {}
    for frame in frames:
        if not isinstance(frame, dict):
            raise StopRun("a MANIFEST.json frame is not an object", 6)
        if frame.get("set") != "window":
            continue
        wid = frame.get("window_id")
        if not isinstance(wid, str) or not wid:
            raise StopRun("a window frame has no window_id", 6)
        groups.setdefault(wid, []).append(frame)
    ordered = []
    for wid, items in groups.items():
        items = sorted(items, key=lambda row: (row.get("t_us") or 0, ROLE_ORDER.get(row.get("role"), 99), row.get("id") or ""))
        orders = [item.get("window_order") for item in items]
        if any(type(item) is not int for item in orders):
            raise StopRun("a window has no integer window_order", 6)
        if len(set(orders)) != 1:
            raise StopRun("a window has inconsistent window_order values", 6)
        ordered.append((orders[0], wid, items))
    ordered.sort(key=lambda row: (row[0], row[1]))
    return ordered


def _load_frame(data: Path, row: dict) -> np.ndarray:
    rel = row.get("package_path")
    if not isinstance(rel, str) or not rel:
        raise StopRun("a frame has no package_path", 6)
    path = _under(data, rel)
    if not path.is_file():
        raise StopRun("a frame file is missing", 6)
    expect = row.get("sha256")
    if not isinstance(expect, str):
        raise StopRun("a frame has no sha256", 6)
    expect = expect.strip().lower()
    if len(expect) != 64:
        raise StopRun("a frame has no sha256", 6)
    got = file_sha256(path)
    if got != expect:
        raise StopRun("a frame sha256 does not match the manifest", 6)
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise StopRun("a frame could not be read", 6)
    try:
        side_hsv._require_frame(bgr)
    except ValueError as exc:
        raise StopRun(str(exc), 6) from exc
    return bgr


def build_contents(order: int, frames: list[tuple[str, bytes]]) -> list[dict]:
    parts: list[dict] = [{
        "text": (
            prompt_text()
            + f"\nThis request is window n={order}. Return that n.\n"
            + f"Frames follow in time order. There are {len(frames)} frames.\n"
        )
    }]
    for index, (role, jpeg) in enumerate(frames, start=1):
        parts.append({"text": f"frame {index} role {role}"})
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode("ascii")}})
    return [{"role": "user", "parts": parts}]


def response_text(body: dict) -> tuple[str, str | None]:
    if not isinstance(body, dict):
        return "", None
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return "", None
    cand = candidates[0]
    if not isinstance(cand, dict):
        return "", None
    finish = cand.get("finishReason")
    finish_label = finish if isinstance(finish, str) else None
    content = cand.get("content")
    if content is None:
        parts = []
    elif not isinstance(content, dict):
        return "", finish_label
    else:
        parts = content.get("parts")
        if parts is None:
            parts = []
        elif not isinstance(parts, list):
            return "", finish_label
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("thought"):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(texts), finish_label


def _thinking_rejected(status: int, payload: str) -> bool:
    if status != 400:
        return False
    low = payload.lower()
    return "thinkingconfig" in low or "thinkingbudget" in low or "thinking_budget" in low


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    if "AIza" in url or "key=" in url.lower():
        raise StopRun("refusing a request URL that carries key material", 5)

    class _NoKeyRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            if "AIza" in newurl or "key=" in newurl.lower():
                raise StopRun("refusing a redirect that carries key material", 5)
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)

    opener = urllib.request.build_opener(_NoKeyRedirect)
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with opener.open(request, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise StopRun("transport error", 5) from exc


def append_ledger(path: Path, record: dict, secret: str) -> None:
    line = scrub(json.dumps(record, sort_keys=True, separators=(",", ":")), secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def running_total(path: Path) -> Decimal:
    if not path.is_file():
        return Decimal("0")
    total = Decimal("0")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise StopRun("LEDGER.jsonl has a truncated or invalid line", 5)
            cost = rec.get("cost_usd")
            if cost not in (None, ""):
                amount = Decimal(str(cost))
                if not amount.is_finite() or amount < 0:
                    raise StopRun("LEDGER.jsonl has a truncated or invalid line", 5)
                total += amount
    except StopRun:
        raise
    except (json.JSONDecodeError, ValueError, TypeError, ArithmeticError) as exc:
        raise StopRun("LEDGER.jsonl has a truncated or invalid line", 5) from exc
    return total


_BLOCKING_LEDGER = {"PWEC-CALL", "PWEC-CAP", "PWEC-FREEZE", "PWEC-STOP"}


def _call_id(order: int, digest: str, attempt: str) -> str:
    return f"w{order:04d}-{digest}-{attempt}"


def blocking_ledger_calls(path: Path) -> set[str]:
    """Call ids that already have a non-seed CALL/CAP/FREEZE/STOP line.

    A seed row used only to pre-load the running total has no `call` field
    and is ignored, so test_seeded_ledger_is_part_of_the_running_total still
    reaches countTokens and the cap check.
    """
    found: set[str] = set()
    if not path.is_file():
        return found
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise StopRun("LEDGER.jsonl has a truncated or invalid line", 5)
            if rec.get("event_code") not in _BLOCKING_LEDGER:
                continue
            call = rec.get("call")
            if isinstance(call, str) and call:
                found.add(call)
    except StopRun:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise StopRun("LEDGER.jsonl has a truncated or invalid line", 5) from exc
    return found


def _guard_fresh_out(out: Path, windows: list[tuple[int, str, list[dict]]], ledger: Path) -> None:
    """Refuse a re-run before any network call, including countTokens."""
    blocked = blocking_ledger_calls(ledger)
    for order, wid, _rows in windows:
        digest = hashlib.sha256(wid.encode("utf-8")).hexdigest()[:8]
        read_path = out / "reads" / f"w{order:04d}-{digest}.json"
        if read_path.exists():
            raise StopRun("refusing to overwrite an existing output", 5)
        for attempt in ("a0", "a1"):
            call = _call_id(order, digest, attempt)
            raw_path = out / "raw" / f"{call}.json"
            if raw_path.exists():
                raise StopRun("refusing to overwrite an existing output", 5)
            if call in blocked:
                raise StopRun("LEDGER.jsonl already has a result for this call", 5)


def _write_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise StopRun("refusing to overwrite an existing output", 5) from exc
    with handle:
        handle.write(text)


def _money(amount: Decimal) -> str:
    return format(_q(amount), "f")


def _base_record(call: str, attempt: str) -> dict:
    return {
        "time_cst": now_cst(),
        "call": call,
        "attempt": attempt,
        "model": MODEL,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking_budget": THINKING_BUDGET,
        "temperature": 1.0,
        "price_per_1M_in_out": [2.0, 10.0],
        "jev": False,
        "cost_mark": "ESTIMATED",
    }


def _guard_out(out: Path) -> None:
    """Ledgers and raw reads stay out of the public tree unless gitignored."""
    repo = _HERE.parents[1]
    try:
        rel = out.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    if rel.parts and rel.parts[0] in ("pwec-data", "private"):
        return
    raise StopRun("refusing to write outputs inside the repo outside pwec-data or private", 2)


def run_reads(data: Path, out: Path, *, transport, api_key: str, expect_sha: list[str] | None = None) -> int:
    """Run the window reads. transport(url, headers, body, timeout) -> (status, bytes)."""
    try:
        return _run_reads(data, out, transport=transport, api_key=api_key, expect_sha=expect_sha)
    except StopRun as exc:
        print(scrub(str(exc), api_key or ""), file=sys.stderr)
        return int(exc.code)


def _run_reads(data: Path, out: Path, *, transport, api_key: str, expect_sha: list[str] | None = None) -> int:
    if not api_key:
        raise StopRun("GEMINI_API_KEY is not set", 2)
    check_expected_shas(expect_sha or [])
    data = data.resolve()
    out = out.resolve()
    _guard_out(out)
    windows = load_windows(data)
    ledger = out / "LEDGER.jsonl"
    running = running_total(ledger)
    _guard_fresh_out(out, windows, ledger)

    def emit(msg: str) -> None:
        print(scrub(msg, api_key), flush=True)

    def log(record: dict) -> None:
        append_ledger(ledger, record, api_key)

    for order, wid, rows in windows:
        digest = hashlib.sha256(wid.encode("utf-8")).hexdigest()[:8]
        frames = []
        for row in rows:
            bgr = _load_frame(data, row)
            role = row.get("role") if isinstance(row.get("role"), str) else "UNKNOWN"
            frames.append((role, _jpeg(compose_panel(bgr))))
        contents = build_contents(order, frames)
        read_path = out / "reads" / f"w{order:04d}-{digest}.json"
        parsed = None
        final_disposition = "UNMEASURED"
        final_reason = None
        for attempt in ("a0", "a1"):
            call = _call_id(order, digest, attempt)
            counted = _count_tokens(transport, api_key, contents, call, log, emit)
            worst = worst_case_usd(counted)
            if would_exceed(running, counted):
                log({
                    **_base_record(call, attempt),
                    "event_code": "PWEC-CAP",
                    "disposition": "STOP",
                    "counted_input_tokens": counted,
                    "worst_case_usd": _money(worst),
                    "running_total_usd": _money(running),
                    "cost_usd": "0.000000",
                    "cost_basis": "stopped before the call; worst case would pass $10.00 ESTIMATED",
                })
                _write_read(read_path, wid, order, "UNMEASURED", None, "cap", api_key)
                emit(f"{call} CAP running={_money(running)} worst={_money(worst)}")
                return 3
            status, payload, body = _generate(transport, api_key, contents, call)
            cost, mark, basis = _charge(body, worst)
            running = _q(running + cost)
            text, finish = response_text(body) if status == 200 else ("", None)
            raw_path = out / "raw" / f"{call}.json"
            _write_once(raw_path, scrub(_dump(body if body else {"http_status": status, "body": payload}), api_key))
            retry = False
            disposition = "STOP"
            if status == 429:
                disposition = "FREEZE"
            elif status >= 500:
                disposition = "STOP"
            elif status == 400:
                disposition = "STOP"
            elif status != 200:
                disposition = "STOP"
            elif finish == "MAX_TOKENS":
                retry = attempt == "a0"
                disposition = "RETRY" if retry else "UNMEASURED"
                if not retry:
                    final_reason = "MAX_TOKENS"
            elif finish == "STOP":
                parsed = parse_read(text, order)
                if parsed is None:
                    retry = attempt == "a0"
                    disposition = "RETRY" if retry else "UNMEASURED"
                    if not retry:
                        final_reason = "no_parse"
                else:
                    disposition = "PARSED"
            else:
                # SAFETY, RECITATION, OTHER, a missing finish, or any other
                # reason. Do not parse the body and do not retry.
                parsed = None
                retry = False
                disposition = "UNMEASURED"
                final_reason = finish if isinstance(finish, str) and finish else "missing finish"
            record = {
                **_base_record(call, attempt),
                "event_code": "PWEC-FREEZE" if status == 429 else ("PWEC-STOP" if disposition == "STOP" else "PWEC-CALL"),
                "disposition": disposition,
                "http_status": status,
                "finish_reason": finish,
                "counted_input_tokens": counted,
                "input_tokens": (body.get("usageMetadata") or {}).get("promptTokenCount") if isinstance(body, dict) else None,
                "output_tokens": (body.get("usageMetadata") or {}).get("candidatesTokenCount") if isinstance(body, dict) else None,
                "thinking_tokens": (body.get("usageMetadata") or {}).get("thoughtsTokenCount") if isinstance(body, dict) else None,
                "parsed_ok": disposition == "PARSED",
                "cost_usd": _money(cost),
                "cost_mark": mark,
                "cost_basis": basis,
                "worst_case_usd": _money(worst),
                "running_total_usd": _money(running),
                "thinking_config_kept": True,
            }
            log(record)
            emit(f"{call} {disposition} http={status} cost_mark={mark} running={_money(running)}")
            if status == 429:
                _write_read(read_path, wid, order, "UNMEASURED", None, "FREEZE", api_key)
                return 4
            if disposition == "STOP":
                reason = "thinkingConfig" if _thinking_rejected(status, payload) else f"http {status}"
                _write_read(read_path, wid, order, "UNMEASURED", None, reason, api_key)
                return 5
            if disposition == "PARSED":
                final_disposition = "PARSED"
                break
            if not retry:
                final_disposition = "UNMEASURED"
                parsed = None
                break
        _write_read(read_path, wid, order, final_disposition, parsed, final_reason, api_key)
    return 0


def _dump(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=2)


def _write_read(path: Path, wid: str, order: int, status: str, parsed: dict | None, reason: str | None, secret: str) -> None:
    record = {
        "window_id": wid,
        "n": order,
        "status": status,
        "reason": reason,
        "read": parsed,
        "jev": False,
    }
    _write_once(path, scrub(_dump(record) + "\n", secret))


def _charge(body: dict, worst: Decimal) -> tuple[Decimal, str, str]:
    usage = body.get("usageMetadata") if isinstance(body, dict) else None
    cost, mark = billed_usd(usage)
    if cost is None:
        return worst, "UNMEASURED", "UNMEASURED: missing usageMetadata; charged at the pre-call worst case"
    return cost, "ESTIMATED", "usageMetadata; thinking billed as output"


def _count_tokens(transport, api_key: str, contents, call: str, log, emit) -> int:
    url = endpoint("countTokens")
    status, payload, body = _post(transport, api_key, url, {"contents": contents})
    record = {
        **_base_record(call, call.rsplit("-", 1)[-1]),
        "event_code": "PWEC-COUNT",
        "http_status": status,
        "cost_usd": "0.000000",
        "cost_basis": "countTokens is not added to the cap; the worst case uses its count",
        "counted_input_tokens": None,
    }
    if status == 429:
        record["event_code"] = "PWEC-FREEZE"
        record["disposition"] = "FREEZE"
        log(record)
        emit(f"{call} count FREEZE")
        raise StopRun("HTTP 429 at countTokens; FREEZE", 4)
    if status >= 500 or status == 400 or status != 200:
        record["event_code"] = "PWEC-STOP"
        record["disposition"] = "STOP"
        if _thinking_rejected(status, payload):
            record["thinking_config_kept"] = True
        log(record)
        emit(f"{call} count STOP http={status}")
        raise StopRun("countTokens failed; STOP", 5)
    counted = body.get("totalTokens") if isinstance(body, dict) else None
    if type(counted) is not int or counted < 0:
        record["event_code"] = "PWEC-STOP"
        record["disposition"] = "STOP"
        log(record)
        raise StopRun("countTokens returned no totalTokens; STOP", 5)
    record["counted_input_tokens"] = counted
    record["disposition"] = "COUNTED"
    log(record)
    return counted


def _generate(transport, api_key: str, contents, call: str):
    url = endpoint("generateContent")
    payload_obj = {"contents": contents, "generationConfig": generation_config()}
    return _post(transport, api_key, url, payload_obj)


def _post(transport, api_key: str, url: str, payload_obj: dict) -> tuple[int, str, dict]:
    if "AIza" in url or "key=" in url.lower():
        raise StopRun("refusing a request URL that carries key material", 5)
    body = json.dumps(payload_obj).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    try:
        status, raw = transport(url, headers, body, TIMEOUT_S)
    except StopRun:
        raise
    except Exception as exc:
        raise StopRun("transport error", 5) from exc
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return int(status), text, parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PWEC-2 Carbon reads. Needs GEMINI_API_KEY. jev is false.")
    parser.add_argument("--data", required=True, help="Directory with MANIFEST.json and packaged frames")
    parser.add_argument("--out", required=True, help="Directory for the ledger, raw responses, and reads")
    parser.add_argument("--expect-sha", action="append", default=[], help="name=sha256, repeatable. Checked before any call.")
    args = parser.parse_args(argv)
    key = os.environ.get("GEMINI_API_KEY", "")
    try:
        return run_reads(
            Path(args.data),
            Path(args.out),
            transport=urllib_transport,
            api_key=key,
            expect_sha=args.expect_sha,
        )
    except StopRun as exc:
        print(scrub(str(exc), key), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
