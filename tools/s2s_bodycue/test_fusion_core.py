"""S2S body cue fusion (phase B): numpy-only tests on synthetic frames. No gameplay frames, no models."""
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as F  # noqa: E402

HERE = Path(__file__).resolve().parent
RED, CYAN, GREEN, GREY = (30, 30, 230), (230, 200, 40), (40, 220, 40), (90, 90, 90)  # BGR


def frame(fill=GREY):
    img = np.zeros((720, 1280, 3), np.uint8); img[:] = fill
    return img


def v02(hud=True, dots=0, tags=()):
    return {"hud_state": "gameplay_hud" if hud else "no_minimap", "minimap_enemy_dots": {"count": dots},
            "inworld_tag": {"gated_no_hud": not hud, "tags": list(tags)}}


def person(xyxy_crop, score=0.9, src="rfdetr"):
    return {"xyxy": list(xyxy_crop), "score": score, "cls": "person"}


def paint_head(img, xyxy_crop, bgr, h=8, w=30):
    x0, y0, x1, _ = F.to_frame(xyxy_crop)
    cx = int((x0 + x1) / 2); top = int(y0) - 16
    img[top:top + h, cx - w // 2:cx + w // 2] = bgr
    return img


BOX = (150, 150, 190, 250)  # crop pixels -> frame (598, 318, 638, 418)


def test_hsv_matches_opencv_convention_on_primaries():
    px = np.array([[[0, 0, 255], [0, 255, 0], [255, 0, 0], [255, 255, 0], [128, 128, 128]]], np.uint8)
    hsv = F.bgr_to_hsv(px)[0]
    assert hsv[:, 0].tolist() == [0, 60, 120, 90, 0]
    assert hsv[:4, 1].tolist() == [255] * 4 and hsv[4, 1] == 0
    assert hsv[:, 2].tolist() == [255, 255, 255, 255, 128]


def test_merge_dedupes_across_models_and_applies_thresholds():
    P = F.DEFAULT_PARAMS
    y = [person(BOX, 0.4)]; r = [person((152, 151, 191, 251), 0.8), person((10, 10, 30, 60), 0.2)]
    m = F.merge_persons(y, r, P)
    assert len(m) == 1 and sorted(m[0]["srcs"]) == ["rfdetr", "yolox"] and m[0]["score"] == 0.8
    assert m[0]["xyxy"] == [600, 319, 639, 419]


def test_merge_drops_tiny_and_huge_boxes():
    m = F.merge_persons([person((0, 0, 20, 5), 0.9), person((0, 0, 380, 380), 0.9)], [], F.DEFAULT_PARAMS)
    assert m == []


def test_red_head_region_gives_yes_with_reason():
    img = paint_head(frame(), BOX, RED)
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    r = F.decide(v02(), ps, F.bgr_to_hsv(img))
    assert r["decision"] == "yes" and r["enemy_flag"] is True
    assert r["persons"][0]["ff"] == "foe" and any("red_head" in x for x in r["reasons"])


@pytest.mark.parametrize("colour", [CYAN, GREEN])
def test_friendly_colour_is_friend_not_yes(colour):
    img = paint_head(frame(), BOX, colour)
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    r = F.decide(v02(dots=1), ps, F.bgr_to_hsv(img))
    assert r["persons"][0]["ff"] == "friend" and r["decision"] == "unknown"


def test_absence_of_evidence_is_unknown_never_no():
    hsv = F.bgr_to_hsv(frame())
    for args in [(v02(), []), (v02(dots=0), F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)), (v02(hud=False), [])]:
        r = F.decide(args[0], args[1], hsv)
        assert r["decision"] == "unknown" and r["enemy_flag"] is False


def test_no_needs_only_friend_persons_hud_no_dots_no_tags():
    img = paint_head(frame(), BOX, CYAN)
    hsv = F.bgr_to_hsv(img)
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    assert F.decide(v02(), ps, hsv)["decision"] == "no"
    assert F.decide(v02(dots=2), ps, hsv)["decision"] == "unknown"
    assert F.decide(v02(hud=False), ps, hsv)["decision"] == "unknown"
    P = copy.deepcopy(F.DEFAULT_PARAMS); P["no_rule_enabled"] = False
    assert F.decide(v02(), ps, hsv, P)["decision"] == "unknown"


def test_person_cues_gated_without_hud():
    img = paint_head(frame(), BOX, RED)
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    r = F.decide(v02(hud=False), ps, F.bgr_to_hsv(img))
    assert r["decision"] == "unknown" and any("gated" in x for x in r["reasons"])


