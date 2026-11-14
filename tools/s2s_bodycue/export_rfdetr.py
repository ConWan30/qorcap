#!/usr/bin/env python3
"""Export RF-DETR Nano (pretrained COCO, Apache-2.0, `pip install rfdetr`, never rfdetr[plus]) to ONNX, then
ONNX -> OpenVINO IR fp32. Writes a hash manifest; model files are never committed.

  RF_HOME=/path/models python export_rfdetr.py --out /path/models --manifest models.json

Optional deps: torch (CPU wheel), rfdetr, onnx, openvino. CPU only.
"""
from __future__ import annotations

import argparse, hashlib, importlib.metadata, json, os, sys
from pathlib import Path


def sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--opset", type=int, default=17)
    a = ap.parse_args(argv)
    import openvino as ov, rfdetr, torch, onnx
    from rfdetr import RFDETRNano
    from rfdetr.assets.model_weights import ModelWeights
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    m = RFDETRNano()  # downloads to $RF_HOME (MD5-checked by rfdetr)
    pth = Path(str(m.model_config.pretrain_weights))
    asset = ModelWeights.RF_DETR_NANO
    onnx_p = Path(m.export(output_dir=str(out / "rfdetr_nano_onnx"), format="onnx", opset_version=a.opset, verbose=False))
    onnx.checker.check_model(str(onnx_p))
    ir_p = out / "rfdetr_nano_ov" / "rfdetr-nano.xml"
    ov.save_model(ov.convert_model(str(onnx_p)), str(ir_p), compress_to_fp16=False)
    rec = {"schema": "s2s-bodycue-models/v0",
           "rfdetr_nano_pth": {"url": asset.url, "md5_expected_by_rfdetr": asset.md5_hash, "sha256": sha256(pth),
                               "bytes": pth.stat().st_size, "license": "Apache-2.0 (rfdetr base package)"},
           "rfdetr_nano_onnx": {"sha256": sha256(onnx_p), "bytes": onnx_p.stat().st_size, "opset": a.opset,
                                "io": "input [1,3,384,384] RGB ImageNet-normalised; dets [1,300,4] cxcywh 0..1; labels [1,300,91] logits"},
           "rfdetr_nano_ir": {"xml_sha256": sha256(ir_p), "bin_sha256": sha256(ir_p.with_suffix(".bin")),
                              "precision": "fp32 (compress_to_fp16=False)"},
           "versions": {"rfdetr": importlib.metadata.version("rfdetr"),
                        "torch": torch.__version__, "onnx": onnx.__version__, "openvino": ov.__version__}}
    json.dump(rec, open(a.manifest, "w"), indent=1)
    print(json.dumps(rec, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
