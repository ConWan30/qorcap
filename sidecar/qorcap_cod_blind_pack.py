"""T-0062 sealed-session blind label pack builder (info_only, no inference).

Builds a Con-facing blind H/N/S label pack for one CoD practice match folder:

* every candidate window that passes the same external ammo-decrease gate as
  ``qorcap_cod_shadow.run_shadow_on_match`` (``0 < prev_mag - mag <=
  MAX_AMMO_DROP_PER_STEP`` inside an R2 hold span, L1/R1 equipment spans
  skipped, one window per anchor frame, ``[t_ammo .. t_ammo + 8]``);
* strips rendered with ``render_composite_strip`` and no header
  (headless ``892x304`` PNG), from the same center 96x96 crops; the ammo
  corner is read only as the gate / ``t_ammo`` timing anchor and never
  becomes strip or model input;
* a seeded shuffle of the Con-facing order, so strip ids ``s001..`` carry no
  session, frame or time order;
* a hidden key (``sealed_key/key.json``, outside the served folder) mapping
  strip id -> session / frame / ``t_ammo_ms`` for later unsealing;
* a deterministic SHA-256 hash manifest of every emitted file.

This tool never loads a model and never scores. Any model, metrics or
threshold argument makes it exit non-zero. Scoring happens later, after Con's
labels are in, as a separate QorVolution step.

Output layout under ``--out``::

    review/            served on 127.0.0.1 only (review.html, manifest.json, strips/sNNN.png)
    sealed_key/        key.json (never served)
    labels/            labels.json written by the loopback server (never served)
    hash_manifest.json path, size, sha256 of every file in review/ and sealed_key/
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import os
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import qorcap_cod_shadow as shadow  # noqa: E402

SCHEMA_PACK = "qor.blind_label_pack.v0"
SCHEMA_KEY = "qor.blind_label_key.v0"
SCHEMA_HASHES = "qor.blind_label_hashes.v0"
REVIEW_DIR = "review"
KEY_DIR = "sealed_key"
LABELS_DIR = "labels"
HASH_MANIFEST = "hash_manifest.json"
STRIP_W = 892
STRIP_H = 304

# Anything that names a model output, threshold, tier, triage reason or source
# position must never reach the served folder. Word-boundary, case-insensitive.
LEAK_TOKENS = (
    "confidence", "bps", "p_cal", "pcal", "logit", "logits", "tau", "verdict",
    "model", "model_id", "mnv4", "onnx", "score", "scores", "pred", "prediction",
    "predictions", "prob", "probability", "source_tier", "tier", "triage",
    "triage_reason", "frame_idx", "t_ammo", "t_ammo_ms", "session_id",
    "event_id", "reader_199", "dataset_split",
)
_LEAK_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t in LEAK_TOKENS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Session-shaped strings: ``match-<digits>`` and ``YYYYMMDD-HHMMSS`` stamps.
_SESSION_RE = re.compile(r"match-\d+|(?<!\d)\d{8}-\d{6}(?!\d)", re.IGNORECASE)
REFUSED_FLAGS = (
    "--model", "--model-id", "--metrics", "--onnx", "--pt", "--weights",
    "--checkpoint", "--temperature", "--tau", "--tau-low-bps", "--tau-high-bps",
    "--score", "--infer",
)


class BlindPackError(Exception):
    pass


class InferenceRefusedError(BlindPackError):
    pass


def refuse_inference_args(argv) -> None:
    for a in argv or []:
        s = str(a)
        flag = s.split("=", 1)[0].lower()
        if flag in REFUSED_FLAGS or s.lower().endswith((".onnx", ".pt", ".pth")):
            raise InferenceRefusedError(
                f"blind pack builder never runs inference; refusing argument {flag!r}"
            )


# ----------------------------------------------------------------- gated windows (same gate as shadow)
def _video_frames(video_path: Path):
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise BlindPackError(f"cannot open video {video_path}")
    try:
        while cap.grab():
            ok, frame = cap.retrieve()
            yield frame if ok else None
    finally:
        cap.release()


def iter_gated_windows(match_dir: Path, *, inputs_path: Path | None = None, frames=None):
    """Yield ``{frame_idx, t_ammo_ms, crops}`` for every non-equipment ammo-gated window.

    Uses ``qorcap_cod_shadow.AmmoGatedWindowBuilder``, the same window builder and gate as
    ``run_shadow_on_match``, without #199 / XP / killfeed reads and without inference.
    ``frames`` (an iterable of BGR frames) replaces ``video.mp4`` for in-memory use.
    """
    from qorcap_cod_post_nov21 import read_ammo_counter

    match_dir = Path(match_dir)
    inputs_info = shadow.parse_match_inputs(match_dir, inputs_path=inputs_path)
    frame_pairs = shadow.parse_match_frame_clock(match_dir)
    if frames is None:
        video_path = match_dir / shadow.FRAME_FILE
        if not video_path.is_file():
            raise BlindPackError(f"missing {shadow.FRAME_FILE} in {match_dir}")
        frames = _video_frames(video_path)
    windows = shadow.AmmoGatedWindowBuilder(frame_pairs, inputs_info["r2_spans"])
    for frame_idx, frame in enumerate(frames):
        if frame is None:
            continue
        crop, _t_us, t_ammo_ms, span = windows.prepare(frame_idx, frame)
        for w in windows.feed(crop):
            yield {"frame_idx": w["frame_idx"], "t_ammo_ms": int(w["t_ammo_ms"]), "crops": w["crops"]}
        ammo_res = read_ammo_counter(frame)
        mag = ammo_res.get("mag") if ammo_res.get("ok") else None
        windows.gate(frame_idx, int(t_ammo_ms), span, mag, crop)


# ----------------------------------------------------------------- shuffle, strips, manifests
def shuffled_order(n: int, seed: int) -> list[int]:
    order = list(range(n))
    random.Random(int(seed)).shuffle(order)
    return order


def strip_id(i: int) -> str:
    return f"s{i + 1:03d}"


def _render_strip(tensor, luma, out_png: Path) -> None:
    shadow.render_composite_strip([], tensor, luma, out_png, header_text="")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_hash_manifest(root: Path) -> dict:
    root = Path(root)
    files = []
    for sub in (REVIEW_DIR, KEY_DIR):
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                data = p.read_bytes()
                files.append({
                    "path": p.relative_to(root).as_posix(),
                    "size": len(data),
                    "sha256": _sha256_bytes(data),
                })
    files.sort(key=lambda f: f["path"])
    manifest = {"schema": SCHEMA_HASHES, "files": files}
    shadow._atomic_write_text(root / HASH_MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _render_blind_html(title: str, strip_ids: list[str]) -> str:
    items_json = json.dumps([{"id": s, "png": f"strips/{s}.png"} for s in strip_ids], indent=2)
    t = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{t}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #111317; color: #e8ecf2; margin: 20px; }}
  .bar {{ display: flex; justify-content: space-between; padding: 12px 16px; background: #1a1e26; border-radius: 8px; margin-bottom: 16px; }}
  .card {{ background: #1a1e26; border: 1px solid #2b3240; border-radius: 8px; padding: 14px; margin-bottom: 14px; }}
  .card.active {{ border-color: #60a5fa; }}
  img.strip {{ max-width: 100%; display: block; margin: 8px 0; }}
  button {{ margin-right: 8px; padding: 6px 12px; border: 0; border-radius: 6px; cursor: pointer; font-weight: 600; color: #fff; }}
  .h {{ background: #16a34a; }} .n {{ background: #dc2626; }} .s {{ background: #4b5563; }}
</style>
</head>
<body>
<div class="bar"><div><strong>{t}</strong> &middot; Info only &middot; Blind</div>
<div>Hotkeys: <strong>H</strong>=hit &middot; <strong>N</strong>=no hit &middot; <strong>S</strong>=skip &middot; <span id="done">0</span>/<span id="total">0</span></div></div>
<div id="list"></div>
<script>
const ITEMS = {items_json};
const LBL = {{}};
let idx = 0;
async function sendLabel(id, key) {{
  try {{
    const r = await fetch('/label', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{id, key}})}});
    if (!r.ok) return;
  }} catch (_) {{ return; }}
  LBL[id] = key;
  if (idx + 1 < ITEMS.length) idx += 1;
  render();
}}
function render() {{
  const root = document.getElementById('list');
  root.innerHTML = '';
  document.getElementById('done').textContent = Object.keys(LBL).length;
  document.getElementById('total').textContent = ITEMS.length;
  ITEMS.forEach((it, i) => {{
    const d = document.createElement('div');
    d.className = 'card' + (i === idx ? ' active' : '');
    d.innerHTML = `<div>${{it.id}} &middot; ${{LBL[it.id] || '-'}}</div>
      <img class="strip" src="${{it.png}}" alt="strip">
      <button class="h" onclick="sendLabel('${{it.id}}','H')">[H] hit</button>
      <button class="n" onclick="sendLabel('${{it.id}}','N')">[N] no hit</button>
      <button class="s" onclick="sendLabel('${{it.id}}','S')">[S] skip</button>`;
    d.onclick = () => {{ idx = i; }};
    root.appendChild(d);
  }});
}}
document.addEventListener('keydown', (e) => {{
  const k = e.key.toUpperCase();
  if (['H', 'N', 'S'].includes(k) && ITEMS[idx]) sendLabel(ITEMS[idx].id, k);
}});
render();
</script>
</body>
</html>
"""


