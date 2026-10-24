"""Cut scorebug and end-menu glyph templates from an allowlisted match folder.

The match folder is read only. Templates and ``provenance.json`` are written to
``--out``, which must sit outside that folder. A match id that is not in
``templates/template_sources.json``, or any id in ``templates/heldout.json``,
is refused before a template is written.

This is a QorCap-internal tool. It does not write a receipt and it does not
touch a vendored QorTime contract.

PowerShell (from the repo, game 1; swap the id for game 2 ``111640``)::

    python tools\\harvest_templates.py --match-dir "$env:USERPROFILE\\Documents\\QorCap Matches\\match-20261024-102332" --labels "$env:USERPROFILE\\Documents\\QorCap Templates\\labels-102332.json" --out "$env:USERPROFILE\\Documents\\QorCap Templates\\out-102332"

``--windows-doc`` prints that one-liner and writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

HARVEST_VERSION = "1"
HARVEST_SCRIPT = "tools/harvest_templates.py"
# Boxes are x1, x2, y1, y2 on a 640x360 frame. Standard boxes are the vendored
# madden27-standard-v1 profile. End-menu boxes are qorcap_endmenu.SIDES / BAND_Y.
# Banner rows are the standard reference band (y 330) plus qorcap_banner._ROI.
ROIS: dict[str, tuple[int, int, int, int]] = {
    "score_left": (220, 246, 342, 357),
    "score_right": (257, 284, 342, 357),
    "clock": (447, 480, 344, 353),
    "quarter": (430, 447, 344, 353),
    "banner": (160, 450, 338, 358),
    "end_menu_away": (280, 368, 28, 66),
    "end_menu_home": (384, 462, 28, 66),
}
NORM_SIZE = {
    "score": (10, 14),
    "clock": (6, 9),
    "quarter": (17, 9),
    "banner": (64, 20),
    "end_menu": (12, 18),
}
PROFILE_OF = {
    "score": "standard",
    "clock": "standard",
    "quarter": "standard",
    "banner": "standard",
    "end_menu": "end_menu",
}
ROLES = tuple(NORM_SIZE)
POWERSHELL_ONELINER = (
    'python tools\\harvest_templates.py --match-dir '
    '"$env:USERPROFILE\\Documents\\QorCap Matches\\match-20261024-102332" '
    '--labels "$env:USERPROFILE\\Documents\\QorCap Templates\\labels-102332.json" '
    '--out "$env:USERPROFILE\\Documents\\QorCap Templates\\out-102332"'
)
WINDOWS_DOC = f"""Harvest scorebug templates on Windows
=====================================

Run this from the qorcap repo in PowerShell. The match folder is only read.
``--out`` must be a different folder, not inside the match. Game 1 is
match-20261024-102332. Game 2 is match-20261024-111640 (change the three
paths). Both ids are the only entries in templates\\template_sources.json.
templates\\heldout.json is refused even if an allowlist names one of those ids.

One-liner:

{POWERSHELL_ONELINER}

Labels file (human picks every glyph; the script does not guess a digit):

{{
  "items": [
    {{"role": "score", "label": "0", "roi": "score_left", "frame_index": 12, "t_ms": 400}}
  ]
}}

Roles: score, clock, quarter, banner, end_menu.
ROIs: score_left, score_right, clock, quarter, banner, end_menu_away, end_menu_home.

After both games, copy the written files into the repo templates\\ directory
(keep paths relative to the provenance file) and commit templates\\provenance.json
with them. Do not commit the match video. CI rejects a template with no
provenance row, a bad sha256, or a source_match_id other than 102332 or 111640.
"""

_FOLDER = re.compile(r"^match-(\d{4})(\d{2})(\d{2})-(\d{6})$")
_LABEL = re.compile(r"^[A-Za-z0-9]{1,16}$")


class Refuse(Exception):
    """A match or a path the script will not harvest. Nothing is written."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_ids(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(x) for x in data["match_ids"]}


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _parse_folder(match_dir: Path) -> tuple[str, str]:
    match = _FOLDER.fullmatch(match_dir.name)
    if match is None:
        raise Refuse(
            f"refusing {match_dir.name}: match folder must be named match-YYYYMMDD-HHMMSS. "
            "no templates written."
        )
    year, month, day, short = match.groups()
    meta = match_dir / "match.json"
    if meta.is_file():
        try:
            stated = str(json.loads(meta.read_text(encoding="utf-8")).get("match_id") or "")
        except (OSError, ValueError) as exc:
            raise Refuse(f"refusing unreadable match.json ({exc}). no templates written.") from exc
        if stated and short not in stated and stated != short:
            raise Refuse("refusing: match.json match_id does not match the folder. no templates written.")
    return short, f"{year}-{month}-{day}"


