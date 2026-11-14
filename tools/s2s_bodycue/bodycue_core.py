"""S2S body cue v0: pure-numpy pre/post-processing for zero-shot COCO person detectors.

No model runtime is imported here, so the default CI (numpy only) can test it.
Runtimes (onnxruntime, torch, rfdetr, openvino) are optional and live in
detect_person.py / export_rfdetr.py / parity_rfdetr.py (see README.md).

Frames are 1280x720 native. The crop is a 384x384 square centred on the screen
centre (the reticle), with no downscale (BODYCUE-CLOUD-BRIEF, Con 8:06/8:08 PM CST).
A person box anywhere in the crop sets person_flag. Friend vs foe is phase B, so every
box here is foe="foe_unknown" and enemy_flag means "COCO person seen in the crop" only.
"""
from __future__ import annotations

import numpy as np

FRAME_W, FRAME_H = 1280, 720
CROP = 384
COCO80_PERSON = 0     # YOLOX 80-class index
COCO91_PERSON = 1     # RF-DETR logits use the 91-slot COCO id space; person is id 1
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
SCHEMA = "s2s-stage1-detector/v0"


def reticle_box(w: int = FRAME_W, h: int = FRAME_H, size: int = CROP) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of a size x size square centred on the frame centre, clamped inside the frame."""
    if size > w or size > h:
        raise ValueError(f"crop {size} larger than frame {w}x{h}")
    x0 = min(max(w // 2 - size // 2, 0), w - size)
    y0 = min(max(h // 2 - size // 2, 0), h - size)
    return x0, y0, x0 + size, y0 + size


def crop_reticle(img: np.ndarray, size: int = CROP) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """HxWxC image -> (size x size x C crop, box). The frame must be 1280x720 (native, no rescale)."""
    h, w = img.shape[:2]
    if (w, h) != (FRAME_W, FRAME_H):
        raise ValueError(f"expected native {FRAME_W}x{FRAME_H}, got {w}x{h}")
    x0, y0, x1, y1 = reticle_box(w, h, size)
    return np.ascontiguousarray(img[y0:y1, x0:x1]), (x0, y0, x1, y1)


# ---------------- YOLOX (Megvii ONNX release 0.1.1rc0, demo/ONNXRuntime conventions) ----------------

def yolox_preprocess(bgr: np.ndarray, size: int = 416, pad: int = 114) -> tuple[np.ndarray, float]:
    """Letterbox like YOLOX demo preproc: bilinear resize keeping aspect (int dims), pad 114, BGR, 0..255, NCHW."""
    h, w = bgr.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(h * r), int(w * r)
    out = np.full((size, size, 3), pad, dtype=np.float32)
    out[:nh, :nw] = resize_bilinear(bgr, nh, nw)
    return out.transpose(2, 0, 1)[None].copy(), r


def resize_bilinear(img: np.ndarray, nh: int, nw: int) -> np.ndarray:
    """Plain bilinear resize (align_corners=False), numpy only; used when cv2 is absent."""
    h, w = img.shape[:2]
    if (h, w) == (nh, nw):
        return img.astype(np.float32)
    y = (np.arange(nh) + 0.5) * h / nh - 0.5
    x = (np.arange(nw) + 0.5) * w / nw - 0.5
    y0 = np.floor(y).astype(int).clip(0, h - 1); y1 = (y0 + 1).clip(0, h - 1); wy = (y - y0).clip(0, 1)[:, None, None]
    x0 = np.floor(x).astype(int).clip(0, w - 1); x1 = (x0 + 1).clip(0, w - 1); wx = (x - x0).clip(0, 1)[None, :, None]
    f = img.astype(np.float32)
    top = f[y0][:, x0] * (1 - wx) + f[y0][:, x1] * wx
    bot = f[y1][:, x0] * (1 - wx) + f[y1][:, x1] * wx
    return top * (1 - wy) + bot * wy


def yolox_grids(size: int = 416, strides=(8, 16, 32)) -> tuple[np.ndarray, np.ndarray]:
    grids, exp = [], []
    for s in strides:
        n = size // s
        xv, yv = np.meshgrid(np.arange(n), np.arange(n))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        exp.append(np.full((n * n, 1), s))
    return np.concatenate(grids).astype(np.float32), np.concatenate(exp).astype(np.float32)


def yolox_decode(raw: np.ndarray, size: int = 416) -> np.ndarray:
    """raw (N, 85) or (1, N, 85) -> decoded (N, 85): cx, cy, w, h in input pixels, obj, 80 class probs."""
    out = np.array(raw, dtype=np.float32).reshape(-1, raw.shape[-1]).copy()
    g, s = yolox_grids(size)
    if out.shape[0] != g.shape[0]:
        raise ValueError(f"{out.shape[0]} anchors, expected {g.shape[0]} for {size}")
    out[:, :2] = (out[:, :2] + g) * s
    out[:, 2:4] = np.exp(out[:, 2:4]) * s
    return out


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    """Greedy NMS on xyxy boxes; returns kept indices, highest score first."""
    order = np.argsort(-scores, kind="stable")
    keep = []
    while order.size:
        i = int(order[0]); keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx0 = np.maximum(boxes[i, 0], boxes[rest, 0]); yy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx1 = np.minimum(boxes[i, 2], boxes[rest, 2]); yy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        a = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        b = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        ov = inter / np.maximum(a + b - inter, 1e-9)
        order = rest[ov <= iou]
    return keep


def yolox_person_boxes(decoded: np.ndarray, ratio: float, score_thr: float, nms_iou: float = 0.45,
                       crop_wh: tuple[int, int] = (CROP, CROP)) -> list[dict]:
    """Person boxes (crop pixels, xyxy) with score = obj * p(person), class-wise NMS like the YOLOX demo."""
    obj = decoded[:, 4]; cls = decoded[:, 5:]
    best = cls.argmax(1)
    score = obj * cls[np.arange(len(cls)), best]
    m = (best == COCO80_PERSON) & (score >= score_thr)
    if not m.any():
        return []
    d = decoded[m]; sc = score[m]
    xyxy = np.stack([d[:, 0] - d[:, 2] / 2, d[:, 1] - d[:, 3] / 2, d[:, 0] + d[:, 2] / 2, d[:, 1] + d[:, 3] / 2], 1) / ratio
    xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, crop_wh[0]); xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, crop_wh[1])
    return [{"xyxy": [round(float(v), 1) for v in xyxy[i]], "score": round(float(sc[i]), 4), "cls": "person"}
            for i in nms(xyxy, sc, nms_iou)]


def yolox_person_max(decoded: np.ndarray) -> float:
    """Highest obj * p(person) among anchors whose best class is person (0.0 if none)."""
    cls = decoded[:, 5:]; best = cls.argmax(1)
    s = decoded[:, 4] * cls[:, COCO80_PERSON]
    s = s[best == COCO80_PERSON]
    return float(s.max()) if s.size else 0.0


# ---------------- RF-DETR (rfdetr ONNX export: dets [1,300,4] cxcywh 0..1, labels [1,300,91] logits) ----------------

def rfdetr_preprocess(rgb: np.ndarray, size: int = 384) -> np.ndarray:
    """RGB uint8 HxWx3 -> NCHW float32, /255, ImageNet mean/std (rfdetr predict convention)."""
    f = resize_bilinear(rgb, size, size) if rgb.shape[:2] != (size, size) else rgb.astype(np.float32)
    f = (f / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
    return f.transpose(2, 0, 1)[None].astype(np.float32).copy()


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=np.float64)))


def rfdetr_person_boxes(dets: np.ndarray, logits: np.ndarray, score_thr: float,
                        crop_wh: tuple[int, int] = (CROP, CROP)) -> list[dict]:
    """Queries whose sigmoid person score >= thr, as xyxy crop pixels (DETR: no NMS)."""
    d = np.asarray(dets, dtype=np.float64).reshape(-1, 4)
    p = sigmoid(np.asarray(logits).reshape(d.shape[0], -1)[:, COCO91_PERSON])
    idx = np.where(p >= score_thr)[0]
    idx = idx[np.argsort(-p[idx], kind="stable")]
    W, H = crop_wh
    out = []
    for i in idx:
        cx, cy, w, h = d[i]
        b = [max(0.0, (cx - w / 2) * W), max(0.0, (cy - h / 2) * H), min(W, (cx + w / 2) * W), min(H, (cy + h / 2) * H)]
        out.append({"xyxy": [round(float(v), 1) for v in b], "score": round(float(p[i]), 4), "cls": "person"})
    return out


def rfdetr_person_max(logits: np.ndarray) -> float:
    l = np.asarray(logits).reshape(-1, np.asarray(logits).shape[-1])
    return float(sigmoid(l[:, COCO91_PERSON]).max())


def top1_classes(logits: np.ndarray) -> np.ndarray:
    """Per-query argmax class over the 91 COCO slots."""
    l = np.asarray(logits)
    return l.reshape(-1, l.shape[-1]).argmax(1)


# ---------------- output contract (matches score_agreement.py detect) ----------------

def frame_record(frame_id: str, boxes: list[dict], person_max: float, crop_box, ms: dict | None = None) -> dict:
    rec = {"frame_id": frame_id, "enemy_flag": bool(boxes), "person_max": round(float(person_max), 4),
           "boxes": boxes, "foe": "foe_unknown", "crop": list(crop_box)}
    if ms is not None:
        rec["ms"] = ms
    return rec


def percentile(xs, q: float) -> float | None:
    """Linear-interpolated percentile (numpy default method) of a non-empty list; None if empty."""
    if not len(xs):
        return None
    return float(np.percentile(np.asarray(xs, dtype=np.float64), q))
