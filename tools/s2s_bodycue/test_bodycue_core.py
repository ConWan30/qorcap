"""S2S body cue v0: numpy-only tests (synthetic inputs, no frames, no models, no runtimes)."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bodycue_core as core  # noqa: E402


def test_reticle_box_is_centred_384_square_at_native_720p():
    assert core.reticle_box() == (448, 168, 832, 552)


def test_crop_refuses_non_native_frames():
    with pytest.raises(ValueError):
        core.crop_reticle(np.zeros((360, 640, 3), np.uint8))


def test_crop_takes_native_pixels_without_rescale():
    img = np.zeros((720, 1280, 3), np.uint8)
    img[360, 640] = (1, 2, 3)
    crop, box = core.crop_reticle(img)
    assert crop.shape == (384, 384, 3) and box == (448, 168, 832, 552)
    assert tuple(crop[192, 192]) == (1, 2, 3)


def test_yolox_grids_match_416_anchor_count():
    g, s = core.yolox_grids(416)
    assert g.shape == (3549, 2) and s.shape == (3549, 1)
    assert s[0, 0] == 8 and s[-1, 0] == 32


def test_yolox_preprocess_pads_384_crop_to_416_with_ratio():
    x, r = core.yolox_preprocess(np.full((384, 384, 3), 7, np.uint8))
    assert x.shape == (1, 3, 416, 416) and x.dtype == np.float32
    assert abs(r - 416 / 384) < 1e-9
    assert np.allclose(x[0, :, :416, :416], 7.0)


def _raw_with_one_person(cx=200.0, cy=180.0, w=64.0, h=128.0, obj=0.9, p=0.95, cls=core.COCO80_PERSON):
    raw = np.zeros((3549, 85), np.float32)
    raw[:, 4] = 0.0
    g, s = core.yolox_grids(416)
    i = int(np.argmin(np.abs(g[:, 0] * s[:, 0] - cx) + np.abs(g[:, 1] * s[:, 0] - cy) + (s[:, 0] != 16) * 1e6))
    st = s[i, 0]
    raw[i, 0] = cx / st - g[i, 0]; raw[i, 1] = cy / st - g[i, 1]
    raw[i, 2] = np.log(w / st); raw[i, 3] = np.log(h / st)
    raw[i, 4] = obj; raw[i, 5 + cls] = p
    return raw


def test_yolox_decode_and_person_box_round_trip():
    dec = core.yolox_decode(_raw_with_one_person()[None])
    boxes = core.yolox_person_boxes(dec, ratio=1.0, score_thr=0.3)
    assert len(boxes) == 1
    assert boxes[0]["xyxy"] == pytest.approx([168.0, 116.0, 232.0, 244.0], abs=0.2)
    assert boxes[0]["score"] == pytest.approx(0.9 * 0.95, abs=1e-4)
    assert core.yolox_person_max(dec) == pytest.approx(0.855, abs=1e-4)


def test_yolox_non_person_class_is_ignored():
    dec = core.yolox_decode(_raw_with_one_person(cls=2))
    assert core.yolox_person_boxes(dec, 1.0, 0.3) == []
    assert core.yolox_person_max(dec) == 0.0


def test_nms_drops_overlap_keeps_disjoint():
    b = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [50, 50, 60, 60]], np.float32)
    assert core.nms(b, np.array([0.9, 0.8, 0.7]), 0.45) == [0, 2]


def test_rfdetr_preprocess_normalises_imagenet():
    x = core.rfdetr_preprocess(np.zeros((384, 384, 3), np.uint8))
    assert x.shape == (1, 3, 384, 384)
    assert np.allclose(x[0, :, 0, 0], -core.IMAGENET_MEAN / core.IMAGENET_STD, atol=1e-6)


def test_rfdetr_person_boxes_threshold_and_scale():
    dets = np.zeros((1, 300, 4), np.float32); logits = np.full((1, 300, 91), -10.0, np.float32)
    dets[0, 5] = [0.5, 0.5, 0.25, 0.5]; logits[0, 5, core.COCO91_PERSON] = 2.0   # sigmoid ~0.881
    dets[0, 9] = [0.1, 0.1, 0.1, 0.1]; logits[0, 9, core.COCO91_PERSON] = -1.0   # ~0.269, below 0.5
    logits[0, 7, 3] = 5.0                                                       # a car, ignored
    boxes = core.rfdetr_person_boxes(dets, logits, 0.5)
    assert len(boxes) == 1 and boxes[0]["xyxy"] == pytest.approx([144.0, 96.0, 240.0, 288.0])
    assert core.rfdetr_person_max(logits) == pytest.approx(1 / (1 + np.exp(-2.0)), abs=1e-4)
    assert core.top1_classes(logits)[7] == 3


def test_frame_record_matches_score_agreement_detect_contract():
    rec = core.frame_record("Fabc123", [{"xyxy": [0, 0, 1, 1], "score": 0.9, "cls": "person"}], 0.9, (448, 168, 832, 552))
    assert rec["enemy_flag"] is True and rec["foe"] == "foe_unknown"
    assert set(rec) >= {"frame_id", "enemy_flag", "boxes", "foe"}
    json.dumps(rec)
    assert core.frame_record("F0", [], 0.1, (0, 0, 1, 1))["enemy_flag"] is False


def test_percentile_median_and_p95():
    xs = list(range(1, 101))
    assert core.percentile(xs, 50) == pytest.approx(50.5)
    assert core.percentile(xs, 95) == pytest.approx(95.05)
    assert core.percentile([], 50) is None
