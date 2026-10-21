"""Paths and private files shared by the recorder, the submit client, and Glimpse.

A name read from a manifest, a zip, or a highlights file is untrusted. ``Path.join``
replaces the directory when the other side is absolute, and on Windows a ``C:name``
prefix does the same. These helpers refuse that instead of opening the file.
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

WIN = sys.platform == "win32"


def data_dir() -> Path:
    """QorCap's private directory: ``$QORCAP_HOME``, else ``%LOCALAPPDATA%\\QorCap``, else ``~/.qorcap``."""
    base = os.getenv("QORCAP_HOME")
    if base:
        return Path(base)
    if WIN:
        return Path(os.getenv("LOCALAPPDATA") or Path.home()) / "QorCap"
    return Path.home() / ".qorcap"


def key_dir() -> Path:
    return data_dir() / "keys"


def restrict_private(p: Path) -> None:
    """Owner-only. POSIX mode 0600. Windows: drop inherited ACEs and grant the current user."""
    try:
        if WIN:
            user = os.getenv("USERNAME", "")
            if user and '"' not in user and "\n" not in user and "\r" not in user:
                subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.chmod(p, 0o600)
            if p.is_dir():
                os.chmod(p, 0o700)
    except OSError:
        pass


def private_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    if not WIN:
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def bundle_child(root: Path, name: object) -> Path | None:
    """``root / name`` when ``name`` is one file in that directory. None when it could escape."""
    if not isinstance(name, str) or name == "" or name in (".", ".."):
        return None
    if any(c in name for c in ("/", "\\", ":", "\x00")):
        return None
    root = Path(root)
    child = root / name
    if child.parent != root:
        return None
    try:
        root_r = root.resolve()
        child_r = child.resolve()
    except OSError:
        return None
    if child_r != root_r and root_r not in child_r.parents:
        return None
    # A symlink may resolve outside the directory. That is still an escape.
    if child_r.parent != root_r:
        return None
    return child


def safe_extract_zip(data: bytes, dest: Path) -> None:
    """Write zip members under ``dest``. Refuse ``..``, absolute names, drive prefixes, and symlinks.

    Members are written by this function. ``ZipFile.extractall`` is not used: it joins the
    archive name onto ``dest`` and follows a link that is already there.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            # Unix symlink: the member itself must not be a link.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"unsafe path in zip: {info.filename}")
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ":" in name or "\x00" in name:
                raise ValueError(f"unsafe path in zip: {info.filename}")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or any(p == ".." for p in parts):
                raise ValueError(f"unsafe path in zip: {info.filename}")
            cur = root
            for part in parts:
                if cur.is_symlink():
                    raise ValueError(f"unsafe path in zip: {info.filename}")
                nxt = cur / part
                if nxt.is_symlink():
                    raise ValueError(f"unsafe path in zip: {info.filename}")
                cur = nxt
            if cur != root and root not in cur.resolve().parents:
                raise ValueError(f"unsafe path in zip: {info.filename}")
            if name.endswith("/"):
                cur.mkdir(parents=True, exist_ok=True)
                continue
            cur.parent.mkdir(parents=True, exist_ok=True)
            with cur.open("wb") as out:
                out.write(zf.read(info.filename))


def mode_owner_only(p: Path) -> bool:
    if WIN:
        return True
    return stat.S_IMODE(p.stat().st_mode) & 0o077 == 0
