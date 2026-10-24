"""Manifest of the scorebug reader code and template packs actually loaded.

Posted to QorCap only when ``QORCAP_FRAME_BIND`` is on. QorCap writes ``readers.json`` and seals
its hash into checkpoint 0. This module does not put anything on the receipt.

Paths stored in the manifest are relative (``sidecar/...`` or ``qor_receipt/...``). Key files are
refused. File contents are hashed and never logged.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "qorcap-readers/v0"
_ON = {"1", "true", "yes", "on"}
_LOCAL_MODULES = (
    "qorcap_standard",
    "qorcap_reader",
    "qorcap_endmenu",
    "qorcap_mnp",
    "qorcap_snp",
    "qorcap_tnp",
    "qorcap_playcall",
)
_QORTIME_MODULES = (
    "qor_receipt._vendor.qoresence.vision.local_scorebug.reader",
    "qor_receipt._vendor.qoresence.vision.local_scorebug.agreement",
    "qor_receipt.final_detect",
    "qor_receipt.agent.pause_detect",
)
_KEY_SUFFIXES = (".key", ".pem", ".p12", ".pfx")


def frame_bind_enabled(raw: str | None = None) -> bool:
    if raw is None:
        raw = os.environ.get("QORCAP_FRAME_BIND", "")
    return raw.strip().lower() in _ON


@dataclass(frozen=True)
class LoadedFile:
    reader_id: str
    version: str
    kind: str  # "code" or "pack"
    path: Path
    rel: str


def safe_rel_path(path: str) -> bool:
    if not path or len(path) > 240 or "\0" in path:
        return False
    if path.startswith(("/", "\\")) or ":" in path:
        return False
    lower = path.lower()
    if lower.endswith(_KEY_SUFFIXES):
        return False
    for part in path.replace("\\", "/").split("/"):
        if part in ("", ".", "..", "keys") or part.lower() == "keys":
            return False
    return True


def _rel(root: Path, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        parts = path.resolve().parts
        if "qor_receipt" not in parts:
            return None
        rel = Path(*parts[parts.index("qor_receipt"):]).as_posix()
    if not safe_rel_path(rel):
        return None
    return rel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _version(mod, fallback: str) -> str:
    for attr in ("VERSION", "__version__", "PACK_VERSION"):
        val = getattr(mod, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()[:80]
    return fallback


def _packs_on(mod) -> list[Path]:
    out = []
    for name in dir(mod):
        if name.startswith("_") and name != "PACK" and not name.startswith("PACK"):
            continue
        val = getattr(mod, name, None)
        if not isinstance(val, Path):
            continue
        if val.suffix.lower() != ".npz" or not val.is_file():
            continue
        out.append(val)
    return out


def build_manifest(files: list[LoadedFile], *, qorcap_version: str) -> dict:
    """Group loaded files by reader id. Unsafe paths are dropped, not hashed."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in files:
        if item.kind not in ("code", "pack"):
            continue
        if not safe_rel_path(item.rel) or not item.path.is_file():
            continue
        row = {"kind": item.kind, "path": item.rel, "sha256": _sha256(item.path)}
        grouped.setdefault((item.reader_id, item.version), []).append(row)
    readers = []
    for (reader_id, version), rows in grouped.items():
        rows.sort(key=lambda r: (r["kind"], r["path"]))
        readers.append({"id": reader_id, "version": version, "files": rows})
    readers.sort(key=lambda r: r["id"])
    if not readers:
        raise ValueError("no reader code or packs were loaded")
    return {"schema": SCHEMA, "qorcap_version": qorcap_version, "readers": readers}


def collect_from_process(root: Path | None = None, *, qorcap_version: str = "0") -> list[LoadedFile]:
    """Modules and pack files already imported in this process. Does not import anything."""
    root = root or Path(__file__).resolve().parent.parent
    out: list[LoadedFile] = []
    seen: set[str] = set()

    def add(reader_id: str, version: str, kind: str, path: Path) -> None:
        rel = _rel(root, path)
        if rel is None or rel in seen:
            return
        seen.add(rel)
        out.append(LoadedFile(reader_id, version, kind, path, rel))

    for name in _LOCAL_MODULES + _QORTIME_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        file = getattr(mod, "__file__", None)
        if not file:
            continue
        reader_id = "qortime" if name.startswith("qor_receipt") else name.replace("qorcap_", "")
        version = _version(mod, qorcap_version if reader_id != "qortime" else "unversioned")
        add(reader_id, version, "code", Path(file))
        for pack in _packs_on(mod):
            add(reader_id, version, "pack", pack)
    return out


def publish(api, *, root: Path | None = None, qorcap_version: str | None = None) -> dict:
    """POST the manifest. QorCap canonicalizes it and seals the file hash; we do not write the file."""
    if qorcap_version is None:
        try:
            from qorcap_submit import CLIENT_VERSION
            qorcap_version = CLIENT_VERSION
        except Exception:
            qorcap_version = "0"
    manifest = build_manifest(collect_from_process(root, qorcap_version=qorcap_version), qorcap_version=qorcap_version)
    api.post("/events", {"type": "readers", "manifest": manifest}, timeout=5)
    return manifest
