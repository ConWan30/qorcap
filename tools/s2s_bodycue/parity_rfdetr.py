#!/usr/bin/env python3
"""RF-DETR Nano parity + CPU latency: PyTorch eager vs ONNX (onnxruntime CPU) vs OpenVINO IR fp32 (CPU).

  python parity_rfdetr.py --onnx rfdetr-nano.onnx --ir rfdetr-nano.xml --frames DIR --manifest manifest.json --out parity.json

Inputs are 384x384 reticle crops of the frames named in the manifest (tuning frames only; never a held-out split).
Per crop and runtime pair: max |diff| of boxes (cxcywh, 0..1) and of sigmoid scores over all 300x91 query/class
cells (query order can differ when two-stage top-k scores tie, so this per-query number is an upper bound),
order-free matched detections >= 0.3, per-query top-1 class agreement, and agreement of the highest-scoring (query, class). Latency is the
model call only (batch 1, after warm-up), median and p95, on this host. Optional deps: torch, rfdetr, onnxruntime,
openvino, Pillow.
"""
from __future__ import annotations

import argparse, json, sys, time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bodycue_core as core  # noqa: E402
from detect_person import cpu_name, read_bgr, sha256  # noqa: E402


def matched_dets(da, la, db, lb, thr: float = 0.3):
    """Order-free parity: every (query, class) cell >= thr in A is matched to the B query of the same class with the
    closest box (L-inf). Returns (n_dets_A, n_dets_B, max box diff, max score diff, n with no same-class B det >= thr/2)."""
    sa, sb = core.sigmoid(la).reshape(-1, la.shape[-1]), core.sigmoid(lb).reshape(-1, lb.shape[-1])
    ba, bb = da.reshape(-1, 4), db.reshape(-1, 4)
    qa, ca = np.where(sa >= thr); qb, cb = np.where(sb >= thr)
    bmax = smax = 0.0; miss = 0
    for q, c in zip(qa, ca):
        cand = np.where(sb[:, c] >= thr / 2)[0]
        if cand.size == 0:
            miss += 1; continue
        d = np.abs(bb[cand] - ba[q]).max(1); j = cand[d.argmin()]
        bmax = max(bmax, float(d.min())); smax = max(smax, float(abs(sb[j, c] - sa[q, c])))
    return len(qa), len(qb), bmax, smax, miss


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True); ap.add_argument("--ir", required=True)
    ap.add_argument("--frames", required=True); ap.add_argument("--manifest", required=True)
    ap.add_argument("--warmup", type=int, default=3); ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    import onnxruntime as ort, openvino as ov, torch
    from rfdetr import RFDETRNano
    m = RFDETRNano()
    net = m.model.model.eval()
    sess = ort.InferenceSession(a.onnx, providers=["CPUExecutionProvider"]); iname = sess.get_inputs()[0].name
    cm = ov.Core().compile_model(a.ir, "CPU", {"INFERENCE_PRECISION_HINT": "f32"}); req = cm.create_infer_request()

    def run_pt(x):
        with torch.no_grad():
            o = net(torch.from_numpy(x))
        return o["pred_boxes"].numpy(), o["pred_logits"].numpy()

    def run_ort(x):
        d, l = sess.run(None, {iname: x}); return d, l

    def run_ov(x):
        r = req.infer({0: x}); return r[cm.outputs[0]], r[cm.outputs[1]]

    runs = {"pytorch": run_pt, "onnx": run_ort, "openvino": run_ov}
    man = json.load(open(a.manifest))
    crops = []
    for fid in sorted(man["frames"]):
        src = man["frames"][fid]["src"]
        p = next(Path(a.frames) / f"{src}{e}" for e in (".jpg", ".png") if (Path(a.frames) / f"{src}{e}").is_file())
        c, _ = core.crop_reticle(read_bgr(p))
        crops.append((fid, core.rfdetr_preprocess(c[:, :, ::-1])))
    for f in runs.values():
        for _ in range(a.warmup):
            f(crops[0][1])
    lat = {k: [] for k in runs}; outs = {k: [] for k in runs}
    for _, x in crops:
        for k, f in runs.items():
            t0 = time.perf_counter(); o = f(x); lat[k].append((time.perf_counter() - t0) * 1e3); outs[k].append(o)
    pairs = [("pytorch", "onnx"), ("pytorch", "openvino"), ("onnx", "openvino")]
    res = {}
    for A, B in pairs:
        bd, sd, t1, top, pmax, md = [], [], [], [], [], []
        for (da, la), (db, lb) in zip(outs[A], outs[B]):
            bd.append(float(np.abs(da - db).max()))
            sa, sb = core.sigmoid(la), core.sigmoid(lb)
            sd.append(float(np.abs(sa - sb).max()))
            t1.append(float((core.top1_classes(la) == core.top1_classes(lb)).mean()))
            top.append(bool(np.unravel_index(sa.argmax(), sa.shape) == np.unravel_index(sb.argmax(), sb.shape)))
            pmax.append(abs(core.rfdetr_person_max(la) - core.rfdetr_person_max(lb)))
            md.append(matched_dets(da, la, db, lb))
        res[f"{A}_vs_{B}"] = {"n_crops": len(bd), "max_abs_box_diff": max(bd), "max_abs_score_diff": max(sd),
                              "per_query_top1_class_agreement_min": min(t1),
                              "per_query_top1_class_agreement_mean": float(np.mean(t1)),
                              "best_query_class_same_all_crops": all(top), "best_query_class_same_n": int(sum(top)),
                              "max_abs_person_max_diff": float(max(pmax)),
                              "dets_ge_0.3": {"n_A": int(sum(x[0] for x in md)), "n_B": int(sum(x[1] for x in md)),
                                              "max_abs_box_diff_matched": max(x[2] for x in md),
                                              "max_abs_score_diff_matched": max(x[3] for x in md),
                                              "unmatched": int(sum(x[4] for x in md))}}
    rep = {"schema": "s2s-bodycue-parity/v0", "cpu": cpu_name(), "torch_threads": torch.get_num_threads(),
           "onnx_sha256": sha256(a.onnx), "ir_xml_sha256": sha256(a.ir), "ir_bin_sha256": sha256(Path(a.ir).with_suffix(".bin")),
           "script_sha256": sha256(__file__), "parity": res,
           "latency_ms_model_call": {k: {"n": len(v), "median": round(core.percentile(v, 50), 2),
                                         "p95": round(core.percentile(v, 95), 2)} for k, v in lat.items()},
           "note": "batch 1, runtime-default threads, this host only; MyTurn latency UNMEASURED"}
    json.dump(rep, open(a.out, "w"), indent=1)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
