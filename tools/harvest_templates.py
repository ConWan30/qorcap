"""Cut per-glyph scorebug and end-menu templates from an allowlisted match folder.

The match folder is read only. Templates and ``provenance.json`` are written to
``--out``, which must sit outside that folder. A match id that is not in
``templates/template_sources.json``, or any id in ``templates/heldout.json``,
is refused before a template is written. The allowlist entry must pin the
sha256 of ``video.mp4``. An empty pin refuses the harvest.

Each saved vector is one glyph, segmented and normalised by the same functions
the reader uses at read time. A label such as ``14`` is the slot text: it is
kept only when the reader segments that many glyphs, and each glyph is stored
under its own digit.

This is a QorCap-internal tool. It does not write a receipt and it does not
touch a vendored QorTime contract.

PowerShell, one self-contained block per game, is in ``docs/HARVEST_RUNBOOK.md``.
It defines ``$docs``, ``$py``, and ``QORTIME``, pins the video, and dry-runs
before it writes. ``--dry-run`` lists the frames and crops and writes nothing.
``--windows-doc`` prints the game-1 block and writes nothing. A non-empty
``--out`` is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HARVEST_VERSION = "3"
HARVEST_SCRIPT = "tools/harvest_templates.py"
# Which reader role a ROI belongs to. Banner fragments are not a per-glyph npy:
# qorcap_banner matches GOA/TOUC masks and does not call harvested_vectors.
ROI_ROLE = {
    "score_left": "score",
    "score_right": "score",
    "clock": "clock",
    "quarter": "quarter",
    "end_menu_away": "end_menu",
    "end_menu_home": "end_menu",
}
QUARTER_LABELS = {"1st", "2nd", "3rd", "4th", "ot"}
PROFILE_OF = {
    "score": "standard",
    "clock": "standard",
    "quarter": "standard",
    "end_menu": "end_menu",
}
# Same pin as sidecar/qorcap_match.py QORTIME_PIN. The runbook sets QORTIME to the
# tree Start Match already installed, so a fresh window does not decode before failing.
QORTIME_PIN = "0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2"
POWERSHELL_ONELINER = (
    'cd "$env:USERPROFILE\\src\\qorcap"\n'
    '$docs = [Environment]::GetFolderPath("MyDocuments")\n'
    '$py   = "$env:LOCALAPPDATA\\QorCap\\venv\\Scripts\\python.exe"\n'
    f'$env:QORTIME = "$env:LOCALAPPDATA\\QorCap\\qortime-{QORTIME_PIN}"\n'
    'New-Item -ItemType Directory -Force "$docs\\QorCap Templates" | Out-Null\n'
    '& $py tools\\pin_template_source.py --match-id 102332 '
    '--video "$docs\\QorCap Matches\\match-20261024-102332\\video.mp4"\n'
    '& $py tools\\harvest_templates.py --dry-run '
    '--match-dir "$docs\\QorCap Matches\\match-20261024-102332" '
    '--labels "$docs\\QorCap Templates\\labels-102332.json" '
    '--out "$docs\\QorCap Templates\\out-102332"\n'
    '& $py tools\\harvest_templates.py '
    '--match-dir "$docs\\QorCap Matches\\match-20261024-102332" '
    '--labels "$docs\\QorCap Templates\\labels-102332.json" '
    '--out "$docs\\QorCap Templates\\out-102332"'
)
WINDOWS_DOC = f"""Harvest scorebug templates on Windows
=====================================

Run this from a git checkout of qorcap. ``tools\\`` is not in the release zip.
Start Match's venv has numpy and opencv; bare ``python`` may not.
``Documents`` may be redirected to OneDrive. ``GetFolderPath("MyDocuments")``
is the same resolution Start Match uses.

The match folder is only read. ``--out`` must be a different folder, not inside
the match. Game 1 is match-20261024-102332. Game 2 is match-20261024-111640
(change the id in the three paths). Both ids are the only entries in
templates\\template_sources.json. templates\\heldout.json is refused even if an
allowlist names one of those ids.

