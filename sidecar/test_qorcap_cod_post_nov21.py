"""Purely synthetic tests for post-Nov-21 CoD info-only HUD readers."""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

SIDECAR = Path(__file__).resolve().parent
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))

import qorcap_cod_hud as hud  # noqa: E402
import qorcap_cod_killfeed as kf  # noqa: E402
import qorcap_cod_post_nov21 as pn  # noqa: E402


def _canvas(h: int = 720, w: int = 1280, fill: tuple[int, int, int] = (28, 32, 30)):
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:, :] = fill
    return img


def _draw_diag_arms(img, bgr: tuple[int, int, int], *, gap: int = 7, reach: int = 20, thickness: int = 2):
    cy, cx = img.shape[0] // 2, img.shape[1] // 2
    for dx_s in (-1, 1):
        for dy_s in (-1, 1):
            cv2.line(img, (cx + dx_s * gap, cy + dy_s * gap), (cx + dx_s * reach, cy + dy_s * reach), bgr, thickness)


def _draw_cardinal_ticks(img, bgr: tuple[int, int, int] = (245, 245, 245), *, gap: int = 9, reach: int = 20):
    cy, cx = img.shape[0] // 2, img.shape[1] // 2
    cv2.circle(img, (cx, cy), 2, bgr, -1)
    cv2.line(img, (cx, cy - gap), (cx, cy - reach), bgr, 2)
    cv2.line(img, (cx, cy + gap), (cx, cy + reach), bgr, 2)
    cv2.line(img, (cx - gap, cy), (cx - reach, cy), bgr, 2)
    cv2.line(img, (cx + gap, cy), (cx + reach, cy), bgr, 2)


def _paste_word(img, text: str, x0: int, y0: int, *, pitch: float = 7.0, scale: float = 1.0, bgr=(255, 255, 255)):
    t = kf.word_template(text, pitch, scale=scale)
    assert t is not None
    mask = np.clip(t / 255.0, 0.0, 1.0)
    h, w = mask.shape[:2]
    y1, x1 = min(img.shape[0], y0 + h), min(img.shape[1], x0 + w)
    alpha = mask[:y1 - y0, :x1 - x0, None]
    roi = img[y0:y1, x0:x1].astype(np.float32)
    col = np.array(bgr, dtype=np.float32).reshape(1, 1, 3)
    img[y0:y1, x0:x1] = np.clip(roi * (1.0 - alpha) + col * alpha, 0, 255).astype(np.uint8)
    return h, w