def test_reticle_mask_ignores_red_dot_sight():
    img = frame(); img[350:370, 630:650] = RED
    ps = F.merge_persons([], [person((170, 190, 210, 280))], F.DEFAULT_PARAMS)  # head region covers the reticle
    r = F.decide(v02(), ps, F.bgr_to_hsv(img))
    assert r["persons"][0]["colour_px"]["red"] == 0 and r["decision"] == "unknown"


def test_tag_counts_only_on_a_person_head_region():
    hsv = F.bgr_to_hsv(frame())
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    reg = F.head_region(ps[0]["xyxy"])
    on = {"strip": [reg[0] + 2, reg[1] + 2, 20, 8]}; off = {"strip": [500, 200, 20, 8]}
    assert F.decide(v02(tags=[on]), ps, hsv)["decision"] == "yes"
    assert F.decide(v02(tags=[off]), ps, hsv)["decision"] == "unknown"
    assert F.decide(v02(tags=[on]), [], hsv)["decision"] == "unknown"


def test_dots_alone_never_yes():
    r = F.decide(v02(dots=5), [], F.bgr_to_hsv(frame()))
    assert r["decision"] == "unknown" and any("minimap_dots_prior" in x for x in r["reasons"])


def test_committed_frozen_params_equal_defaults():
    fz = json.load(open(HERE / "FROZEN-fusion-params.json", encoding="utf-8"))
    assert fz["params"] == F.DEFAULT_PARAMS
    assert fz["schema"] == "s2s-bodycue-fusion-frozen/v0"


MALFORMED = {
    "missing_count": lambda r: r["minimap_enemy_dots"].pop("count"),
    "nan_count": lambda r: r["minimap_enemy_dots"].__setitem__("count", float("nan")),
    "none_count": lambda r: r["minimap_enemy_dots"].__setitem__("count", None),
    "negative_count": lambda r: r["minimap_enemy_dots"].__setitem__("count", -1),
    "bool_count": lambda r: r["minimap_enemy_dots"].__setitem__("count", False),
    "float_count": lambda r: r["minimap_enemy_dots"].__setitem__("count", 0.0),
    "missing_dots_object": lambda r: r.pop("minimap_enemy_dots"),
    "missing_tags": lambda r: r["inworld_tag"].pop("tags"),
    "tags_none": lambda r: r["inworld_tag"].__setitem__("tags", None),
    "bad_tag_strip": lambda r: r["inworld_tag"].__setitem__("tags", [{"strip": [1, 2, float("inf"), 4]}]),
    "missing_tag_object": lambda r: r.pop("inworld_tag"),
    "missing_gated_flag": lambda r: r["inworld_tag"].pop("gated_no_hud"),
    "missing_minimap_flag": lambda r: r.pop("hud_state"),
    "invalid_minimap_flag": lambda r: r.__setitem__("hud_state", True),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_v02_record_is_unknown_never_no(case):
    """P174-M1: a friend-coloured person would give `no` on a clean record; a broken field must give unknown."""
    img = paint_head(frame(), BOX, CYAN)
    hsv = F.bgr_to_hsv(img)
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    assert F.decide(v02(), ps, hsv)["decision"] == "no"
    rec = v02(); MALFORMED[case](rec)
    r = F.decide(rec, ps, hsv)
    assert r["decision"] == "unknown" and r["enemy_flag"] is False and r["malformed"]
    assert r["reasons"][0].startswith("malformed_v02_record")


def test_malformed_record_cannot_give_yes_either():
    img = paint_head(frame(), BOX, RED)
    ps = F.merge_persons([], [person(BOX)], F.DEFAULT_PARAMS)
    rec = v02(); rec["minimap_enemy_dots"].pop("count")
    assert F.decide(rec, ps, F.bgr_to_hsv(img))["decision"] == "unknown"
    assert F.decide("not a dict", ps, F.bgr_to_hsv(img))["malformed"] == ["v02_record_not_an_object"]


def test_fuse_load_params_pins_published_hash_and_parses_the_same_bytes(tmp_path):
    import fuse
    from frozen_pin import FROZEN_PARAMS_SHA256
    fz, sha = fuse.load_params(HERE / "FROZEN-fusion-params.json")
    assert sha == FROZEN_PARAMS_SHA256 and fz["params"] == F.DEFAULT_PARAMS
    other = tmp_path / "f.json"
    other.write_bytes((HERE / "FROZEN-fusion-params.json").read_bytes() + b"\n")
    with pytest.raises(SystemExit):
        fuse.load_params(other)
    with pytest.raises(SystemExit):
        fuse.load_params(HERE / "FROZEN-fusion-params.json", "0" * 64)
