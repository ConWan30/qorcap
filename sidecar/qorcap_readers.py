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
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "qorcap-readers/v0"
# Same path rule as qortime schema/contracts/qorcap-readers.v0.json at 585ccc4.
PATH_PATTERN = (
    r"^(?!.*(?:^|/)[^/]*(?:\.| )(?:/|$))"
    r"(?!.*\.(?:[Kk][Ee][Yy]|[Pp][Ee][Mm]|[Pp]12|[Pp][Ff][Xx]|[Jj][Ww][Kk]|[Pp][Pp][Kk]|[Jj][Kk][Ss])$)"
    r"(?!.*(?:^|/)[Kk][Ee][Yy][Ss](?:/|$))"
    r"(?!.*(?:^|/)[Ss][Ee][Cc][Rr][Ee][Tt][Ss]?(?:/|$))"
    r"(?!.*(?:^|/)[^/]*_[Kk][Ee][Yy](?:\.[^/]*)?(?:/|$))"
    r"(?!.*(?:^|/)[Ii][Dd]_(?:[Rr][Ss][Aa]|[Dd][Ss][Aa]|[Ee][Cc][Dd][Ss][Aa](?:_[Ss][Kk])?|[Ee][Dd]25519(?:_[Ss][Kk])?|[Ee][Dd]448(?:_[Ss][Kk])?)(?:\.[^/]*)?(?:/|$))"
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.+-]*/)*[A-Za-z0-9_][A-Za-z0-9_.+-]*$"
)
_PATH_RE = re.compile(PATH_PATTERN)
_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_STEMS = frozenset(
    {"con", "nul", "aux", "prn"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
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


def path_identity(path: str) -> str:
    """NFC, then casefold, then strip trailing dots and spaces on each segment."""
    folded = unicodedata.normalize("NFC", path).casefold()
    return "/".join(part.rstrip(". ") for part in folded.split("/"))


def token_identity(value: str) -> str:
    """Reader and runtime ids: NFC, casefold, then strip trailing dots and spaces."""
    return unicodedata.normalize("NFC", value).casefold().rstrip(". ")


def _device_segment(part: str) -> bool:
    return part.split(".", 1)[0].casefold() in _DEVICE_STEMS


def safe_rel_path(path: str) -> bool:
    if not isinstance(path, str) or not path or len(path) > 240 or "\0" in path or not path.isascii():
        return False
    if _PATH_RE.fullmatch(path) is None:
        return False
    return not any(_device_segment(part) for part in path.split("/"))


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


def _runtime_row(row: dict) -> dict:
    if set(row) != {"id", "version", "sha256"}:
        raise ValueError("runtime has unknown keys")
    ident = row["id"]
    version = row["version"]
    sha = row["sha256"]
    if not isinstance(ident, str) or _RUNTIME_ID_RE.fullmatch(ident) is None:
        raise ValueError("runtime id is not an ASCII build name")
    if not isinstance(version, str) or not version.strip() or len(version) > 80:
        raise ValueError("runtime version is empty or too long")
    if not isinstance(sha, str) or _HEX64_RE.fullmatch(sha) is None:
        raise ValueError("runtime sha256 must be 64 lowercase hex chars")
    return {"id": ident, "version": version, "sha256": sha}


def runtimes_from_builds(ffmpeg: dict | None, opencv: dict | None) -> list[dict]:
    """ffmpeg and cv2 are the builds the merged schema names. Unbound builds are omitted."""
    out = []
    if isinstance(ffmpeg, dict) and "sha256" in ffmpeg and "version" in ffmpeg:
        ver = str(ffmpeg["version"]).strip()[:80]
        sha = str(ffmpeg["sha256"])
        if ver and _HEX64_RE.fullmatch(sha):
            out.append({"id": "ffmpeg", "version": ver, "sha256": sha})
    if isinstance(opencv, dict) and opencv.get("version") and opencv.get("build_sha256"):
        ver = str(opencv["version"]).strip()[:80]
        sha = str(opencv["build_sha256"])
        if ver and _HEX64_RE.fullmatch(sha):
            out.append({"id": "cv2", "version": ver, "sha256": sha})
    return out


def build_manifest(files: list[LoadedFile], *, qorcap_version: str,
                   runtimes: list[dict] | None = None, ffmpeg: dict | None = None,
                   opencv: dict | None = None) -> dict:
    """Group loaded files by reader id. Unsafe paths are dropped, not hashed.

    The object is the closed ``qorcap-readers/v0`` shape: schema, qorcap_version, readers,
    runtimes. Reader ids, runtime ids, and paths are unique after NFC, casefolding, and
    trailing-dot/space stripping. A path cannot be both code and pack.
    """
    if not isinstance(qorcap_version, str) or not qorcap_version.strip() or len(qorcap_version) > 80:
        raise ValueError("qorcap_version is empty or too long")
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
        if not isinstance(reader_id, str) or not reader_id.strip() or len(reader_id) > 80:
            raise ValueError("reader id is empty or too long")
        if not isinstance(version, str) or not version.strip() or len(version) > 80:
            raise ValueError("reader version is empty or too long")
        rows.sort(key=lambda r: (r["kind"], r["path"]))
        readers.append({"id": reader_id, "version": version, "files": rows})
    readers.sort(key=lambda r: r["id"])
    if not readers:
        raise ValueError("no reader code or packs were loaded")
    seen_ids: dict[str, str] = {}
    for reader in readers:
        key = token_identity(reader["id"])
        if not key:
            raise ValueError("reader id is empty after normalization")
        if key in seen_ids:
            raise ValueError(f"reader id {reader['id']} collides with {seen_ids[key]}")
        seen_ids[key] = reader["id"]
    seen_paths: dict[str, str] = {}
    for reader in readers:
        for item in reader["files"]:
            ident = path_identity(item["path"])
            if not ident:
                raise ValueError("path is empty after normalization")
            prev = seen_paths.get(ident)
            if prev is not None:
                if prev != item["kind"]:
                    raise ValueError(f"{item['path']} is listed as both code and pack")
                raise ValueError(f"duplicate path {item['path']}")
            seen_paths[ident] = item["kind"]
    if runtimes is None:
        runtimes = runtimes_from_builds(ffmpeg, opencv)
    checked = [_runtime_row(row) for row in runtimes]
    if not checked:
        raise ValueError("no runtime build was bound")
    seen_rt: dict[str, str] = {}
    for row in checked:
        key = token_identity(row["id"])
        if not key:
            raise ValueError("runtime id is empty after normalization")
        if key in seen_rt:
            raise ValueError(f"runtime id {row['id']} collides with {seen_rt[key]}")
        seen_rt[key] = row["id"]
    checked.sort(key=lambda r: r["id"])
    return {
        "schema": SCHEMA,
        "qorcap_version": qorcap_version,
        "readers": readers,
        "runtimes": checked,
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
    _py, _numpy_v, opencv = _runtime_versions()
    files = collect_from_process(root, qorcap_version=qorcap_version)
    match = root / "sidecar" / "qorcap_match.py"
    if match.is_file():
        rel = _rel(root, match)
        if rel and rel not in {item.rel for item in files}:
            files.append(LoadedFile("match", qorcap_version, "code", match, rel))
    manifest = build_manifest(
        files,
        qorcap_version=qorcap_version,
        ffmpeg=ffmpeg_binding(ffmpeg),
        opencv=opencv,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_compact(manifest), encoding="utf-8")
    return manifest