Before the first harvest, pin the video with tools\\pin_template_source.py.
Get-FileHash prints uppercase hex. The pin tool stores lowercase sha256 and
refuses an id that is not allowlisted. Do not paste the hash by hand. An empty
pin refuses the harvest. A folder renamed to an allowlisted name is refused
unless ``video.mp4`` matches that entry. Set QORTIME (or pass --qortime)
before the run. The script checks QorTime before it hashes or decodes the video.

``sidecar\\packs\\held_out_sha256.json`` lists public frame and clip hashes,
not full match-video hashes. The folder name plus this pin is the guard.

One-liner:

{POWERSHELL_ONELINER}

Dry-run (same arguments plus ``--dry-run``) lists the frames and per-glyph
crops and writes nothing.

Labels file (a person names every slot; the script does not guess a digit).
``label`` is the slot text. Its length, after a clock colon is removed, must
equal the number of glyphs the reader segments. Each glyph is saved on its own.

{{
  "items": [
    {{"role": "score", "label": "14", "roi": "score_left", "frame_index": 1200, "start_frame_index": 1200, "end_frame_index": 1260}}
  ]
}}

Roles: score, clock, quarter, end_menu.
ROIs: score_left, score_right, clock, quarter, end_menu_away, end_menu_home.
Score, clock, and end-menu labels are digits 0-9, one per segmented glyph.
A clock label may contain a colon (``5:23`` is three digits). Quarter labels
are 1st, 2nd, 3rd, 4th, or ot. Banner is not harvested: no reader loads a
harvested banner vector.

Each item needs ``frame_index`` and a range that contains that frame:
``start_frame_index`` and ``end_frame_index``, or ``start_t_ms`` and
``end_t_ms``, or both. The script checks the range against the decoded frame.
``harvest_date`` is the UTC time of the harvest, not the match-folder date.

Sunday, Monday, Thursday, and PSN readers have no harvest regions. A standard
harvest does not bring those readers back.

After both games, copy the written files into the repo templates\\ directory
(keep paths relative to the provenance file) and commit templates\\provenance.json
with them. Do not commit the match video. CI rejects a template with no
provenance row, a bad sha256, a source match other than 102332 or 111640, or a
row that does not match the provenance schema.
"""

_FOLDER = re.compile(r"^match-(\d{8})-(\d{6})$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class Refuse(Exception):
    """A match or a path the script will not harvest. Nothing is written."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_sources(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refuse(f"refusing unreadable allowlist ({exc}). no templates written.") from exc
    rows = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise Refuse("refusing: allowlist needs a sources list with video_sha256. no templates written.")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise Refuse("refusing: an allowlist source is not an object. no templates written.")
        mid = str(row.get("match_id") or "")
        folder = str(row.get("folder") or "")
        digest = str(row.get("video_sha256") or "").strip().lower()
        if not re.fullmatch(r"\d{6}", mid) or not _FOLDER.fullmatch(folder):
            raise Refuse(
                f"refusing allowlist entry match_id={mid!r} folder={folder!r}. no templates written."
            )
        if folder[-6:] != mid:
            raise Refuse(
                f"refusing: allowlist folder {folder} does not end in match id {mid}. no templates written."
            )
        out.append({"match_id": mid, "folder": folder, "video_sha256": digest})
    return out


