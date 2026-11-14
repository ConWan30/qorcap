"""S2S body cue v0: runtime tests that need the optional deps and local model files (never committed).

Excluded from both CI sidecar suites (see tools/assert_pytest_skips.py EXCLUDED_TEST_FILES). Run locally:
  BODYCUE_MODELS=/path/models /path/venv/bin/python -m pytest -q tools/s2s_bodycue/test_bodycue_runtime.py
Fixture: matplotlib's bundled public sample photo grace_hopper.jpg (one person), so no gameplay frame is used.
"""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ort = pytest.importorskip("onnxruntime")
ov = pytest.importorskip("openvino")
mpl = pytest.importorskip("matplotlib")
from PIL import Image  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_person as dp  # noqa: E402

M = Path(os.environ.get("BODYCUE_MODELS", "/workspace/s2s-stage1/bodycue/models"))
YOLOX = M / "yolox_tiny.onnx"
RF_ONNX = M / "rfdetr_nano_onnx" / "rfdetr-nano.onnx"
RF_IR = M / "rfdetr_nano_ov" / "rfdetr-nano.xml"
if not (YOLOX.is_file() and RF_ONNX.is_file() and RF_IR.is_file()):
    pytest.skip(f"model files not under {M}", allow_module_level=True)


@pytest.fixture(scope="module")
def crop():
    p = Path(mpl.get_data_path()) / "sample_data" / "grace_hopper.jpg"
    im = Image.open(p).convert("RGB"); w, h = im.size; s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((384, 384), Image.BILINEAR)
    return np.ascontiguousarray(np.asarray(im)[:, :, ::-1])


def test_yolox_finds_the_person(crop):
    boxes, pmax, _ = dp.Runner("yolox", "onnxruntime", str(YOLOX))(crop, 0.3)
    assert boxes and pmax > 0.5


@pytest.mark.parametrize("runtime,model", [("onnxruntime", RF_ONNX), ("openvino", RF_IR)])
def test_rfdetr_finds_the_person(crop, runtime, model):
    boxes, pmax, _ = dp.Runner("rfdetr", runtime, str(model))(crop, 0.5)
    assert boxes and pmax > 0.5


def test_rfdetr_onnx_and_openvino_agree_above_threshold(crop):
    a, pa, _ = dp.Runner("rfdetr", "onnxruntime", str(RF_ONNX))(crop, 0.3)
    b, pb, _ = dp.Runner("rfdetr", "openvino", str(RF_IR))(crop, 0.3)
    assert len(a) == len(b) and abs(pa - pb) < 1e-3
    for x, y in zip(a, b):
        assert np.allclose(x["xyxy"], y["xyxy"], atol=0.5)
