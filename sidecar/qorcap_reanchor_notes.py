"""Notes for a grammar or clock re-anchor.

``contracts/score_timeline.v0.json`` events are ``{t_ms, home, away, quarter}`` and
``additionalProperties`` is false. The chain hashes only those four fields plus ``prev``.
There is no place there for ``grammar_reanchor`` or ``clock_reanchor``.

This file is the sidecar. One JSON object per line in ``reanchor_notes.jsonl``, beside
the match bundle. It is not a field of ``receipt.v0`` and it is not a checkpoint-hash
input. The engine's score event keeps its existing keys, so ``frames_hash`` does not move.
"""
from __future__ import annotations

import json
from pathlib import Path

FILENAME = "reanchor_notes.jsonl"


def append_note(bundle: Path, row: dict) -> None:
    """Append one note. ``bundle`` is the match directory."""
    path = Path(bundle) / FILENAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def record_unresolved_final(bundle: Path | None, result: dict | None) -> None:
    """One final line when the published result abstained because the grammar was unresolved."""
    if bundle is None or not isinstance(result, dict):
        return
    if result.get("method") == "end_menu_read":
        return
    notes = result.get("notes") or []
    if not any(isinstance(n, str) and "score grammar disagreed" in n for n in notes):
        return
    append_note(bundle, {"kind": "final", "grammar_unresolved": True})
