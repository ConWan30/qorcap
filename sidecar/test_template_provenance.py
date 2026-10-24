"""CI source check for glyph templates.

Fails when a tracked template file has no provenance row, a sha256 does not
match, a source match is held out or not allowlisted, a row fails the JSON
Schema (absolute path, unknown role, extra field), a provenance label does
not match its file name, or a held-out id is stored in template-pack metadata.
``held_out_sha256.json`` is a blocklist of public clips, not a template pack,
so it is not scanned for those ids.

The scan is ``git ls-files`` (tracked paths only). CI checks QorTime out into
an untracked ``qortime/`` inside the workspace; that tree is not a QorCap
template. A tracked path under ``qortime/`` or ``qortime-main/`` fails on its
own. A tracked symlink under ``templates/`` or ``sidecar/`` fails.

``.npz`` and ``.npy`` anywhere tracked under ``sidecar/`` (including
``glimpse``) and image or array files under ``templates/`` need a provenance
row. Fixture images and docs screenshots are not templates.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))
sys.path.insert(0, str(ROOT / "tools"))

from harvest_templates import glyph_name_matches  # noqa: E402
from qorcap_template_guard import sha256_file  # noqa: E402

PROV = ROOT / "templates" / "provenance.json"
SOURCES = ROOT / "templates" / "template_sources.json"
HELDOUT = ROOT / "templates" / "heldout.json"
SCHEMA = json.loads((ROOT / "schema" / "template_provenance.v0.json").read_text(encoding="utf-8"))
ALLOW = {"102332", "111640"}
FOLDERS = {"match-20261024-102332", "match-20261024-111640"}
HELD = {"170123", "021003", "125430", "084523", "193534"}
# Images that are not glyph templates.
SKIP_PREFIXES = (
    ("sidecar", "fixtures"),
    ("docs", "assets"),
    ("docs", "qa"),
)
ARRAY_EXTS = {".npz", ".npy"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
SKIP_JSON = {"held_out_sha256.json"}


def _ids(path: Path) -> set[str]:
    return {str(x) for x in json.loads(path.read_text(encoding="utf-8"))["match_ids"]}


def is_scanned_template(rel: Path) -> bool:
    """True when a tracked repo-relative path must have a provenance row.

    A ``.jpg`` under ``templates/`` and an ``.npz`` or ``.npy`` anywhere
    (including ``sidecar/glimpse`` and a ``qortime/evil.npz`` that someone
    committed) count. Fixture images and docs screenshots do not. The caller
    passes tracked paths only, so an untracked CI checkout is not in the list.
    """
    if not rel.parts:
        return False
    # The 143212 end-menu pack is hash-pinned in qorcap_endmenu, with the 125430
    # rows removed. It is not a per-glyph harvest row. Any other npz still is.
    if rel.as_posix() == "sidecar/packs/madden27_end_menu_v2.npz":
        return False
    suffix = rel.suffix.lower()
    if suffix not in ARRAY_EXTS | IMAGE_EXTS:
        return False
    if any(rel.parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES):
        return False
    if suffix in ARRAY_EXTS:
        return True
    return rel.parts[:1] == ("templates",) or rel.parts[:2] == ("sidecar", "packs")


def tracked_index() -> list[tuple[str, str]]:
    """``(mode, path)`` from ``git ls-files -z -s``. Untracked files are absent."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "-s"],
        cwd=ROOT, check=True, capture_output=True,
    )
    rows = []
    for rec in proc.stdout.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode = meta.split(b" ", 1)[0].decode("ascii")
        rows.append((mode, path.decode("utf-8")))
    return rows


def index_violations(rows: list[tuple[str, str]]) -> list[str]:
    """Tracked qortime trees, and tracked symlinks under templates/ or sidecar/."""
    bad = []
    for mode, path in rows:
        if path.startswith("qortime/") or path.startswith("qortime-main/"):
            bad.append(path)
        if mode == "120000" and (path.startswith("templates/") or path.startswith("sidecar/")):
            bad.append(f"symlink:{path}")
    return bad


def _template_files() -> list[Path]:
    found = []
    for _mode, rel in tracked_index():
        path = ROOT / rel
        if is_scanned_template(Path(rel)):
            found.append(path)
    return sorted(found)


def _entry_path(entry: dict) -> Path | None:
    rel = str(entry.get("path") or "")
    if not rel or Path(rel).is_absolute() or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
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


def _sources() -> list[dict]:
    data = json.loads(SOURCES.read_text(encoding="utf-8"))
    rows = data["sources"]
    assert isinstance(rows, list)
    return rows


def test_allowlist_and_heldout_files_are_exact():
    assert _ids(SOURCES) == ALLOW
    assert _ids(HELDOUT) == HELD
    rows = _sources()
    assert {row["match_id"] for row in rows} == ALLOW
    assert {row["folder"] for row in rows} == FOLDERS
    for row in rows:
        digest = str(row["video_sha256"])
        assert digest == "" or len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)


