"""CI source check for glyph templates.

Fails when a template file has no provenance row, a sha256 does not match, a
source match is held out or not allowlisted, or a held-out id is stored in
template-pack metadata. ``held_out_sha256.json`` is a blocklist of public
clips, not a template pack, so it is not scanned for those ids.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))

from qorcap_template_guard import sha256_file  # noqa: E402

PROV = ROOT / "templates" / "provenance.json"
SOURCES = ROOT / "templates" / "template_sources.json"
HELDOUT = ROOT / "templates" / "heldout.json"
ALLOW = {"102332", "111640"}
HELD = {"170123", "021003", "125430", "084523", "193534"}
EXTS = {".npz", ".npy", ".png"}
SKIP_JSON = {"held_out_sha256.json"}


def _ids(path: Path) -> set[str]:
    return {str(x) for x in json.loads(path.read_text(encoding="utf-8"))["match_ids"]}


def _template_files() -> list[Path]:
    found = []
    for folder in (ROOT / "sidecar" / "packs", ROOT / "templates"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in EXTS:
                found.append(path)
    return sorted(found)


def _entry_path(entry: dict) -> Path | None:
    rel = str(entry.get("path") or "")
    if not rel or ".." in Path(rel).parts:
        return None
    for cand in (ROOT / rel, PROV.parent / rel):
        if cand.is_file():
            return cand
    return None


def _pack_strings() -> list[str]:
    texts = []
    packs = ROOT / "sidecar" / "packs"
    for path in sorted(packs.glob("*.npz")):
        with np.load(path, allow_pickle=False) as blob:
            for key in blob.files:
                arr = blob[key]
                if getattr(arr.dtype, "kind", "") in {"U", "S", "O"}:
                    texts.extend(str(x) for x in arr.ravel())
    for path in sorted(packs.glob("*.json")):
        if path.name in SKIP_JSON:
            continue
        texts.append(path.read_text(encoding="utf-8"))
    data = json.loads(PROV.read_text(encoding="utf-8"))
    for entry in data.get("templates") or []:
        if isinstance(entry, dict):
            for key, value in entry.items():
                if key == "sha256":
                    continue
                texts.append(str(value))
    return texts


def test_allowlist_and_heldout_files_are_exact():
    assert _ids(SOURCES) == ALLOW
    assert _ids(HELDOUT) == HELD


def test_every_template_file_is_allowlisted_and_hashed():
    data = json.loads(PROV.read_text(encoding="utf-8"))
    assert data["schema"] == "qorcap-template-provenance/v0"
    entries = data["templates"]
    assert isinstance(entries, list)
    by_file: dict[Path, dict] = {}
    for entry in entries:
        found = _entry_path(entry)
        assert found is not None, entry
        assert sha256_file(found) == entry["sha256"]
        mid = str(entry["source_match_id"])
        assert mid in ALLOW, mid
        assert mid not in HELD, mid
        by_file[found.resolve()] = entry
    for path in _template_files():
        assert path.resolve() in by_file, f"no provenance entry for {path.relative_to(ROOT)}"


def test_held_out_ids_are_not_in_template_pack_metadata():
    blob = "\n".join(_pack_strings())
    for item in sorted(HELD):
        assert item not in blob, item
