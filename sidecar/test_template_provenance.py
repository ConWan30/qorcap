"""CI source check for glyph templates.

Fails when a template file has no provenance row, a sha256 does not match, a
source match is held out or not allowlisted, a row fails the JSON Schema
(absolute path, unknown role, extra field), or a held-out id is stored in
template-pack metadata. ``held_out_sha256.json`` is a blocklist of public
clips, not a template pack, so it is not scanned for those ids.

``.npz`` and ``.npy`` anywhere under ``sidecar/`` (including ``glimpse``) and
image or array files under ``templates/`` need a provenance row. Fixture
images and docs screenshots are not templates.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))

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
# CI clones the QorTime pin into the workspace as qortime/. Its stock
# madden27_standard_v1.npz is not a QorCap template. qortime-main is the
# unpinned contracts checkout. Neither tree is part of this repo.
SKIP_TOP = {"qortime", "qortime-main", ".git"}
ARRAY_EXTS = {".npz", ".npy"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
SKIP_JSON = {"held_out_sha256.json"}


def _ids(path: Path) -> set[str]:
    return {str(x) for x in json.loads(path.read_text(encoding="utf-8"))["match_ids"]}


def is_scanned_template(rel: Path) -> bool:
    """True when a repo-relative path must have a provenance row.

    The CI job checks the QorTime pin out into ``qortime/`` (and qortime main
    into ``qortime-main/``). Those trees are not this repo. A ``.jpg`` under
    ``templates/`` and an ``.npz`` or ``.npy`` anywhere under ``sidecar/``
    (including ``glimpse``) still count.
    """
    if not rel.parts or rel.parts[0] in SKIP_TOP:
        return False
    suffix = rel.suffix.lower()
    if suffix not in ARRAY_EXTS | IMAGE_EXTS:
        return False
    if any(rel.parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES):
        return False
    if suffix in ARRAY_EXTS:
        return True
    return rel.parts[:1] == ("templates",) or rel.parts[:2] == ("sidecar", "packs")


def _template_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if is_scanned_template(rel):
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


def test_scan_ignores_the_ci_qortime_checkout_and_still_flags_sidecar_arrays():
    stock = Path(
        "qortime/qor_receipt/_vendor/qoresence/vision/local_scorebug/packs/madden27_standard_v1.npz"
    )
    assert not is_scanned_template(stock)
    assert not is_scanned_template(Path("qortime-main/packs/madden27_standard_v1.npz"))
    assert is_scanned_template(Path("sidecar/glimpse/leak.npz"))
    assert is_scanned_template(Path("sidecar/glimpse/leak.npy"))
    assert is_scanned_template(Path("sidecar/packs/old.npz"))
    assert is_scanned_template(Path("templates/glyph.jpg"))
    assert is_scanned_template(Path("templates/score/1.npy"))
    assert is_scanned_template(Path("sidecar/packs/banner.png"))
    assert not is_scanned_template(Path("sidecar/fixtures/m27.jpg"))
    assert not is_scanned_template(Path("docs/qa/shot.jpg"))
    assert not is_scanned_template(Path("docs/assets/shot.png"))
    assert not is_scanned_template(Path("README.png"))


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
        by_file[found.resolve()] = entry
    for path in _template_files():
        assert path.resolve() in by_file, f"no provenance entry for {path.relative_to(ROOT)}"


def _valid_row() -> dict:
    return {
        "path": "score/1__score_left__f000000__g0.npy",
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
        "harvest_version": "2",
        "harvest_date": "2026-10-24T17:31:00Z",
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


def test_held_out_ids_are_not_in_template_pack_metadata():
    blob = "\n".join(_pack_strings())
    for item in sorted(HELD):
        assert item not in blob, item
