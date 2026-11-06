"""Game set for Start Match: Madden or Call of Duty.

The set is chosen before recording, the same way the side is chosen. A flag wins,
then a one-shot ``next-game.txt``, then the prompt. A direct CLI run with no
``--game`` stays on Madden, so older tests keep the scorebug path.

Call of Duty is a different set, not a Madden scorebug with the labels swapped.
The differentiator is multi-signal match verification for skill evidence
(controller + video + correlated game HUD), using real match recordings as the
calibration source. The product line is in ``docs/GAME_SETS.md``.

The file written beside the bundle (``game_set.json``) is not a receipt field.
``match.game`` and ``match.scorebug_profile`` are the receipt names.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

GAME_FILE_NAME = "next-game.txt"
GAME_FILE_MAX_AGE_S = 4 * 3600
GAME_SET_FILE = "game_set.json"

# Receipt note when a Call of Duty match ends without an end-screen read.
COD_END_NOTE = (
    "Call of Duty end screen was not read, so the score is not Counted. "
    "Stopping the recorder does not read the end screen."
)
CLOSED_NOTE = "Game set file could not be read. No score was Counted."


@dataclass(frozen=True)
class GameSet:
    """One product set. ``game`` and ``profile`` are the receipt strings."""

    set_id: str
    label: str
    game: str
    profile: str
    loads: tuple[str, ...]
    end: str
    readers: str


MADDEN = GameSet(
    set_id="madden",
    label="Madden",
    game="madden27",
    profile="madden27_standard",
    loads=("home_away", "scorebug", "end_menu"),
    end="end_menu",
    readers="madden_scorebug",
)
COD = GameSet(
    set_id="cod",
    label="Call of Duty",
    game="cod_mw4",
    profile="cod_mw4_hud",
    loads=("crosshair", "hitmarker", "kill_popup", "killfeed", "pause", "recoil", "scoreboard", "end_screen"),
    end="end_screen",
    readers="cod_match_signals",
)
# A game_set.json that is present but unreadable. Do not run the Madden scorebug.
CLOSED = GameSet(
    set_id="closed",
    label="unreadable game set",
    game="",
    profile="",
    loads=(),
    end="end_screen",
    readers="fail_closed",
)

_FILE_ALIASES = {
    "madden": "madden",
    "madden27": "madden",
    "cod": "cod",
    "mw4": "cod",
    "call_of_duty": "cod",
    "callofduty": "cod",
    "call of duty": "cod",
}


def set_for(set_id: str | None) -> GameSet:
    if set_id == "cod":
        return COD
    if set_id == "closed":
        return CLOSED
    return MADDEN


def uses_madden_reader(spec: GameSet) -> bool:
    return spec.set_id == "madden"


def canonical_file_game(raw: str) -> str | None:
    key = " ".join(raw.strip().lower().split())
    if not key:
        return None
    if key in _FILE_ALIASES:
        return _FILE_ALIASES[key]
    return _FILE_ALIASES.get(key.replace(" ", "_"))


def take_game_file(path: Path, *, now: float | None = None) -> tuple[str | None, str]:
    """Read and consume a one-shot game file. Renamed, never deleted.

    Returns ``(madden|cod or None, note)``. A stale or unknown file is renamed
    and the prompt is used instead.
    """
    if not path.exists():
        return None, ""
    now = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8-sig").strip().lower()
        age = now - path.stat().st_mtime
    except OSError as e:
        return None, f"game file {path} unreadable ({e}); asking instead"
    game = canonical_file_game(raw)
    ok = game in ("madden", "cod") and age <= GAME_FILE_MAX_AGE_S
    tag = "used" if ok else "ignored"
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
    dest = path.with_name(f"next-game.{tag}-{stamp}.txt")
    while dest.exists():
        n += 1
        dest = path.with_name(f"next-game.{tag}-{stamp}-{n}.txt")
    try:
        path.replace(dest)
    except OSError:
        pass
    if not ok:
        why = "it is not madden or cod" if game not in ("madden", "cod") else f"it is {int(age // 60)} min old"
        return None, f"game file ignored ({why}); asking instead"
    return game, f"game from {path.name}: {set_for(game).label}"


def write_game_set(path: Path, spec: GameSet, source: str | None) -> None:
    """Sidecar record of the set that was loaded. Not a receipt field."""
    doc = {
        "schema": "qorcap-game-set/v0",
        "set": spec.set_id,
        "source": source,
        "label": spec.label,
        "game": spec.game,
        "scorebug_profile": spec.profile,
        "loads": list(spec.loads),
        "end": spec.end,
        "readers": spec.readers,
        "chosen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def recorded_set(bundle: Path) -> GameSet:
    """The set a bundle was started with.

    No ``game_set.json`` means an older Madden bundle, unless the bundle already
    names ``cod_mw4``. A file that cannot be read does not select the Madden scorebug.
    """
    path = bundle / GAME_SET_FILE
    if not path.is_file():
        if _peek_game(bundle) == COD.game:
            return COD
        return MADDEN
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CLOSED
    if not isinstance(data, dict):
        return CLOSED
    sid = data.get("set")
    if sid == "cod":
        return COD
    if sid == "madden":
        return MADDEN
    return CLOSED


def _peek_game(bundle: Path) -> str | None:
    """Game id already written on the bundle, when game_set.json is missing."""
    bundle_path = bundle / "bundle.json"
    if bundle_path.is_file():
        try:
            data = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("game"), str):
            return data["game"]
    draft_path = bundle / "receipt.draft.json"
    if draft_path.is_file():
        try:
            data = json.loads(draft_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        match = data.get("match") if isinstance(data, dict) else None
        if isinstance(match, dict) and isinstance(match.get("game"), str):
            return match["game"]
    return None


def stamp_draft(draft: dict, spec: GameSet) -> dict:
    """Put the set on the draft receipt. Does not confirm a score."""
    if spec.set_id == "closed":
        result = dict(draft.get("result") or {})
        notes = [n for n in (result.get("notes") or []) if isinstance(n, str)]
        if CLOSED_NOTE not in notes:
            notes.append(CLOSED_NOTE)
        result["notes"] = notes
        result["final_confirmed"] = False
        result["method"] = "last_scorebug_read_unconfirmed"
        result.pop("final_basis", None)
        return dict(draft, result=result)
    match = dict(draft.get("match") or {})
    match["game"] = spec.game
    if spec.set_id == "cod":
        match["scorebug_profile"] = spec.profile
        result = dict(draft.get("result") or {})
        if result.get("method") != "end_menu_read":
            notes = [n for n in (result.get("notes") or []) if isinstance(n, str)]
            if COD_END_NOTE not in notes:
                notes.append(COD_END_NOTE)
            result["notes"] = notes
            result["final_confirmed"] = False
            result["method"] = "last_scorebug_read_unconfirmed"
            result.pop("final_basis", None)
            return dict(draft, match=match, result=result)
        return dict(draft, match=match, result=result)
    profile = str(match.get("scorebug_profile") or "")
    if not profile.startswith("madden27_"):
        match["scorebug_profile"] = spec.profile
    return dict(draft, match=match)
