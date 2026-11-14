#!/usr/bin/env python3
"""Export RF-DETR Nano (pretrained COCO, Apache-2.0, `pip install rfdetr`, never rfdetr[plus]) to ONNX, then
ONNX -> OpenVINO IR fp32. Writes a hash manifest; model files are never committed.

  python export_rfdetr.py --out /path/models --manifest models.json

The .pth is fetched only if absent, then checked against the committed models.manifest.json sha256 before it is
loaded (fail closed). OpenVINO telemetry must be opted out first (`opt_in_out --opt_out`), or the script refuses.
A fresh export's ONNX/IR hashes are reported against the committed pins, not required to match them.
Optional deps: torch (CPU wheel), rfdetr, onnx, openvino. CPU only.
"""
from __future__ import annotations

import argparse, hashlib, importlib.metadata, json, os, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pins  # noqa: E402


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
    pins.require_openvino_telemetry_opt_out()  # P173-L3: refuse before any conversion call
    pinned = pins.load_manifest()["rfdetr_nano_pth"]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    pth = out / "rf-detr-nano.pth"
    if not pth.is_file():
        # P173-L2: the Roboflow URL is a mutable product path, so the committed sha256 is the real pin.
        urllib.request.urlretrieve(pinned["url"], pth)
    man_ = pins.load_manifest()
    import openvino as ov, rfdetr, torch, onnx
    from rfdetr import RFDETRNano
    from rfdetr.assets.model_weights import ModelWeights
    # P173-L1 / P174-L1: the role-bound pin is checked on the bytes that rfdetr then loads (private checked copy).
    with pins.pinned_copy(pth, "rfdetr_pth", man_, name="rf-detr-nano.pth") as vpth:
        m = RFDETRNano(pretrain_weights=str(vpth))
    asset = ModelWeights.RF_DETR_NANO
    onnx_p = Path(m.export(output_dir=str(out / "rfdetr_nano_onnx"), format="onnx", opset_version=a.opset, verbose=False))
    onnx.checker.check_model(str(onnx_p))
    ir_p = out / "rfdetr_nano_ov" / "rfdetr-nano.xml"
    ov.save_model(ov.convert_model(str(onnx_p)), str(ir_p), compress_to_fp16=False)
    rec = {"schema": "s2s-bodycue-models/v0",
           "rfdetr_nano_pth": {"url": asset.url, "md5_expected_by_rfdetr": asset.md5_hash, "sha256": pins.role_hash(man_, "rfdetr_pth"),
                               "bytes": pth.stat().st_size, "license": "Apache-2.0 (rfdetr base package)"},
           "rfdetr_nano_onnx": {"sha256": sha256(onnx_p), "bytes": onnx_p.stat().st_size, "opset": a.opset,
                                "io": "input [1,3,384,384] RGB ImageNet-normalised; dets [1,300,4] cxcywh 0..1; labels [1,300,91] logits"},
           "rfdetr_nano_ir": {"xml_sha256": sha256(ir_p), "bin_sha256": sha256(ir_p.with_suffix(".bin")),
                              "precision": "fp32 (compress_to_fp16=False)"},
           "versions": {"rfdetr": importlib.metadata.version("rfdetr"),
                        "torch": torch.__version__, "onnx": onnx.__version__, "openvino": ov.__version__}}
    rec["matches_committed_manifest"] = {
        "onnx": rec["rfdetr_nano_onnx"]["sha256"] == pins.role_hash(man_, "rfdetr_onnx"),
        "ir": (rec["rfdetr_nano_ir"]["xml_sha256"], rec["rfdetr_nano_ir"]["bin_sha256"])
              == (pins.role_hash(man_, "ov_xml"), pins.role_hash(man_, "ov_bin"))}
    json.dump(rec, open(a.manifest, "w"), indent=1)
    print(json.dumps(rec, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