def _write_review(out_root: Path, title: str, windows: list[dict], key_extra: dict) -> dict:
    """``windows`` are in source order, each with ``tensor``, ``luma`` and key fields."""
    out_root = Path(out_root)
    review = out_root / REVIEW_DIR
    if review.exists() and any(review.iterdir()):
        raise BlindPackError(f"refusing to overwrite non-empty review folder: {review}")
    strips = review / "strips"
    strips.mkdir(parents=True, exist_ok=True)
    seed = int(key_extra["seed"])
    order = shuffled_order(len(windows), seed)
    key_items = []
    ids = []
    for pos, src in enumerate(order):
        sid = strip_id(pos)
        w = windows[src]
        _render_strip(w["tensor"], w["luma"], strips / f"{sid}.png")
        ids.append(sid)
        key_items.append({"id": sid, **{k: v for k, v in w.items() if k not in ("tensor", "luma")}})
    manifest = {"schema": SCHEMA_PACK, "info_only": True, "count": len(ids),
                "items": [{"id": s, "png": f"strips/{s}.png"} for s in ids]}
    shadow._atomic_write_text(review / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    shadow._atomic_write_text(review / "review.html", _render_blind_html(title, ids))
    key = {"schema": SCHEMA_KEY, "info_only": True, **{k: v for k, v in key_extra.items()},
           "count": len(ids), "items": key_items}
    shadow._atomic_write_text(out_root / KEY_DIR / "key.json", json.dumps(key, indent=2, sort_keys=True) + "\n")
    hashes = write_hash_manifest(out_root)
    hits = scan_for_leaks(review)
    if hits:
        raise BlindPackError(f"unblinding strings in served folder: {hits}")
    return {"review_dir": str(review), "count": len(ids), "seed": seed,
            "hash_manifest": str(out_root / HASH_MANIFEST), "files": len(hashes["files"])}


def build_session_pack(match_dir: Path, out_root: Path, *, seed: int, inputs_path: Path | None = None,
                       frames=None) -> dict:
    match_dir = Path(match_dir)
    session = shadow.require_safe_leaf(match_dir.name)
    out_root = Path(out_root)
    shadow._ensure_out_outside_match(out_root, match_dir)
    windows = []
    for w in iter_gated_windows(match_dir, inputs_path=inputs_path, frames=frames):
        tensor, luma = shadow.build_tensor_and_luma_from_center_crops(w["crops"])
        windows.append({"session": session, "frame": w["frame_idx"], "t_ammo_ms": w["t_ammo_ms"],
                        "tensor": tensor, "luma": luma})
    if not windows:
        raise BlindPackError(f"no ammo-gated windows in {match_dir}")
    return _write_review(out_root, "QorCap blind label pack", windows,
                         {"mode": "session", "seed": int(seed), "session": session})


def build_single_row_pack(npz_path: Path, row: int, out_root: Path, *, seed: int = 0,
                          windows_key: str = "windows") -> dict:
    """Info-only blind strip for one stored window row (no relabel, no retrain).

    Reads only ``windows_key`` (``(N, 9, H, W, 3)`` BGR center crops, H/W >= 96, as in the
    ``train_windows_v2_*.npz`` files) and rebuilds the tensor/luma the same way as the F3
    hygiene builder. Label, tier and score arrays in the npz are never read.
    """
    import numpy as np

    with np.load(Path(npz_path), allow_pickle=False) as z:
        if windows_key not in z.files:
            raise BlindPackError(f"npz lacks {windows_key!r}; available arrays: {sorted(z.files)}")
        arr = z[windows_key]
        n = int(arr.shape[0])
        if not 0 <= int(row) < n:
            raise BlindPackError(f"row {row} out of range 0..{n - 1}")
        win = np.asarray(arr[int(row)])
    if win.ndim != 4 or win.shape[0] != shadow.WINDOW_FRAMES or win.shape[3] != 3:
        raise BlindPackError(f"unexpected window shape {win.shape}; expected (9, H, W, 3)")
    frames = [win[t] for t in range(shadow.WINDOW_FRAMES)]
    tensor = shadow.build_mnv4_v2_tensor(frames, crop_size=shadow.CROP_SIZE, dx=0, dy=0, tol_px=shadow.TOL_PX)
    luma = shadow.extract_center_luma_crops(frames, crop_size=shadow.CROP_SIZE, dx=0, dy=0)
    windows = [{"npz": Path(npz_path).name, "row": int(row), "tensor": tensor, "luma": luma}]
    return _write_review(Path(out_root), "QorCap blind single-row look", windows,
                         {"mode": "single_row", "seed": int(seed)})


# ----------------------------------------------------------------- leak scan
def _png_text_chunks(data: bytes) -> list[bytes]:
    out = []
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return [data]
    i = 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        if ctype not in (b"IDAT", b"IHDR", b"IEND", b"PLTE"):
            out.append(ctype + data[i + 8:i + 8 + length])
        i += 12 + length
    return out


def scan_for_leaks(review_dir: Path) -> list[str]:
    review_dir = Path(review_dir)
    hits = []
    for p in sorted(review_dir.rglob("*")):
        rel = p.relative_to(review_dir).as_posix()
        for m in _LEAK_RE.finditer(rel):
            hits.append(f"{rel}: name:{m.group(1)}")
        for m in _SESSION_RE.finditer(rel):
            hits.append(f"{rel}: name:session:{m.group(0)}")
        if not p.is_file():
            continue
        data = p.read_bytes()
        texts = _png_text_chunks(data) if p.suffix.lower() == ".png" else [data]
        for t in texts:
            txt = t.decode("utf-8", errors="replace")
            for m in _LEAK_RE.finditer(txt):
                hits.append(f"{rel}: {m.group(1)}")
            for m in _SESSION_RE.finditer(txt):
                hits.append(f"{rel}: session:{m.group(0)}")
    return sorted(set(hits))


# ----------------------------------------------------------------- loopback label server
def write_label(out_root: Path, sid: str, key: str) -> str:
    k = str(key).strip().upper()
    if k not in ("H", "N", "S"):
        raise ValueError(f"invalid key {key!r}; expected H, N, or S")
    if not re.fullmatch(r"s\d{3,}", str(sid)):
        raise ValueError(f"invalid strip id {sid!r}")
    review_manifest = json.loads((Path(out_root) / REVIEW_DIR / "manifest.json").read_text(encoding="utf-8"))
    if sid not in {it["id"] for it in review_manifest["items"]}:
        raise KeyError(sid)
    path = Path(out_root) / LABELS_DIR / "labels.json"
    labels = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    labels[sid] = shadow.KEY_TO_HUMAN_LABEL[k]
    shadow._atomic_write_text(path, json.dumps(labels, indent=2, sort_keys=True) + "\n")
    return labels[sid]


def serve_blind_pack(out_root: Path, *, port: int = 0) -> http.server.ThreadingHTTPServer:
    """Serve ``review/`` on 127.0.0.1 only; labels go to ``labels/`` outside the served folder."""
    out_root = Path(out_root).resolve()
    review = out_root / REVIEW_DIR

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(review), **kw)

        def do_POST(self):
            if self.path != "/label":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                write_label(out_root, body["id"], body["key"])
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            resp = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format, *args):
            return

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)


