#!/usr/bin/env python3
"""S2S body cue fusion (phase B) CLI: v0.2 cue JSON + YOLOX and RF-DETR person JSONs + frames -> yes/no/unknown.

  python fuse.py --params FROZEN-fusion-params.json [--params-sha256 <hex>] \\
      --v02 v02_fid.json --yolox yolox.json --rfdetr rfdetr.json --frames RAW --manifest manifest.json --out fused.json

Fails closed if the params file's sha256 differs from the published frozen_pin constant (or --params-sha256),
if it has duplicate keys, if its params differ from fusion_core.DEFAULT_PARAMS, if a person JSON was run with a
threshold above the frozen one (boxes would be missing), or if any frame is missing from an input. A v0.2 record
with a missing/None/wrong-type/non-finite/negative field is reported as malformed (stderr, per-frame "malformed",
detector.malformed_frames) and decided `unknown`. Output follows the score_agreement.py `detect` contract
(frame_id, enemy_flag = decision == "yes") plus decision, reasons and per-person evidence. No pixels are written.
Needs numpy and Pillow (or OpenCV) to read frames. CPU only, no network.
"""
from __future__ import annotations

import argparse, hashlib, json, sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as F  # noqa: E402
import pins  # noqa: E402
from frozen_pin import FROZEN_PARAMS_SHA256  # noqa: E402

SCHEMA = "s2s-stage1-detector/v0"


def sha256(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def read_bgr(path) -> np.ndarray:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return np.ascontiguousarray(np.asarray(im.convert("RGB"))[:, :, ::-1])
    except ImportError:
        import cv2
        img = cv2.imread(str(path))
        if img is None:
            raise SystemExit(f"unreadable frame {Path(path).name}")
        return img


def load_params(path, expect_sha=None):
    """P174-L2: one read; the bytes must hash to the published frozen_pin.FROZEN_PARAMS_SHA256 (and to
    --params-sha256 if given); the same bytes are parsed (duplicate keys refused); params must equal DEFAULT_PARAMS."""
    b = Path(path).read_bytes()
    got = hashlib.sha256(b).hexdigest()
    if got != FROZEN_PARAMS_SHA256:
        raise SystemExit(f"params sha256 {got[:12]}… != published {FROZEN_PARAMS_SHA256[:12]}…; refusing")
    if expect_sha is not None and got != expect_sha:
        raise SystemExit(f"params sha256 {got[:12]}… != --params-sha256 {expect_sha[:12]}…; refusing")
    fz = pins.parse_json_bytes(b)
    if fz["params"] != F.DEFAULT_PARAMS:
        raise SystemExit("frozen params differ from fusion_core.DEFAULT_PARAMS; refusing")
    return fz, got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    for k in ("params", "v02", "yolox", "rfdetr", "frames", "manifest", "out"):
        ap.add_argument(f"--{k}", required=True)
    ap.add_argument("--params-sha256", default=None, help="optional; must equal the published constant")
    a = ap.parse_args(argv)
    fz, params_sha = load_params(a.params, a.params_sha256)
    P = fz["params"]
    man = json.load(open(a.manifest))["frames"]
    V = {f["frame_id"]: f for f in json.load(open(a.v02))["frames"]}
    dets = {}
    for kind, p in (("yolox", a.yolox), ("rfdetr", a.rfdetr)):
        D = json.load(open(p))
        thr = D["detector"]["params"]["thr"]
        if thr > P["person_thr"][kind] + 1e-9:
            raise SystemExit(f"{kind} boxes were cut at {thr} > frozen {P['person_thr'][kind]}; refusing")
        dets[kind] = {f["frame_id"]: f for f in D["frames"]}
    frames = []
    for fid in sorted(man):
        if fid not in V or fid not in dets["yolox"] or fid not in dets["rfdetr"]:
            raise SystemExit(f"frame {fid} missing from an input; refusing")
        src = man[fid]["src"]
        p = next((Path(a.frames) / f"{src}{e}" for e in (".jpg", ".png") if (Path(a.frames) / f"{src}{e}").is_file()), None)
        if p is None:
            raise SystemExit(f"missing frame for {fid}")
        hsv = F.bgr_to_hsv(read_bgr(p))
        persons = F.merge_persons(dets["yolox"][fid]["boxes"], dets["rfdetr"][fid]["boxes"], P)
        r = F.decide(V[fid], persons, hsv, P)
        frames.append({"frame_id": fid, **r, "foe": "enemy" if r["decision"] == "yes" else "foe_unknown"})
        if r["malformed"]:
            print(f"MALFORMED v0.2 record {fid}: {', '.join(r['malformed'])} -> unknown", file=sys.stderr)
    out = {"schema": SCHEMA,
           "detector": {"name": "enemy_on_screen_fusion", "version": P["version"],
                        "params_sha256": params_sha, "fusion_core_sha256": sha256(F.__file__),
                        "script_sha256": sha256(__file__),
                        "inputs": {"v02_sha256": sha256(a.v02), "yolox_sha256": sha256(a.yolox), "rfdetr_sha256": sha256(a.rfdetr)},
                        "malformed_frames": sorted(f["frame_id"] for f in frames if f["malformed"]),
                        "absence_rule": "no counted cue -> unknown, never no; 'no' needs friend-coloured persons only, "
                                        "no tag and zero minimap dots"},
           "frames": frames}
    json.dump(out, open(a.out, "w"), indent=1)
    c = {k: sum(f["decision"] == k for f in frames) for k in ("yes", "no", "unknown")}
    print(json.dumps({"n": len(frames), **c, "malformed": sum(bool(f["malformed"]) for f in frames)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