def _load_ids(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(x) for x in data["match_ids"]}


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _parse_folder(match_dir: Path) -> str:
    match = _FOLDER.fullmatch(match_dir.name)
    if match is None:
        raise Refuse(
            f"refusing {match_dir.name}: match folder must be named match-YYYYMMDD-HHMMSS. "
            "no templates written."
        )
    short = match.group(2)
    meta = match_dir / "match.json"
    if meta.is_file():
        try:
            stated = str(json.loads(meta.read_text(encoding="utf-8")).get("match_id") or "")
        except (OSError, ValueError) as exc:
            raise Refuse(f"refusing unreadable match.json ({exc}). no templates written.") from exc
        if stated and short not in stated and stated != short:
            raise Refuse("refusing: match.json match_id does not match the folder. no templates written.")
    return short


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


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Size and mtime only. The video hash is taken once, separately."""
    found = {}
    if not root.exists():
        return found
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        st = path.stat()
        found[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return found


def _harvest_date(now) -> str:
    when = now if now is not None else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc).replace(microsecond=0)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt_int(raw: dict, key: str):
    if key not in raw or raw[key] is None:
        return None
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise Refuse(f"refusing: {key} must be an integer >= 0. no templates written.")
    return val


def _pair(raw: dict, start: str, end: str):
    lo, hi = _opt_int(raw, start), _opt_int(raw, end)
    if (lo is None) != (hi is None):
        raise Refuse(f"refusing: {start} and {end} are both required. no templates written.")
    if lo is not None and lo > hi:
        raise Refuse(f"refusing: {start} is after {end}. no templates written.")
    return lo, hi


def _slot_text(role: str, label: str) -> str:
    if role == "banner":
        raise Refuse(
            "refusing role=banner: no reader loads a harvested banner vector. no templates written."
        )
    if role == "quarter":
        word = label.lower()
        if word not in QUARTER_LABELS:
            raise Refuse(
                f"refusing quarter label {label!r}. use 1st, 2nd, 3rd, 4th, or ot. no templates written."
            )
        return word
    text = label.replace(":", "") if role == "clock" else label
    if not text or any(ch not in "0123456789" for ch in text):
        raise Refuse(
            f"refusing {role} label {label!r}: digits 0-9 only, one per glyph. no templates written."
        )
    return text


def _load_labels(path: Path) -> list[dict]:
    try:
        # utf-8-sig accepts a BOM from Windows Notepad and a plain UTF-8 file.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Refuse(
            f"refusing unreadable labels ({exc}). save labels as UTF-8 JSON. no templates written."
        ) from exc
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise Refuse("refusing: labels file needs a non-empty items list. no templates written.")
    out = []
    seen: set[tuple] = set()
    slots: dict[tuple, str] = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise Refuse("refusing: a label item is not an object. no templates written.")
        role = str(raw.get("role") or "")
        label = str(raw.get("label") or "")
        roi = str(raw.get("roi") or "")
        if roi not in ROI_ROLE or ROI_ROLE[roi] != role:
            raise Refuse(
                f"refusing label role={role!r} roi={roi!r} label={label!r}. no templates written."
            )
        text = _slot_text(role, label)
        frame_index = raw.get("frame_index")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
            raise Refuse("refusing: each label needs an integer frame_index >= 0. no templates written.")
        start_f, end_f = _pair(raw, "start_frame_index", "end_frame_index")
        start_t, end_t = _pair(raw, "start_t_ms", "end_t_ms")
        if start_f is None and start_t is None:
            raise Refuse(
                "refusing: each label needs a frame range (start_frame_index, end_frame_index) "
                "or a time range (start_t_ms, end_t_ms). no templates written."
            )
        key = (role, label, roi, frame_index, start_f, end_f, start_t, end_t)
        if key in seen:
            raise Refuse(f"refusing duplicate label {key}. no templates written.")
        seen.add(key)
        slot = (roi, frame_index)
        if slot in slots and slots[slot] != label:
            raise Refuse(
                f"refusing: {roi} frame {frame_index} is labelled {slots[slot]!r} and {label!r}. "
                "one label per slot per frame. no templates written."
            )
        slots[slot] = label
        out.append({
            "role": role,
            "label": label,
            "text": text,
            "roi": roi,
            "frame_index": frame_index,
            "start_frame_index": start_f,
            "end_frame_index": end_f,
            "start_t_ms": start_t,
            "end_t_ms": end_t,
        })
    out.sort(key=lambda item: (item["role"], item["label"], item["roi"], item["frame_index"]))
    return out


def _open_video(video: Path):
    """One capture. Callers decode with ``read`` in order and never seek."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise Refuse(f"refusing: cannot read {video.name}. no templates written.")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    return cap, fps, count


