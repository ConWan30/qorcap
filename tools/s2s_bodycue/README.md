# S2S body cue v0 (zero-shot COCO person on the reticle crop)

Draft tooling for S2S Stage 1 phases A0/A1 (YOLOX-Tiny baseline, RF-DETR Nano). CPU only. Frames never leave the
machine. Model files and gameplay frames are **never committed**: `models.manifest.json` keeps the source URLs and
sha256s instead.

`enemy_flag` here means only "a COCO `person` box at or above the threshold inside the 384x384 crop centred on
the screen". Friend vs foe (red tag, teammate colour, minimap) is phase B, so every box is `foe_unknown`. It is not
an enemy-on-screen measure by itself, and no pass bar is claimed.

| File | Needs | What |
|---|---|---|
| `bodycue_core.py` | numpy | crop, YOLOX letterbox/decode/NMS, RF-DETR normalise/person boxes, output record |
| `detect_person.py` | onnxruntime or openvino, Pillow | run a model over manifest frames, write the `score_agreement.py detect` JSON plus per-crop latency |
| `export_rfdetr.py` | torch (CPU), rfdetr, onnx, openvino | RF-DETR Nano COCO -> ONNX (opset 17) -> OpenVINO IR fp32, hash manifest |
| `parity_rfdetr.py` | torch, rfdetr, onnxruntime, openvino | PyTorch vs ONNX vs OpenVINO outputs and latency on crops |
| `test_bodycue_core.py` | numpy, pytest | synthetic tests; on both CI sidecar lists |
| `test_bodycue_runtime.py` | the optional deps + local model files | public sample photo only; excluded from CI (reason in `tools/assert_pytest_skips.py`) |

## Optional deps (not installed by CI)

Use an isolated venv; nothing system-wide. `rfdetr` is the Apache-2.0 base package, never `rfdetr[plus]` (PML 1.0).

```
python -m venv venv-bodycue
venv-bodycue/bin/pip install --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple torch torchvision rfdetr openvino onnx onnxruntime
```

## Commands

```
curl -L -o models/yolox_tiny.onnx https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx
RF_HOME=models python export_rfdetr.py --out models --manifest models-rfdetr.json
python detect_person.py --kind yolox  --model models/yolox_tiny.onnx --frames RAW --manifest manifest.json --out det.json
python detect_person.py --kind rfdetr --model models/rfdetr_nano_onnx/rfdetr-nano.onnx --frames RAW --manifest manifest.json --out det.json
python parity_rfdetr.py --onnx models/rfdetr_nano_onnx/rfdetr-nano.onnx --ir models/rfdetr_nano_ov/rfdetr-nano.xml \
    --frames RAW --manifest manifest.json --out parity.json
BODYCUE_MODELS=models python -m pytest -q tools/s2s_bodycue/test_bodycue_runtime.py
```

Thresholds are fixed before any held-out run: YOLOX 0.30 (YOLOX demo default), RF-DETR 0.50 (rfdetr `predict`
default). OpenVINO runs with `INFERENCE_PRECISION_HINT=f32` because RF-DETR logits collapse in f16 (rfdetr docs).
Latency numbers are per host; MyTurn latency is UNMEASURED until measured there.
