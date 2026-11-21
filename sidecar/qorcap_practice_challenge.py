"""Local CoD practice-lane response-bound prompt (advisory only).

Practice lane + Call of Duty only. Official CoD challenges stay closed (`challenges=False` on
OfficialLive). Posts a local nonce to QorCap's own `/challenge` endpoint (`practice=True`) so the
controller / hand-timeline response bound can be checked after stop without calling the official
referee or touching receipt status.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

_BUTTONS = ("CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT")
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})
DEFAULT_DELAY_S = 15.0
DEFAULT_WINDOW_MS = 90_000


def enabled(game: str | None, environ: Mapping[str, str] | None = None) -> bool:
    """True only in the practice lane for Call of Duty, unless opted out."""
    env = os.environ if environ is None else environ
    if (game or "").strip().lower() != "cod":
        return False
    if (env.get("QORCAP_LANE") or "").strip().lower() != "practice":
        return False
    flag = (env.get("QORCAP_PRACTICE_CHALLENGE") or "").strip().lower()
    return flag not in _FALSE_WORDS


def pattern_for(nonce_hex: str) -> list[str]:
    """Port of `evidence::pattern_for` (OPTIONS + 4 face/d-pad buttons)."""
    if len(nonce_hex) != 32 or any(c not in "0123456789abcdef" for c in nonce_hex):
        raise ValueError("nonce must be 32 lowercase hex chars (16 bytes)")
    digest = hashlib.sha256(bytes.fromhex(nonce_hex)).digest()
    out: list[str] = []
    i = 0
    while len(out) < 4:
        b = _BUTTONS[digest[i % 32] % 8]
        i += 1
        if out and out[-1] == b:
            b = _BUTTONS[(_BUTTONS.index(b) + 1) % 8]
        out.append(b)
    return ["OPTIONS", *out]


def build_practice_challenge(
    *,
    role: str = "home",
    issued_ms: int | None = None,
    window_ms: int = DEFAULT_WINDOW_MS,
    nonce: str | None = None,
) -> dict:
    """Build a local CoD practice challenge payload for `POST /challenge`."""
    tok = nonce or secrets.token_hex(16)
    pat = pattern_for(tok)
    start_ms = int(time.time() * 1000) if issued_ms is None else int(issued_ms)
    end_ms = start_ms + int(window_ms)
    return {
        "id": tok[:8],
        "role": role,
        "phase": "practice",
        "practice": True,
        "game": "cod",
        "nonce": tok,
        "pattern": pat,
        "issued_ms": start_ms,
        "deadline_ms": end_ms,
    }


def prompt_lines(payload: dict) -> list[str]:
    pat = " -> ".join(payload.get("pattern") or [])
    secs = max(1, (int(payload["deadline_ms"]) - int(payload["issued_ms"])) // 1000)
    return [
        f"PRACTICE RESPONSE BOUND ({payload['id']}): press {pat} within {secs}s.",
        "Advisory only (practice lane); never changes receipt status and does not identify who held the controller.",
    ]


class PracticeChallengePrompter:
    """Post one local CoD practice challenge to QorCap and show it on the console."""

    def __init__(
        self,
        api,
        *,
        role: str = "home",
        console=None,
        say: Callable[[str], None] = print,
        delay_s: float | None = None,
        window_ms: int = DEFAULT_WINDOW_MS,
    ) -> None:
        self.api = api
        self.role = role
        self.console = console
        self.say = say
        self.window_ms = int(window_ms)
        if delay_s is None:
            raw = os.environ.get("QORCAP_PRACTICE_CHALLENGE_DELAY_S", "")
            try:
                self.delay_s = max(0.0, float(raw)) if raw.strip() else DEFAULT_DELAY_S
            except ValueError:
                self.delay_s = DEFAULT_DELAY_S
        else:
            self.delay_s = max(0.0, float(delay_s))
        self.posted: dict | None = None
        self._thread: threading.Thread | None = None

    def trigger(self, *, nonce: str | None = None, issued_ms: int | None = None) -> dict | None:
        payload = build_practice_challenge(
            role=self.role,
            issued_ms=issued_ms,
            window_ms=self.window_ms,
            nonce=nonce,
        )
        try:
            resp = self.api.post("/challenge", payload, timeout=2.0)
        except Exception as exc:
            self.say(f"(practice response bound unavailable: {exc})")
            return None
        if not isinstance(resp, dict) or not resp.get("ok"):
            return None
        self.posted = resp.get("challenge") if isinstance(resp.get("challenge"), dict) else payload
        shown = False
        if self.console is not None:
            try:
                shown = bool(
                    self.console.show_challenge(
                        phase="practice",
                        label="cod practice",
                        pattern=list(payload["pattern"]),
                        issued_ms=int(payload["issued_ms"]),
                        deadline_ms=int(payload["deadline_ms"]),
                        practice=True,
                    )
                )
            except Exception:
                shown = False
        if not shown:
            for line in prompt_lines(payload):
                self.say(line)
        return resp

    def start(self, stop: threading.Event) -> threading.Thread:
        def _run() -> None:
            if stop.wait(self.delay_s):
                return
            try:
                st = self.api.get("/status", timeout=1.0)
                if isinstance(st, dict) and st.get("state") in ("stopping", "stopped", "finishing"):
                    return
            except Exception:
                pass
            if not stop.is_set():
                self.trigger()

        t = threading.Thread(target=_run, name="qorcap-practice-challenge", daemon=True)
        t.start()
        self._thread = t
        return t


def read_hand_challenge_summary(bundle: Path) -> list[str]:
    """Read `hand_challenge.jsonl` advisory lines from a finished practice bundle."""
    path = Path(bundle) / "hand_challenge.jsonl"
    if not path.is_file():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        lines.append(
            f"  Response bound [{rec.get('phase', 'practice')} {rec.get('id', '?')}]: "
            f"{rec.get('status', 'UNKNOWN')} {rec.get('outcome', 'UNKNOWN')} "
            f"(bound_head={rec.get('bound_head_status', 'UNKNOWN')}, stream={rec.get('stream_source', 'none')}) "
            f"— {rec.get('statement', '')}"
        )
    return lines
