"""Allowlisted glyph-template provenance.

This is a QorCap-internal record. It is not a ``receipt.v0`` field and it is not
part of the vendored QorTime bundle contract.

A reader may score a glyph only from a file listed in ``templates/provenance.json``
whose ``source_match_id`` is in ``templates/template_sources.json`` and is not in
``templates/heldout.json``, and whose bytes match the recorded sha256. Anything
else is missing. The reader abstains and does not guess a score.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TEMPLATES_MISSING = "templates_missing"
TEMPLATES_MISSING_REASON = (
    "templates_missing: no allowlisted scorebug templates are loaded; abstaining; no score read"
)

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "templates" / "provenance.json"
SOURCES_PATH = ROOT / "templates" / "template_sources.json"
HELDOUT_PATH = ROOT / "templates" / "heldout.json"

_LOGGED = False


def log_templates_missing() -> None:
    """One stderr line per process. The line names the abstention and no score."""
    global _LOGGED
    if _LOGGED:
        return
    _LOGGED = True
    print(TEMPLATES_MISSING_REASON, file=sys.stderr)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def match_ids(path: Path) -> set[str]:
    data = _load_json(path)
    return {str(x) for x in data.get("match_ids", [])}


def provenance_entries(path: Path | None = None) -> list[dict]:
    path = path or PROVENANCE_PATH
    if not path.is_file():
        return []
    data = _load_json(path)
    rows = data.get("templates")
    return list(rows) if isinstance(rows, list) else []


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_template(entry: dict, provenance_path: Path | None = None) -> Path | None:
    """Repo-relative path, or a path next to the provenance file. ``..`` is refused."""
    rel = str(entry.get("path") or "")
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return None
    prov = provenance_path or PROVENANCE_PATH
    for cand in (ROOT / rel, prov.parent / rel):
        if cand.is_file():
            return cand
    return None


def authorized_entries(path: Path | None = None) -> list[tuple[dict, Path]]:
    """Provenance rows whose file exists, hash matches, and match id is allowlisted.

    A held-out id, an id outside the allowlist, a missing file, or a hash mismatch
    drops that row. Callers then see no template and abstain. This does not raise.
    """
    path = path or PROVENANCE_PATH
    try:
        allowed = match_ids(SOURCES_PATH)
        held = match_ids(HELDOUT_PATH)
    except (OSError, ValueError):
        return []
    out: list[tuple[dict, Path]] = []
    for entry in provenance_entries(path):
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("source_match_id") or "")
        if mid not in allowed or mid in held:
            continue
        found = resolve_template(entry, path)
        if found is None:
            continue
        digest = str(entry.get("sha256") or "")
        if len(digest) != 64 or sha256_file(found) != digest:
            continue
        out.append((entry, found))
    return out


def file_authorized(template_path: Path, provenance_path: Path | None = None) -> bool:
    """True when ``template_path`` is one of the authorized provenance files."""
    try:
        target = Path(template_path).resolve()
    except OSError:
        return False
    for _entry, found in authorized_entries(provenance_path):
        try:
            if found.resolve() == target:
                return True
        except OSError:
            continue
    return False


def harvested_vectors(role: str, *, profile: str | None = None, path: Path | None = None) -> dict:
    """``label -> float32 (n, d)`` for authorized ``.npy`` rows of this role.

    ``profile`` None keeps every profile. An entry with no profile counts as ``standard``.
    """
    import numpy as np

    buckets: dict[str, list] = {}
    order: list[str] = []
    for entry, found in authorized_entries(path):
        if str(entry.get("role") or "") != role:
            continue
        if found.suffix.lower() != ".npy":
            continue
        if profile is not None and str(entry.get("profile") or "standard") != profile:
            continue
        try:
            vec = np.load(found)
        except (OSError, ValueError):
            continue
        arr = np.asarray(vec, dtype=np.float32).ravel()
        if arr.size == 0 or not np.isfinite(arr).all():
            continue
        label = str(entry.get("label") or "")
        if not label:
            continue
        if label not in buckets:
            order.append(label)
        buckets.setdefault(label, []).append(arr)
    return {label: np.stack(buckets[label], axis=0) for label in order}


def has_role(role: str, *, profile: str | None = None) -> bool:
    return bool(harvested_vectors(role, profile=profile))
