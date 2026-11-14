#!/usr/bin/env python3
"""S2S body cue v0: zero-shot COCO person detector on reticle crops, onnxruntime or OpenVINO CPU.

  python detect_person.py --kind yolox  --model yolox_tiny.onnx  --frames DIR --manifest manifest.json --out det.json
  python detect_person.py --kind rfdetr --model rfdetr-nano.onnx --frames DIR --manifest manifest.json --out det.json
  python detect_person.py --kind rfdetr --runtime openvino --model rfdetr-nano.xml ...

--frames holds <src>.jpg|.png named by manifest["frames"][fid]["src"]; every manifest frame is run.
Output JSON follows the score_agreement.py `detect` contract:
  {"schema", "detector": {...}, "frames": [{"frame_id", "enemy_flag", "boxes", "foe", ...}], "latency_ms": {...}}
enemy_flag here means "a COCO person box >= --thr in the 384 reticle crop"; friend/foe fusion is phase B, so
foe is always "foe_unknown". Optional deps: onnxruntime (or openvino), Pillow. CPU only. Frames never leave the box.
"""
from __future__ import annotations

import argparse, hashlib, json, os, platform, sys, time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bodycue_core as core  # noqa: E402

VERSION = "s2s1-bodycue/0.1-draft"
DEFAULT_THR = {"yolox": 0.30, "rfdetr": 0.50}  # pre-registered: YOLOX demo default / rfdetr predict default


def sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_bgr(path) -> np.ndarray:
    """Decode with Pillow (RGB) and return BGR uint8, the channel order YOLOX was trained on."""
    from PIL import Image
    with Image.open(path) as im:
        return np.ascontiguousarray(np.asarray(im.convert("RGB"))[:, :, ::-1])


def cpu_name() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "UNKNOWN"


class Runner:
    def __init__(self, kind: str, runtime: str, model: str, threads: int = 0):
        self.kind, self.runtime = kind, runtime
        if runtime == "onnxruntime":
            import onnxruntime as ort
            so = ort.SessionOptions()
            if threads:
                so.intra_op_num_threads = threads
            self.sess = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
            self.inp = self.sess.get_inputs()[0].name
            self.version = f"onnxruntime {ort.__version__}"
            self._run = lambda x: self.sess.run(None, {self.inp: x})
        elif runtime == "openvino":
            import openvino as ov
            core_ = ov.Core()
            cfg = {"INFERENCE_PRECISION_HINT": "f32"}  # RF-DETR logits collapse in f16 (rfdetr OpenVINO docs)
            if threads:
                cfg["INFERENCE_NUM_THREADS"] = threads
            self.cm = core_.compile_model(model, "CPU", cfg)
            self.req = self.cm.create_infer_request()
            self.version = f"openvino {ov.__version__}"
            outs = self.cm.outputs

            def _run(x):
                r = self.req.infer({0: x})
                return [r[o] for o in outs]
            self._run = _run
        else:
            raise ValueError(runtime)

    def __call__(self, bgr_crop: np.ndarray, thr: float):
        t0 = time.perf_counter()
        if self.kind == "yolox":
            x, r = core.yolox_preprocess(bgr_crop)
        else:
            x = core.rfdetr_preprocess(bgr_crop[:, :, ::-1])
        t1 = time.perf_counter()
        outs = self._run(x)
        t2 = time.perf_counter()
        if self.kind == "yolox":
            dec = core.yolox_decode(outs[0])
            boxes = core.yolox_person_boxes(dec, r, thr)
            pmax = core.yolox_person_max(dec)
        else:
            boxes = core.rfdetr_person_boxes(outs[0], outs[1], thr)
            pmax = core.rfdetr_person_max(outs[1])
        t3 = time.perf_counter()
        return boxes, pmax, {"pre": (t1 - t0) * 1e3, "infer": (t2 - t1) * 1e3, "post": (t3 - t2) * 1e3,
                             "total": (t3 - t0) * 1e3}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["yolox", "rfdetr"], required=True)
    ap.add_argument("--runtime", choices=["onnxruntime", "openvino"], default="onnxruntime")
    ap.add_argument("--model", required=True)
    ap.add_argument("--frames", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--crop", choices=["reticle384"], default="reticle384")
    ap.add_argument("--classes", choices=["person"], default="person")
    ap.add_argument("--thr", type=float, default=None)
    ap.add_argument("--threads", type=int, default=0, help="0 = runtime default")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    thr = DEFAULT_THR[a.kind] if a.thr is None else a.thr
    man = json.load(open(a.manifest))
    run = Runner(a.kind, a.runtime, a.model, a.threads)
    items = []
    for fid in sorted(man["frames"]):
        src = man["frames"][fid]["src"]
        p = next((Path(a.frames) / f"{src}{e}" for e in (".jpg", ".png") if (Path(a.frames) / f"{src}{e}").is_file()), None)
        if p is None:
            raise SystemExit(f"missing frame for {fid}")
        items.append((fid, p))
    first = read_bgr(items[0][1])
    c0, _ = core.crop_reticle(first)
    for _ in range(a.warmup):
        run(c0, thr)
    frames, tot, inf = [], [], []
    for fid, p in items:
        img = read_bgr(p)
        crop, box = core.crop_reticle(img)
        boxes, pmax, ms = run(crop, thr)
        tot.append(ms["total"]); inf.append(ms["infer"])
        frames.append(core.frame_record(fid, boxes, pmax, box, {k: round(v, 3) for k, v in ms.items()}))
    lat = {"n": len(tot), "cpu": cpu_name(), "logical_cpus": os.cpu_count(), "threads": a.threads or "runtime default",
           "warmup": a.warmup, "batch": 1,
           "total_median": round(core.percentile(tot, 50), 2), "total_p95": round(core.percentile(tot, 95), 2),
           "infer_median": round(core.percentile(inf, 50), 2), "infer_p95": round(core.percentile(inf, 95), 2),
           "note": "per crop, excludes image decode; this host only. MyTurn latency UNMEASURED."}
    out = {"schema": core.SCHEMA,
           "detector": {"name": f"coco_person_zeroshot_{a.kind}", "version": VERSION, "kind": a.kind,
                        "runtime": run.version, "model_sha256": sha256(a.model), "model_file": os.path.basename(a.model),
                        "script_sha256": sha256(__file__), "core_sha256": sha256(core.__file__),
                        "params": {"crop": a.crop, "crop_size": core.CROP, "classes": [a.classes], "thr": thr,
                                   "enemy_flag": "any person box >= thr in crop", "foe": "foe_unknown (phase B)"}},
           "latency_ms": lat, "frames": frames}
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps({"n": len(frames), "flags": sum(f["enemy_flag"] for f in frames), "latency_ms": lat}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