def _held_hit(text: str, held: set[str]) -> str | None:
    for item in held:
        if item and item in text:
            return item
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _held_out_hashes(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.get("sha256", {}).items()}


def _norm(patch, size: tuple[int, int]):
    import cv2
    import numpy as np

    arr = np.asarray(patch)
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    arr = arr.astype(np.float32)
    if arr.size == 0:
        return None
    if float(arr.max()) > 1.0:
        arr = arr / 255.0
    vec = cv2.resize(arr, size, interpolation=cv2.INTER_AREA).ravel().astype(np.float32)
    vec = vec - float(vec.mean())
    norm = float(np.linalg.norm(vec))
    if norm < 1e-6:
        return None
    return vec / norm


def _read_frame(video: Path, index: int):
    """Decode frames 0..index in order. Seeking by timestamp is not used."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise Refuse(f"refusing: cannot read {video.name}. no templates written.")
    frame = None
    try:
        for _ in range(index + 1):
            ok, frame = cap.read()
            if not ok or frame is None:
                raise Refuse(f"refusing: video has no frame {index}. no templates written.")
    finally:
        cap.release()
    return frame


def _prepare_frame(frame):
    import cv2

    if frame.shape[1] != 640 or frame.shape[0] != 360:
        frame = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
    return frame


def _load_labels(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refuse(f"refusing unreadable labels ({exc}). no templates written.") from exc
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise Refuse("refusing: labels file needs a non-empty items list. no templates written.")
    out = []
    seen: set[tuple] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise Refuse("refusing: a label item is not an object. no templates written.")
        role = str(raw.get("role") or "")
        label = str(raw.get("label") or "")
        roi = str(raw.get("roi") or "")
        if role not in ROLES or roi not in ROIS or _LABEL.fullmatch(label) is None:
            raise Refuse(
                f"refusing label role={role!r} roi={roi!r} label={label!r}. no templates written."
            )
        frame_index = raw.get("frame_index")
        t_ms = raw.get("t_ms")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
            raise Refuse("refusing: each label needs an integer frame_index >= 0. no templates written.")
        if t_ms is not None and (isinstance(t_ms, bool) or not isinstance(t_ms, int) or t_ms < 0):
            raise Refuse("refusing: t_ms must be an integer >= 0 or omitted. no templates written.")
        key = (role, label, roi, frame_index)
        if key in seen:
            raise Refuse(f"refusing duplicate label {key}. no templates written.")
        seen.add(key)
        out.append({
            "role": role,
            "label": label,
            "roi": roi,
            "frame_index": frame_index,
            "t_ms": t_ms,
        })
    out.sort(key=lambda item: (item["role"], item["label"], item["roi"], item["frame_index"]))
    return out


def _snapshot(root: Path) -> dict[str, str]:
    found = {}
    if not root.exists():
        return found
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        found[str(path.relative_to(root))] = _sha256(path)
    return found


def harvest(match_dir: Path, out_dir: Path, labels_path: Path, allow_path: Path, held_path: Path,
            hash_path: Path) -> dict:
    """Build the output document and file bytes. Writes only under ``out_dir`` after every check."""
    match_dir = match_dir.resolve()
    out_dir = out_dir.resolve()
    if not match_dir.is_dir():
        raise Refuse(f"refusing: {match_dir} is not a match folder. no templates written.")
    before = _snapshot(match_dir)
    short, harvest_date = _parse_folder(match_dir)
    try:
        allowed = _load_ids(allow_path)
        held = _load_ids(held_path)
    except (OSError, ValueError, KeyError) as exc:
        raise Refuse(f"refusing unreadable allowlist or held-out list ({exc}). no templates written.") from exc
    hit = _held_hit(match_dir.name, held) or (short if short in held else None)
    if hit:
        raise Refuse(f"refusing held-out match {hit}. no templates written.")
    if short not in allowed:
        raise Refuse(f"refusing match {short}: not in the template-source allowlist. no templates written.")
    if _inside(out_dir, match_dir) or _inside(match_dir, out_dir):
        raise Refuse("refusing: --out must be outside the match folder. no templates written.")
    video = match_dir / "video.mp4"
    if not video.is_file():
        raise Refuse("refusing: match folder has no video.mp4. no templates written.")
    if not (match_dir / "timeline.jsonl").is_file() and not (match_dir / "anchors.jsonl").is_file():
        raise Refuse("refusing: match folder has no timeline.jsonl or anchors.jsonl. no templates written.")
    for path in match_dir.rglob("*"):
        found = _held_hit(path.name, held)
        if found:
            raise Refuse(f"refusing held-out name {path.name} ({found}). no templates written.")
    try:
        table = _held_out_hashes(hash_path)
    except (OSError, ValueError) as exc:
        raise Refuse(f"refusing: held-out sha256 table unreadable ({exc}). no templates written.") from exc
    video_hash = _sha256(video)
    if video_hash in table:
        raise Refuse(
            f"refusing held-out footage (sha256 matches {table[video_hash]}). no templates written."
        )
    labels = _load_labels(labels_path)
    # Decode each requested frame once, in index order.
    frames = {}
    for index in sorted({item["frame_index"] for item in labels}):
        frames[index] = _prepare_frame(_read_frame(video, index))
    built: list[tuple[str, bytes, dict]] = []
    for item in labels:
        frame = frames[item["frame_index"]]
        x1, x2, y1, y2 = ROIS[item["roi"]]
        crop = frame[y1:y2, x1:x2]
        vec = _norm(crop, NORM_SIZE[item["role"]])
        if vec is None:
            raise Refuse(
                f"refusing flat crop role={item['role']} label={item['label']} "
                f"frame={item['frame_index']}. no templates written."
            )
        stem = f"{item['role']}/{item['label']}__{item['roi']}__f{item['frame_index']:06d}"
        npy_name = stem + ".npy"
        png_name = stem + ".png"
        import cv2
        import numpy as np

        buf = io.BytesIO()
        np.save(buf, np.ascontiguousarray(vec, dtype=np.float32))
        npy_bytes = buf.getvalue()
        ok, png_buf = cv2.imencode(".png", crop, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok:
            raise Refuse("refusing: png encode failed. no templates written.")
        png_bytes = png_buf.tobytes()
        for name, payload in ((npy_name, npy_bytes), (png_name, png_bytes)):
            built.append((name, payload, {
                "path": name.replace("\\", "/"),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "role": item["role"],
                "label": item["label"],
                "profile": PROFILE_OF[item["role"]],
                "source_match_id": short,
                "t_ms": item["t_ms"],
                "frame_index": item["frame_index"],
                "harvest_version": HARVEST_VERSION,
                "harvest_date": harvest_date,
            }))
    if _snapshot(match_dir) != before:
        raise Refuse("refusing: match folder changed while reading. no templates written.")
    doc = {
        "schema": "qorcap-template-provenance/v0",
        "harvest_script": HARVEST_SCRIPT,
        "harvest_version": HARVEST_VERSION,
        "templates": [row for _name, _payload, row in built],
    }
    body = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    # Write only after every crop succeeded, and only outside the match folder.
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qorcap-harvest-", dir=str(out_dir.parent)) as tmp_name:
        tmp = Path(tmp_name)
        for name, payload, _row in built:
            dest = tmp / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
        (tmp / "provenance.json").write_bytes(body)
        (tmp / "HARVEST_WINDOWS.txt").write_text(WINDOWS_DOC, encoding="utf-8", newline="\n")
        out_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(p for p in tmp.rglob("*") if p.is_file()):
            rel = path.relative_to(tmp)
            target = out_dir / rel
            if _inside(target, match_dir):
                raise Refuse("refusing: a template path landed inside the match folder. no templates written.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    if _snapshot(match_dir) != before:
        raise Refuse("refusing: match folder changed. harvest output is not trusted.")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest allowlisted scorebug templates outside the match folder")
    ap.add_argument("--match-dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--labels", type=Path)
    root = _repo_root()
    ap.add_argument("--allowlist", type=Path, default=root / "templates" / "template_sources.json")
    ap.add_argument("--heldout", type=Path, default=root / "templates" / "heldout.json")
    ap.add_argument("--heldout-sha256", type=Path, default=root / "sidecar" / "packs" / "held_out_sha256.json")
    ap.add_argument("--windows-doc", action="store_true", help="print the PowerShell one-liner and write nothing")
    args = ap.parse_args(argv)
    if args.windows_doc:
        print(POWERSHELL_ONELINER)
        return 0
    if args.match_dir is None or args.out is None or args.labels is None:
        print("refusing: --match-dir, --labels, and --out are required. no templates written.", file=sys.stderr)
        print(POWERSHELL_ONELINER, file=sys.stderr)
        return 2
    try:
        doc = harvest(args.match_dir, args.out, args.labels, args.allowlist, args.heldout, args.heldout_sha256)
    except Refuse as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote {len(doc['templates'])} template files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