def _measured_msec(cap) -> int:
    import cv2

    msec = float(cap.get(cv2.CAP_PROP_POS_MSEC))
    if msec != msec or msec < 0:
        msec = 0.0
    return int(round(msec))


def iter_decoded(video: Path):
    """Sequential frames in the harvester's decode order.

    Yields ``(index, 640x360 frame, t_ms, fps, frame_count)``. Preview and
    harvest both use this so a printed ``frame_index`` is the index harvest cuts.
    """
    cap, fps, count = _open_video(video)
    try:
        cursor = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            yield cursor, _prepare_frame(frame), _measured_msec(cap), fps, count
            cursor += 1
    finally:
        cap.release()


def _read_frames(video: Path, indices: set[int]) -> dict[int, tuple]:
    """One sequential decode. ``index -> (640x360 frame, container t_ms)``."""
    needed = sorted(indices)
    found: dict[int, tuple] = {}
    want = 0
    for cursor, frame, msec, _fps, _count in iter_decoded(video):
        if want >= len(needed):
            break
        if cursor == needed[want]:
            found[cursor] = (frame, msec)
            want += 1
        elif cursor > needed[want]:
            break
    if want < len(needed):
        raise Refuse(f"refusing: video has no frame {needed[want]}. no templates written.")
    return found


def _prepare_frame(frame):
    import cv2

    if frame.shape[1] != 640 or frame.shape[0] != 360:
        frame = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
    return frame


def _resolve_qortime(arg: str | None) -> tuple[Path, str]:
    """The sidecar search, before any video byte is hashed or decoded.

    ``--qortime``, then ``$QORTIME``, then ``<repo>/qortime``, then the tree
    Start Match installed under ``$QORCAP_HOME`` or ``%LOCALAPPDATA%\\QorCap``
    (``qortime-<pin>``). This does not download.
    """
    root = _repo_root()
    side = str(root / "sidecar")
    if side not in sys.path:
        sys.path.insert(0, side)
    from qorcap_match import find_qortime

    try:
        found = find_qortime(arg, download=False)
    except SystemExit as exc:
        detail = exc.code if isinstance(exc.code, str) else exc
        raise Refuse(
            f"refusing: QorTime was not found ({detail}). "
            "Pass --qortime, set QORTIME, or install the pinned tree under "
            "QORCAP_HOME or %LOCALAPPDATA%\\QorCap. no templates written."
        ) from exc
    return found, _qortime_commit(found)


def _qortime_commit(path: Path) -> str:
    import subprocess

    if (path / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True,
        )
        rev = (proc.stdout or "").strip().lower()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", rev):
            return rev
    pinned = path.name[len("qortime-"):] if path.name.startswith("qortime-") else ""
    if re.fullmatch(r"[0-9a-f]{40}", pinned):
        return pinned
    raise Refuse(
        f"refusing: {path} has no QorTime commit. no templates written."
    )