def test_scan_flags_sidecar_arrays_and_a_committed_qortime_pack():
    assert is_scanned_template(Path("sidecar/glimpse/leak.npz"))
    assert is_scanned_template(Path("sidecar/glimpse/leak.npy"))
    assert is_scanned_template(Path("sidecar/packs/old.npz"))
    assert is_scanned_template(Path("qortime/evil.npz"))
    assert is_scanned_template(Path("qortime-main/evil.npy"))
    assert is_scanned_template(Path("templates/glyph.jpg"))
    assert is_scanned_template(Path("templates/score/1.npy"))
    assert is_scanned_template(Path("sidecar/packs/banner.png"))
    assert not is_scanned_template(Path("sidecar/fixtures/m27.jpg"))
    assert not is_scanned_template(Path("docs/qa/shot.jpg"))
    assert not is_scanned_template(Path("docs/assets/shot.png"))
    assert not is_scanned_template(Path("README.png"))


def test_index_rejects_a_tracked_qortime_tree_and_template_symlinks():
    assert index_violations(tracked_index()) == []
    planted = [
        ("100644", "sidecar/glimpse/leak.npz"),
        ("100644", "qortime/evil.npz"),
        ("120000", "templates/score/link.npy"),
        ("120000", "sidecar/packs/link.npz"),
        ("120000", "docs/assets/shot.png"),
    ]
    assert index_violations(planted) == [
        "qortime/evil.npz",
        "symlink:templates/score/link.npy",
        "symlink:sidecar/packs/link.npz",
    ]
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/qortime/" in text and "/qortime-main/" in text


def test_untracked_qortime_pack_is_not_scanned():
    """CI's ``git init qortime`` checkout is untracked. It must not need a row."""
    path = ROOT / "qortime" / "evil.npz"
    path.parent.mkdir(exist_ok=True)
    np.savez(path, a=np.zeros(1, np.float32))
    try:
        rels = {item.relative_to(ROOT).as_posix() for item in _template_files()}
        assert "qortime/evil.npz" not in rels
    finally:
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass


def test_every_template_file_is_allowlisted_and_hashed():
    jsonschema = pytest.importorskip("jsonschema")
    data = json.loads(PROV.read_text(encoding="utf-8"))
    jsonschema.validate(data, SCHEMA)
    assert data["schema"] == "qorcap-template-provenance/v0"
    entries = data["templates"]
    assert isinstance(entries, list)
    by_id = {row["match_id"]: row for row in _sources()}
    by_file: dict[Path, dict] = {}
    for entry in entries:
        found = _entry_path(entry)
        assert found is not None, entry
        assert sha256_file(found) == entry["sha256"]
        mid = str(entry["source_match_id"])
        assert mid in ALLOW, mid
        assert mid not in HELD, mid
        src = by_id[mid]
        assert entry["video_sha256"] == src["video_sha256"]
        assert entry["source_match_folder"] == src["folder"]
        assert src["video_sha256"], mid
        assert glyph_name_matches(entry), entry
        by_file[found.resolve()] = entry
    for path in _template_files():
        assert path.resolve() in by_file, f"no provenance entry for {path.relative_to(ROOT)}"


def _valid_row() -> dict:
    return {
        "path": "score/102332__1__score_left__f000000__g0.npy",
        "sha256": "a" * 64,
        "role": "score",
        "label": "1",
        "profile": "standard",
        "source_match_id": "102332",
        "source_match_folder": "match-20261024-102332",
        "video_sha256": "b" * 64,
        "frame_index": 0,
        "t_ms": 0,
        "start_frame_index": 0,
        "end_frame_index": 0,
        "harvest_version": "3",
        "harvest_date": "2026-10-24T17:31:00Z",
        "qortime_commit": "0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2",
    }


def test_schema_rejects_a_bad_role_an_absolute_path_and_an_extra_field():
    jsonschema = pytest.importorskip("jsonschema")
    base = json.loads(PROV.read_text(encoding="utf-8"))

    def reject(row: dict) -> None:
        doc = dict(base, templates=[row])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, SCHEMA)

    banana = _valid_row()
    banana["role"] = "banana"
    reject(banana)
    absolute = _valid_row()
    absolute["path"] = "/tmp/evil.npy"
    reject(absolute)
    extra = _valid_row()
    extra["note"] = "no"
    reject(extra)
    wide = _valid_row()
    wide["label"] = "14"
    reject(wide)
    dated = _valid_row()
    dated["harvest_date"] = "2026-10-24"
    reject(dated)
    assert glyph_name_matches(_valid_row())
    relabelled = _valid_row()
    relabelled["label"] = "7"
    assert not glyph_name_matches(relabelled)
    wrong_role = _valid_row()
    wrong_role["role"] = "clock"
    assert not glyph_name_matches(wrong_role)


def test_legacy_end_menu_pack_is_hash_pinned_and_drops_125430():
    """The recorded-frame final check reads this pack. It is not a harvest row."""
    from qorcap_endmenu import LEGACY_END_MENU_SHA256

    path = ROOT / "sidecar" / "packs" / "madden27_end_menu_v2.npz"
    assert path.is_file()
    assert not is_scanned_template(Path("sidecar/packs/madden27_end_menu_v2.npz"))
    assert is_scanned_template(Path("sidecar/packs/old.npz"))
    assert sha256_file(path) == LEGACY_END_MENU_SHA256
    with np.load(path) as blob:
        sources = [str(x) for x in blob["source"]]
        labels = {str(x) for x in blob["labels"]}
    assert set("0123456789") <= labels
    assert "real:95" not in sources
    for item in sorted(HELD):
        assert all(item not in src for src in sources), item


def test_held_out_ids_are_not_in_template_pack_metadata():
    blob = "\n".join(_pack_strings())
    for item in sorted(HELD):
        assert item not in blob, item
