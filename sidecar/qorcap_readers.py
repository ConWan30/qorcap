"""Manifest of the scorebug reader code and template packs actually loaded.

Built before ``qorcap record`` starts and passed as ``--readers-manifest``. QorCap writes
``readers.json`` and seals its hash into checkpoint 0. There is no wait and no later post.
This module does not put anything on the receipt.

Paths stored in the manifest are relative (``sidecar/...`` or ``qor_receipt/...``). Key files are
refused. File contents are hashed as raw bytes and never logged. ``*.py`` is pinned to LF in
``.gitattributes`` so a Windows checkout hashes the same bytes as the repo.
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
    "qorcap_psn",
    "qorcap_match",
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


def vendored_scorebug_packs(mod) -> list[Path]:
    """The npz ``load_scorebug_pack`` would open: the last ``madden27_standard_v*.npz``."""
    file = getattr(mod, "__file__", None)
    if not file:
        return []
    pack_dir = Path(file).resolve().parent / "packs"
    found = sorted(pack_dir.glob("madden27_standard_v*.npz"))
    return [found[-1]] if found else []


def build_manifest(files: list[LoadedFile], *, qorcap_version: str, python: str = "0",
                   numpy: str | None = None, opencv: dict | None = None,
                   ffmpeg: dict | None = None, glimpse: dict | None = None) -> dict:
    """Group loaded files by reader id. Unsafe paths are dropped, not hashed.

    The object is closed: QorCap rejects any key that is not in this shape, including a forged
    ``reader_verified_by_referee``. Hashes are of the raw file bytes.
    """
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
    return {
        "schema": SCHEMA,
        "qorcap_version": qorcap_version,
        "python": python,
        "numpy": numpy,
        "opencv": opencv,
        "ffmpeg": ffmpeg or {"state": "unbound"},
        "glimpse": glimpse or {"state": "unbound"},
        "readers": readers,
    }


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
        if name.endswith("local_scorebug.reader") or name.endswith(".reader"):
            for pack in vendored_scorebug_packs(mod):
                add(reader_id, version, "pack", pack)
    return out


def _runtime_versions() -> tuple[str, str | None, dict | None]:
    import platform
    py = platform.python_version()
    numpy_v = None
    opencv = None
    try:
        import numpy
        numpy_v = str(numpy.__version__)
    except Exception:
        numpy_v = None
    try:
        import cv2
        info = cv2.getBuildInformation()
        opencv = {
            "version": str(cv2.__version__),
            "build_sha256": hashlib.sha256(info.encode("utf-8", "replace")).hexdigest(),
        }
    except Exception:
        opencv = None
    return py, numpy_v, opencv


def ffmpeg_binding(ffmpeg: str | None = None) -> dict:
    """Version line plus the raw SHA-256 of the ffmpeg binary. Unbound if either is missing."""
    import shutil
    import subprocess
    bin_path = ffmpeg or shutil.which("ffmpeg")
    if not bin_path:
        return {"state": "unbound"}
    path = Path(bin_path)
    if not path.is_file():
        return {"state": "unbound"}
    try:
        proc = subprocess.run([str(path), "-version"], capture_output=True, text=True, timeout=5)
    except Exception:
        return {"state": "unbound"}
    line = (proc.stdout or "").splitlines()
    if proc.returncode != 0 or not line or not line[0].strip():
        return {"state": "unbound"}
    try:
        sha = _sha256(path)
    except Exception:
        return {"state": "unbound"}
    return {"version": line[0].strip()[:240], "sha256": sha}


def glimpse_binding() -> dict:
    """Remote Glimpse identity known before the match. ``veto_ran`` is false: the manifest is sealed
    before any frame is read, so this recording cannot claim the veto already ran.

    If the watcher module cannot be imported, the section is ``{"state": "unbound"}``.
    """
    try:
        import qorcap_glimpse_watch as watch
    except Exception:
        return {"state": "unbound"}
    settings = {
        "model": watch.MODEL,
        "version": watch.VERSION,
        "role_version": watch.ROLE_VERSION,
        "max_tokens": watch.CROP_MAX_TOKENS,
        "temperature": 0,
        "veto_enabled": os.environ.get("QORCAP_GLIMPSE_TD_VETO", "").strip() == "1",
    }
    h = hashlib.sha256()
    prompts = Path(watch.PROMPTS)
    if prompts.is_dir():
        for path in sorted(p for p in prompts.iterdir() if p.is_file()):
            h.update(path.name.encode("utf-8"))
            h.update(path.read_bytes())
    h.update(json_compact(settings).encode("utf-8"))
    return {
        "model": str(watch.MODEL),
        "version": str(watch.VERSION),
        "prompt_sha256": h.hexdigest(),
        "veto_ran": False,
    }


def json_compact(obj: dict) -> str:
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _import_live_stack() -> None:
    """Import the readers Start Match constructs, so their files are already in sys.modules."""
    import qorcap_endmenu  # noqa: F401
    import qorcap_mnp  # noqa: F401
    import qorcap_playcall  # noqa: F401
    import qorcap_reader  # noqa: F401
    import qorcap_standard  # noqa: F401
    try:
        import qorcap_psn  # noqa: F401
    except Exception:
        pass
    try:
        from qor_receipt._vendor.qoresence.vision.local_scorebug import reader as _reader  # noqa: F401
        from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow  # noqa: F401
        from qor_receipt.agent.pause_detect import PauseDetector  # noqa: F401
        from qor_receipt.final_detect import detect_final  # noqa: F401
    except Exception:
        pass


def write_prerecord_manifest(path: Path, *, ffmpeg: str | None = None, root: Path | None = None) -> dict:
    """Write the manifest Start Match passes to ``--readers-manifest``. Does not start qorcap."""
    root = root or Path(__file__).resolve().parent.parent
    try:
        from qorcap_submit import CLIENT_VERSION
        qorcap_version = CLIENT_VERSION
    except Exception:
        qorcap_version = "0"
    _import_live_stack()
    py, numpy_v, opencv = _runtime_versions()
    files = collect_from_process(root, qorcap_version=qorcap_version)
    match = root / "sidecar" / "qorcap_match.py"
    if match.is_file():
        rel = _rel(root, match)
        if rel and rel not in {item.rel for item in files}:
            files.append(LoadedFile("match", qorcap_version, "code", match, rel))
    manifest = build_manifest(
        files,
        qorcap_version=qorcap_version,
        python=py,
        numpy=numpy_v,
        opencv=opencv,
        ffmpeg=ffmpeg_binding(ffmpeg),
        glimpse=glimpse_binding(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_compact(manifest), encoding="utf-8")
    return manifest