def _reader_stack(qortime: Path):
    """The reader's own segmenters. Nothing here reimplements ink or components."""
    root = _repo_root()
    for entry in (str(root / "sidecar"), str(qortime)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from dataclasses import replace

    from qor_receipt._vendor.qoresence.vision.local_scorebug.glyphs import GLYPH_SIZE
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader, _shift
    from qorcap_endmenu import GLYPH, HOLES, NO_HOLE, hole_signature, norm_vec, side_glyphs
    from qorcap_standard import ANCHOR_SEARCH_PX

    reader = ScorebugReader()
    reader.profile = replace(reader.profile, anchor_search_px=ANCHOR_SEARCH_PX)
    return reader, _shift, GLYPH_SIZE, side_glyphs, norm_vec, hole_signature, HOLES, NO_HOLE, GLYPH


def _check_range(item: dict, measured_t_ms: int) -> None:
    frame_index = item["frame_index"]
    if item["start_frame_index"] is not None:
        if not item["start_frame_index"] <= frame_index <= item["end_frame_index"]:
            raise Refuse(
                f"refusing: frame {frame_index} is outside the labelled frame range "
                f"{item['start_frame_index']}-{item['end_frame_index']}. no templates written."
            )
    if item["start_t_ms"] is not None:
        if not item["start_t_ms"] <= measured_t_ms <= item["end_t_ms"]:
            raise Refuse(
                f"refusing: measured t_ms {measured_t_ms} for frame {frame_index} is outside "
                f"the labelled range {item['start_t_ms']}-{item['end_t_ms']}. no templates written."
            )


def _preview(vec, width: int, height: int):
    import cv2
    import numpy as np

    img = np.asarray(vec, dtype=np.float32).reshape(height, width)
    img = img - float(img.min())
    peak = float(img.max())
    if peak > 0:
        img = img / peak
    shown = (img * 255.0).astype(np.uint8)
    ok, buf = cv2.imencode(".png", shown, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise Refuse("refusing: png encode failed. no templates written.")
    return buf.tobytes()


def _npy(vec) -> bytes:
    import numpy as np

    buf = io.BytesIO()
    np.save(buf, np.ascontiguousarray(vec, dtype=np.float32))
    return buf.getvalue()


def _segment(reader, shift, glyph_size, side_glyphs, norm_vec, hole_signature, holes, no_hole, end_glyph,
             frame, item) -> list[tuple[str, object]]:
    """One ``(label, vector)`` per glyph, from the reader's segmenter."""
    role = item["role"]
    text = item["text"]
    if role == "end_menu":
        side = "away" if item["roi"] == "end_menu_away" else "home"
        masks = side_glyphs(frame, side)
        if not masks or any(mask is None for mask in masks):
            raise Refuse(
                f"refusing: end-menu segmentation failed for {item['roi']} "
                f"frame {item['frame_index']}. no templates written."
            )
        if len(masks) != len(text):
            raise Refuse(
                f"refusing: label {item['label']!r} has {len(text)} glyphs but the reader "
                f"segmented {len(masks)} on frame {item['frame_index']}. no templates written."
            )
        out = []
        for ch, mask in zip(text, masks):
            vec = norm_vec(mask)
            if vec is None:
                raise Refuse(
                    f"refusing flat end-menu glyph {ch!r} frame {item['frame_index']}. "
                    "no templates written."
                )
            signature = hole_signature(mask)
            if (ch in holes and signature not in holes[ch]) or (ch in no_hole and signature != "0"):
                raise Refuse(
                    f"refusing: end-menu glyph {ch!r} holes {signature} would be blanked at read time. "
                    "no templates written."
                )
            if int(vec.size) != end_glyph[0] * end_glyph[1]:
                raise Refuse("refusing: end-menu vector is not the reader's glyph size. no templates written.")
            out.append((ch, vec))
        return out

    band, why = reader.reference_band(frame)
    if band is None:
        raise Refuse(
            f"refusing: frame {item['frame_index']} has no scorebug band ({why}). no templates written."
        )
    layout, (dx, dy), _info = reader.check_layout(band)
    if layout != "ok":
        raise Refuse(
            f"refusing: frame {item['frame_index']} is not a standard bar the reader would score "
            f"({layout}). no templates written."
        )
    if role == "score":
        box = reader.profile.left_score if item["roi"] == "score_left" else reader.profile.right_score
        slot = reader._crop(band, shift(box, dx, dy))
        glyphs, gwhy, _boxes = reader.score_glyphs(slot)
        if glyphs is None:
            raise Refuse(
                f"refusing: score segmentation failed ({gwhy}) role={role} label={item['label']!r} "
                f"frame={item['frame_index']}. no templates written."
            )
        if len(glyphs) != len(text):
            raise Refuse(
                f"refusing: label {item['label']!r} has {len(text)} glyphs but the reader segmented "
                f"{len(glyphs)} on frame {item['frame_index']}. no templates written."
            )
        rules = dict(reader.profile.score_hole_rules)
        out = []
        width, height = glyph_size
        for ch, (vec, signature) in zip(text, glyphs):
            if signature == "2+" or (ch in rules and signature not in rules[ch]):
                raise Refuse(
                    f"refusing: score glyph {ch!r} holes {signature} would be blanked at read time. "
                    "no templates written."
                )
            if int(vec.size) != width * height:
                raise Refuse("refusing: score vector is not the reader's glyph size. no templates written.")
            out.append((ch, vec))
        return out
    if role == "clock":
        vecs, cwhy = reader.clock_glyphs(band, dx, dy)
        if vecs is None:
            raise Refuse(
                f"refusing: clock segmentation failed ({cwhy}) label={item['label']!r} "
                f"frame={item['frame_index']}. no templates written."
            )
        if len(vecs) != len(text):
            raise Refuse(
                f"refusing: label {item['label']!r} has {len(text)} glyphs but the reader segmented "
                f"{len(vecs)} on frame {item['frame_index']}. no templates written."
            )
        width, height = reader.profile.clock_glyph_size
        out = []
        for ch, vec in zip(text, vecs):
            if int(vec.size) != width * height:
                raise Refuse("refusing: clock vector is not the reader's glyph size. no templates written.")
            out.append((ch, vec))
        return out
    vec = reader.quarter_vector(band, dx, dy)
    if vec is None:
        raise Refuse(
            f"refusing: quarter segmentation failed label={item['label']!r} "
            f"frame={item['frame_index']}. no templates written."
        )
    x1, x2, y1, y2 = reader.profile.quarter
    if int(vec.size) != (x2 - x1) * (y2 - y1):
        raise Refuse("refusing: quarter vector is not the reader's quarter box. no templates written.")
    return [(text, vec)]


_GLYPH_NAME = re.compile(
    r"^(?P<role>score|clock|quarter|end_menu)/"
    r"(?P<match>[0-9]{6})__(?P<label>[0-9]|1st|2nd|3rd|4th|ot)__"
    r"(?P<roi>[a-z0-9_]+)__f(?P<frame>[0-9]{6})__g(?P<index>[0-9]+)\.(?P<ext>npy|png)$"
)


def glyph_name_matches(row: dict) -> bool:
    """The file name is the role, the match id, the glyph label, and the frame."""
    match = _GLYPH_NAME.fullmatch(str(row.get("path") or "").replace("\\", "/"))
    if match is None:
        return False
    return (
        match.group("role") == str(row.get("role") or "")
        and match.group("label") == str(row.get("label") or "")
        and match.group("match") == str(row.get("source_match_id") or "")
        and int(match.group("frame")) == row.get("frame_index")
    )


def _out_occupied(out_dir: Path) -> bool:
    if not out_dir.exists():
        return False
    if not out_dir.is_dir():
        return True
    return any(out_dir.iterdir())


def _row(name: str, payload: bytes, item: dict, glyph: str, short: str, folder: str,
         video_hash: str, measured_t_ms: int, harvest_date: str, qortime_commit: str) -> dict:
    row = {
        "path": name.replace("\\", "/"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "role": item["role"],
        "label": glyph,
        "profile": PROFILE_OF[item["role"]],
        "source_match_id": short,
        "source_match_folder": folder,
        "video_sha256": video_hash,
        "frame_index": item["frame_index"],
        "t_ms": measured_t_ms,
        "harvest_version": HARVEST_VERSION,
        "harvest_date": harvest_date,
        "qortime_commit": qortime_commit,
    }
    if item["start_frame_index"] is not None:
        row["start_frame_index"] = item["start_frame_index"]
        row["end_frame_index"] = item["end_frame_index"]
    if item["start_t_ms"] is not None:
        row["start_t_ms"] = item["start_t_ms"]
        row["end_t_ms"] = item["end_t_ms"]
    return row


def _preview_wh(reader, glyph_size, end_glyph, role: str) -> tuple[int, int]:
    if role == "score":
        return glyph_size
    if role == "clock":
        return tuple(reader.profile.clock_glyph_size)
    if role == "quarter":
        x1, x2, y1, y2 = reader.profile.quarter
        return (x2 - x1, y2 - y1)
    return end_glyph


def harvest(match_dir: Path, out_dir: Path, labels_path: Path, allow_path: Path, held_path: Path,
            hash_path: Path, *, dry_run: bool = False, now=None, qortime: str | None = None) -> dict:
    """Segment glyphs and, unless ``dry_run``, write them under ``out_dir`` only.

    QorTime is resolved before the match video is hashed or decoded.
    """
    qortime_path, qortime_commit = _resolve_qortime(qortime)
    if qortime_commit != QORTIME_PIN:
        print(
            f"warning: QorTime commit {qortime_commit} differs from the Start Match pin {QORTIME_PIN}",
            file=sys.stderr,
        )
    # realpath, not the path as given: a symlink named like an allowlisted match
    # still has the held-out folder on the other side of the link.
    match_dir = Path(os.path.realpath(match_dir))
    out_dir = Path(os.path.realpath(out_dir))
    if not match_dir.is_dir():
        raise Refuse(f"refusing: {match_dir} is not a match folder. no templates written.")
    before = _snapshot(match_dir)
    short = _parse_folder(match_dir)
    try:
        sources = _load_sources(allow_path)
        held = _load_ids(held_path)
    except Refuse:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise Refuse(f"refusing unreadable allowlist or held-out list ({exc}). no templates written.") from exc
    hit = _held_hit(match_dir.name, held) or (short if short in held else None)
    if hit:
        raise Refuse(f"refusing held-out match {hit}. no templates written.")
    entry = next((row for row in sources if row["match_id"] == short), None)
    if entry is None:
        raise Refuse(f"refusing match {short}: not in the template-source allowlist. no templates written.")
    if entry["folder"] != match_dir.name:
        raise Refuse(
            f"refusing: folder {match_dir.name} does not match the allowlist folder "
            f"{entry['folder']}. no templates written."
        )
    if _inside(out_dir, match_dir) or _inside(match_dir, out_dir):
        raise Refuse("refusing: --out must be outside the match folder. no templates written.")
    if _out_occupied(out_dir):
        raise Refuse("refusing: --out is not empty. no templates written.")
    video = match_dir / "video.mp4"
    if not video.is_file():
        raise Refuse("refusing: match folder has no video.mp4. no templates written.")
    if not (match_dir / "timeline.jsonl").is_file() and not (match_dir / "anchors.jsonl").is_file():
        raise Refuse("refusing: match folder has no timeline.jsonl or anchors.jsonl. no templates written.")
    for path in match_dir.rglob("*"):
        real = os.path.realpath(path)
        found = _held_hit(path.name, held) or _held_hit(real, held)
        if found:
            raise Refuse(f"refusing held-out name {path.name} ({found}) at {real}. no templates written.")
    try:
        table = _held_out_hashes(hash_path)
    except (OSError, ValueError) as exc:
        raise Refuse(f"refusing: held-out sha256 table unreadable ({exc}). no templates written.") from exc
    video_hash = _sha256(video)
    if video_hash in table:
        raise Refuse(
            f"refusing held-out footage (sha256 matches {table[video_hash]}). no templates written."
        )
    pinned = entry["video_sha256"]
    if not _SHA.fullmatch(pinned):
        raise Refuse(
            f"refusing: allowlist has no video_sha256 for {match_dir.name}. "
            "pin the sha256 of video.mp4 before harvesting. no templates written."
        )
    if pinned != video_hash:
        raise Refuse(
            f"refusing: video.mp4 sha256 does not match the allowlist entry for {match_dir.name}. "
            "no templates written."
        )
    labels = _load_labels(labels_path)
    frames = _read_frames(video, {item["frame_index"] for item in labels})
    for item in labels:
        _check_range(item, frames[item["frame_index"]][1])
    stack = _reader_stack(qortime_path)
    reader, shift, glyph_size, side_glyphs, norm_vec, hole_signature, holes, no_hole, end_glyph = stack
    harvest_date = _harvest_date(now)
    built: list[tuple[str, bytes, dict]] = []
    for item in labels:
        frame, measured = frames[item["frame_index"]]
        glyphs = _segment(
            reader, shift, glyph_size, side_glyphs, norm_vec, hole_signature, holes, no_hole, end_glyph,
            frame, item,
        )
        width, height = _preview_wh(reader, glyph_size, end_glyph, item["role"])
        for index, (glyph, vec) in enumerate(glyphs):
            stem = (
                f"{item['role']}/{short}__{glyph}__{item['roi']}__f{item['frame_index']:06d}__g{index}"
            )
            npy_name = stem + ".npy"
            png_name = stem + ".png"
            npy_bytes = _npy(vec)
            png_bytes = _preview(vec, width, height)
            for name, payload in ((npy_name, npy_bytes), (png_name, png_bytes)):
                built.append((name, payload, _row(
                    name, payload, item, glyph, short, match_dir.name, video_hash, measured, harvest_date,
                    qortime_commit,
                )))
    if _snapshot(match_dir) != before:
        raise Refuse("refusing: match folder changed while reading. no templates written.")
    doc = {
        "schema": "qorcap-template-provenance/v0",
        "harvest_script": HARVEST_SCRIPT,
        "harvest_version": HARVEST_VERSION,
        "qortime_commit": qortime_commit,
        "templates": [row for _name, _payload, row in built],
    }
    if dry_run:
        return doc
    body = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qorcap-harvest-", dir=str(out_dir.parent)) as tmp_name:
        tmp = Path(tmp_name)
        for name, payload, _row_doc in built:
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


def _print_plan(doc: dict, out_dir: Path) -> None:
    print(f"dry-run: {len(doc['templates'])} files would be written to {out_dir}; writing nothing")
    for row in doc["templates"]:
        span = []
        if "start_frame_index" in row:
            span.append(f"frames {row['start_frame_index']}-{row['end_frame_index']}")
        if "start_t_ms" in row:
            span.append(f"t_ms {row['start_t_ms']}-{row['end_t_ms']}")
        print(
            f"{row['path']} label={row['label']} role={row['role']} "
            f"frame={row['frame_index']} t_ms={row['t_ms']} {' '.join(span)}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest allowlisted per-glyph scorebug templates")
    ap.add_argument("--match-dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--labels", type=Path)
    root = _repo_root()
    ap.add_argument("--allowlist", type=Path, default=root / "templates" / "template_sources.json")
    ap.add_argument("--heldout", type=Path, default=root / "templates" / "heldout.json")
    ap.add_argument("--heldout-sha256", type=Path, default=root / "sidecar" / "packs" / "held_out_sha256.json")
    ap.add_argument("--windows-doc", action="store_true", help="print the PowerShell one-liner and write nothing")
    ap.add_argument("--dry-run", action="store_true", help="list the frames and crops and write nothing")
    ap.add_argument("--qortime", default=None, help="QorTime checkout (else $QORTIME, then the installed pin)")
    args = ap.parse_args(argv)
    if args.windows_doc:
        print(POWERSHELL_ONELINER)
        return 0
    if args.match_dir is None or args.out is None or args.labels is None:
        print("refusing: --match-dir, --labels, and --out are required. no templates written.", file=sys.stderr)
        print(POWERSHELL_ONELINER, file=sys.stderr)
        return 2
    try:
        doc = harvest(
            args.match_dir, args.out, args.labels, args.allowlist, args.heldout, args.heldout_sha256,
            dry_run=args.dry_run, qortime=args.qortime,
        )
    except Refuse as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        _print_plan(doc, args.out)
        return 0
    print(f"wrote {len(doc['templates'])} template files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