def _paste_mask(img, mask_u8, x0: int, y0: int, *, scale: float = 1.0, bgr=(240, 240, 240)):
    scaled = cv2.resize(mask_u8, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    h, w = scaled.shape[:2]
    y1, x1 = min(img.shape[0], y0 + h), min(img.shape[1], x0 + w)
    alpha = (scaled[:y1 - y0, :x1 - x0, None].astype(np.float32)) / 255.0
    roi = img[y0:y1, x0:x1].astype(np.float32)
    col = np.array(bgr, dtype=np.float32).reshape(1, 1, 3)
    img[y0:y1, x0:x1] = np.clip(roi * (1.0 - alpha) + col * alpha, 0, 255).astype(np.uint8)
    return h, w


def _draw_spaced_digits(img, text: str, x0: int, y0: int, *, scale: float = 2.0, bgr=(245, 245, 245)):
    x = x0
    step = int(round(8.5 * scale))
    for ch in text:
        _paste_word(img, ch, x, y0, pitch=7.0, scale=scale, bgr=bgr)
        x += step
    return x


def test_center_box_geometry_scales_across_resolutions():
    expected = {
        (720, 1280): (60, 60, 3600),
        (1080, 1920): (90, 90, 8100),
        (1440, 2560): (120, 120, 14400),
        (2160, 3840): (180, 180, 32400),
    }
    for (h, w), (rw, rh, rpx) in expected.items():
        box = pn.center_box_for_shape(h, w)
        assert box is not None
        assert (box["w"], box["h"], box["roi_px"]) == (rw, rh, rpx)
        assert abs(box["area_pct"] - 0.3906) < 0.01
    assert pn.center_box_for_shape(100, 150) is None


def test_center_box_trigger_white_hit_red_kill_and_wide_offcenter_rejected():
    trig = pn.CenterBoxTrigger()
    idle = _canvas()
    _draw_cardinal_ticks(idle)
    r_idle = trig.step(idle, t_us=1000)
    assert r_idle["state"] == "NONE"
    assert r_idle["info_only"] is True and r_idle["counted"] is False

    # Wide off-center red blob MUST NEVER trigger CenterBoxTrigger KILL (no wide-crop _red_x path).
    offcenter = _canvas()
    _draw_cardinal_ticks(offcenter)
    cv2.circle(offcenter, (900, 250), 22, (0, 0, 255), -1)
    cv2.line(offcenter, (640, 365), (640, 385), (0, 0, 240), 3)  # single vertical red sight post
    r_off = trig.step(offcenter, t_us=2000)
    assert r_off["state"] != "KILL"

    # White diagonal hitmarker inside fixed center box -> HIT.
    hit = _canvas()
    _draw_cardinal_ticks(hit)
    _draw_diag_arms(hit, (245, 245, 245))
    r_hit = trig.step(hit, t_us=3000)
    assert r_hit["state"] == "HIT"
    assert r_hit["triggered"] is True

    # Red diagonal hitmarker inside fixed center box -> KILL.
    kill = _canvas()
    _draw_diag_arms(kill, (20, 20, 245))
    r_kill = trig.step(kill, t_us=4000)
    assert r_kill["state"] == "KILL"
    assert r_kill["triggered"] is True
    assert "_state" not in r_kill


def test_center_box_trigger_abstains_on_flash_explosion_and_static_decal():
    trig = pn.CenterBoxTrigger()
    flash = _canvas(fill=(245, 245, 245))
    assert trig.step(flash, t_us=1000)["state"] == "ABSTAIN"

    red_flood = _canvas()
    red_flood[320:400, 600:680] = (10, 10, 240)
    assert trig.step(red_flood, t_us=2000)["state"] == "ABSTAIN"

    trig.reset()
    static_decal = _canvas()
    _draw_diag_arms(static_decal, (20, 20, 240))
    states = [trig.step(static_decal, t_us=i * 16_000)["state"] for i in range(6)]
    assert states[0] == "KILL"
    assert states[-1] == "ABSTAIN"


def test_center_box_trigger_ads_retraction():
    trig = pn.CenterBoxTrigger()
    hip = _canvas()
    _draw_cardinal_ticks(hip)
    assert trig.step(hip, t_us=1000)["state"] == "NONE"

    ads = _canvas()
    cv2.circle(ads, (640, 360), 2, (245, 245, 245), -1)
    r_ads = trig.step(ads, t_us=2000)
    assert r_ads["state"] == "ADS_OR_FIRING"
    assert r_ads["triggered"] is True


def test_cod_digits_ammo_and_live_score_decode_0_to_100_plus():
    frame = _canvas()
    # Bottom-right ammo: mag "21" (tall ~24px) + reserve "108" (smaller ~16px)
    _draw_spaced_digits(frame, "21", 950, 590, scale=2.5, bgr=(248, 248, 248))
    _draw_spaced_digits(frame, "108", 1020, 600, scale=1.6, bgr=(240, 240, 240))
    ammo = pn.read_ammo_counter(frame)
    assert ammo["ok"] is True and ammo["mag"] == 21 and ammo["reserve"] == 108
    assert ammo["info_only"] is True and ammo["counted"] is False

    # Left HUD live team scores: top "21", bottom "105" (~16px height matching 720p live scores)
    _draw_spaced_digits(frame, "21", 32, 185, scale=1.6, bgr=(240, 240, 240))
    _draw_spaced_digits(frame, "105", 32, 240, scale=1.6, bgr=(240, 240, 240))
    score = pn.read_live_score(frame)
    assert score["ok"] is True and score["top_score"] == 21 and score["bottom_score"] == 105

    # Blank / tiny noisy patch must ABSTAIN cleanly
    blank = _canvas(100, 120)
    assert pn.read_cod_digits(blank)["state"] == "ABSTAIN"
    assert pn.read_ammo_counter(_canvas())["state"] == "ABSTAIN"


def test_kill_xp_popup_detects_xp_and_abstains_on_flood():
    frame = _canvas()
    xp_bgr = (0, 220, 255)  # yellow in OpenCV BGR (H ~ 26)
    end_x = _draw_spaced_digits(frame, "210", 605, 290, scale=1.8, bgr=xp_bgr)
    _paste_word(frame, "XP", end_x + 8, 290, pitch=7.0, scale=1.8, bgr=xp_bgr)
    res = pn.read_kill_xp_popup(frame)
    assert res["ok"] is True and res["state"] == "XP_POPUP"
    assert res["xp_value"] == 210
    assert res["info_only"] is True and res["counted"] is False

    assert pn.read_kill_xp_popup(_canvas())["state"] == "NONE"

    flood = _canvas()
    flood[180:380, 460:860] = xp_bgr
    assert pn.read_kill_xp_popup(flood)["state"] == "ABSTAIN"


def test_post_nov21_owner_row_large_text_and_fail_closed():
    pack = hud._pack()
    assert pack is not None
    # Construct synthetic Large-Text Domination board (row_scale=1.35, headers ~1.2x, CAPTURES mode + personal card DEATHS)
    board = _canvas()
    gold = (35, 195, 245)
    # Row owner tag Qortrola30 at 1.35x
    _paste_word(board, "Qortrola30", 173, 453, pitch=7.0, scale=1.35, bgr=gold)
    # Headers at 1.2x
    _paste_mask(board, pack["score_header_mask"], 330, 398, scale=1.15)
    _paste_mask(board, pack["kills_header_mask"], 430, 398, scale=1.20)
    _paste_mask(board, pack["mode_header_masks"]["CAPTURES"], 530, 398, scale=1.25)
    # Row values: score=303, kills=25, captures=1 (cleanly spaced digits)
    _draw_spaced_digits(board, "303", 332, 453, scale=1.0, bgr=(240, 240, 240))
    _draw_spaced_digits(board, "25", 434, 453, scale=1.0, bgr=(240, 240, 240))
    _draw_spaced_digits(board, "1", 545, 453, scale=1.0, bgr=(240, 240, 240))
    # Personal card below
    _paste_mask(board, pack["owner_card_masks"][0], 380, 622, scale=1.0, bgr=gold)
    _paste_word(board, "ELIMINATIONS", 585, 632, pitch=7.0, scale=1.15, bgr=(225, 225, 225))
    _draw_spaced_digits(board, "27", 605, 652, scale=1.6, bgr=(245, 245, 245))
    _paste_word(board, "DEATHS", 752, 632, pitch=7.0, scale=1.15, bgr=(225, 225, 225))
    _draw_spaced_digits(board, "21", 762, 652, scale=1.6, bgr=(245, 245, 245))

    got = pn.read_owner_row_post_nov21(board)
    assert got["ok"] is True
    assert got["kills"] == 25 and got["deaths"] == 21
    assert got["score"] == 303 and got["eliminations"] == 27
    assert got["assists"] is None
    assert got["info_only"] is True and got["counted"] is False

    # 1.0x Hardpoint-style no-DEATHS-column fallback via OBJ. TIME personal card
    hp = _canvas()
    _paste_mask(hp, pack["owner_mask"], 172, 481, scale=1.0, bgr=gold)
    _paste_mask(hp, pack["score_header_mask"], 333, 400, scale=1.0)
    _paste_mask(hp, pack["kills_header_mask"], 434, 400, scale=1.0)
    _paste_word(hp, "OBJ. TIME", 535, 402, pitch=5.8, scale=1.0, bgr=(230, 230, 230))
    _draw_spaced_digits(hp, "452", 333, 481, scale=1.1, bgr=(240, 240, 240))
    _draw_spaced_digits(hp, "26", 434, 481, scale=1.1, bgr=(240, 240, 240))
    _draw_spaced_digits(hp, "56", 535, 481, scale=1.1, bgr=(240, 240, 240))
    _paste_mask(hp, pack["owner_card_masks"][0], 376, 626, scale=1.0, bgr=gold)
    _paste_mask(hp, pack["deaths_header_mask"], 755, 632, scale=1.0)
    _draw_spaced_digits(hp, "40", 760, 650, scale=1.6, bgr=(245, 245, 245))
    got_hp = pn.read_owner_row_post_nov21(hp)
    assert got_hp["ok"] is True and got_hp["kills"] == 26 and got_hp["deaths"] == 40
    assert got_hp["mode_header"] == "NO_DEATHS_COLUMN_OR_OBJ_TIME"

    # Missing DEATHS card label must fail closed -> ABSTAIN
    no_deaths = board.copy()
    no_deaths[590:710, :] = 25
    ab = pn.read_owner_row_post_nov21(no_deaths)
    assert ab["ok"] is False and ab["state"] == "ABSTAIN"


def test_killfeed_large_text_stacked_self_and_rival_rejection():
    frame = _canvas()
    # Band starts at y=288. Draw non-self red line slightly above + 1.25x gold Qortrola30 line just below it.
    gold = (30, 195, 245)
    red = (40, 40, 235)
    cyan = (230, 190, 30)
    # Teammate cyan stacked closely above Large-Text gold self line (with a thin bright bridge)
    _paste_word(frame, "AllyTag99", 30, 434, pitch=7.0, scale=1.0, bgr=cyan)
    cv2.rectangle(frame, (118, 436), (144, 444), (240, 240, 240), -1)
    _paste_word(frame, "EnemyOne", 150, 434, pitch=7.0, scale=1.0, bgr=red)
    # 1-row faint halo bridge between stacked lines
    frame[448:450, 32:44] = (95, 95, 95)
    # Large-Text 1.25x gold self line
    _paste_word(frame, "Qortrola30", 30, 450, pitch=7.0, scale=1.25, bgr=gold)
    cv2.rectangle(frame, (132, 453), (156, 463), (245, 245, 245), -1)
    _paste_word(frame, "TargetTwo", 162, 450, pitch=6.6, scale=1.25, bgr=red)
    # Nearby rival 1.25x Qortrola31 lower down MUST NOT read as self
    _paste_word(frame, "Qortrola31", 30, 492, pitch=7.0, scale=1.25, bgr=gold)
    cv2.rectangle(frame, (132, 495), (156, 505), (245, 245, 245), -1)
    _paste_word(frame, "OtherFoe", 162, 492, pitch=6.6, scale=1.25, bgr=red)

    res = kf.read_killfeed_lines(frame)
    assert res["ok"] is True
    killers = [ln["killer"] for ln in res["lines"]]
    assert "self" in killers
    self_lines = [ln for ln in res["lines"] if ln["killer"] == "self"]
    assert len(self_lines) == 1
    assert self_lines[0].get("killer_hud_scale") == 1.25
    # Verify rival line Qortrola31 stayed non-self
    other_lines = [ln for ln in res["lines"] if ln is not self_lines[0]]
    assert all(ln["killer"] != "self" for ln in other_lines)


def test_reconcile_events_and_burst_windows():
    cb_hit = {"state": "HIT", "reason": "center_white_diagonal_marker"}
    cb_kill = {"state": "KILL", "reason": "center_red_diagonal_marker"}
    xp_ok = {"state": "XP_POPUP", "xp_value": 210}
    xp_none = {"state": "NONE", "xp_value": None}

    e_hit = pn.reconcile_event_signals(center_box=cb_hit, xp_popup=xp_none, t_us=1_000_000)
    assert e_hit["event"] == "HIT"

    e_kill = pn.reconcile_event_signals(
        center_box=cb_kill, xp_popup=xp_ok, killfeed_lines=[{"killer": "self"}], t_us=2_000_000,
    )
    assert e_kill["event"] == "KILL" and e_kill["killfeed_confirm"] is True

    # Red center without XP -> ABSTAIN
    e_red_no_xp = pn.reconcile_event_signals(center_box=cb_kill, xp_popup=xp_none, t_us=3_000_000)
    assert e_red_no_xp["event"] == "ABSTAIN"

    # Killfeed contradicts self kill -> ABSTAIN
    e_kf_disagree = pn.reconcile_event_signals(
        center_box=cb_kill, xp_popup=xp_ok, killfeed_lines=[{"killer": "not_self"}], t_us=4_000_000,
    )
    assert e_kf_disagree["event"] == "ABSTAIN"

    # XP without center marker -> ABSTAIN
    e_xp_only = pn.reconcile_event_signals(
        center_box={"state": "NONE"}, xp_popup=xp_ok, t_us=5_000_000,
    )
    assert e_xp_only["event"] == "ABSTAIN"

    bursts = pn.select_capture_bursts([e_hit, e_kill, e_red_no_xp], pre_ms=35, post_ms=500, merge_gap_ms=800)
    assert len(bursts) == 1
    assert bursts[0]["events"] == ["HIT", "KILL"]
    assert bursts[0]["clock_note"] == "same clock domain; residual UNMEASURED"
    assert bursts[0]["info_only"] is True and bursts[0]["counted"] is False


def test_killfeed_large_text_gold_near_miss_returns_unreadable():
    """#199 N1: 1.25x gold look-alike near-miss must return UNREADABLE, never NOT_SELF."""
    frame = _canvas()
    gold = (30, 195, 245)
    red = (40, 40, 235)
    # Gold 1.25x near-miss killer look-alike ("Qortrola31") + red victim
    _paste_word(frame, "Qortrola31", 30, 450, pitch=7.0, scale=1.25, bgr=gold)
    cv2.rectangle(frame, (132, 453), (156, 463), (245, 245, 245), -1)
    _paste_word(frame, "TargetOne", 162, 450, pitch=6.6, scale=1.25, bgr=red)
    # Non-gold cyan rival at 1.0x still returns not_self
    cyan = (230, 190, 30)
    _paste_word(frame, "AllyTag99", 30, 492, pitch=7.0, scale=1.0, bgr=cyan)
    cv2.rectangle(frame, (118, 494), (144, 502), (240, 240, 240), -1)
    _paste_word(frame, "OtherFoe", 150, 492, pitch=7.0, scale=1.0, bgr=red)

    res = kf.read_killfeed_lines(frame)
    assert res["ok"] is True
    assert len(res["lines"]) == 2
    assert res["lines"][0]["killer"] == kf.UNREADABLE
    assert res["lines"][0].get("killer_hud_scale") == 1.25
    assert res["lines"][1]["killer"] == kf.NOT_SELF