# ----------------------------------------------------------------- CLI
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        refuse_inference_args(argv)
    except InferenceRefusedError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="QorCap blind label pack builder (info_only, no inference)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_s = sub.add_parser("session", help="Blind pack of every ammo-gated window in one match folder")
    p_s.add_argument("--match-dir", required=True)
    p_s.add_argument("--out", required=True)
    p_s.add_argument("--seed", type=int, required=True)
    p_s.add_argument("--inputs")
    p_r = sub.add_parser("row", help="Info-only blind strip for one stored window row")
    p_r.add_argument("--npz", required=True)
    p_r.add_argument("--row", type=int, required=True)
    p_r.add_argument("--out", required=True)
    p_r.add_argument("--seed", type=int, default=0)
    p_r.add_argument("--windows-key", default="windows")
    p_v = sub.add_parser("serve", help="Serve review/ on 127.0.0.1 for H/N/S labels")
    p_v.add_argument("--out", required=True)
    p_v.add_argument("--port", type=int, default=8780)
    args = ap.parse_args(argv)

    if args.cmd == "session":
        res = build_session_pack(Path(args.match_dir), Path(args.out), seed=args.seed,
                                 inputs_path=Path(args.inputs) if args.inputs else None)
    elif args.cmd == "row":
        res = build_single_row_pack(Path(args.npz), args.row, Path(args.out), seed=args.seed,
                                    windows_key=args.windows_key)
    else:
        srv = serve_blind_pack(Path(args.out), port=args.port)
        print(f"Serving blind pack at http://127.0.0.1:{srv.server_address[1]}/review.html")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0
    print(json.dumps({"count": res["count"], "seed": res["seed"], "files": res["files"],
                      "review_dir": res["review_dir"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
