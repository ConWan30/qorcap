"""Calibrated Call of Duty reads on Con's private practice frames.

A present signal is ok and calibrated. An empty or wrong crop stays not read.
VICTORY and DEFEAT agree as an end screen and do not invent home and away, so
they do not post a final. DEFEAT does not agree on a VICTORY frame, and
VICTORY does not agree on a DEFEAT frame. Scoreboard K/A is read when the
digits are there, and the comparison stays not Counted without an offline count.
"""
import json
from pathlib import Path

import cv2
import numpy as np

import qorcap_cod as C
import qorcap_cod_hud as H

FIX = Path(__file__).resolve().parent / "fixtures" / "cod"

BOARDS = {
    "sb_001.png": (42, 9),
    "sb_002.png": (49, 12),
    "sb_003.png": (38, 12),
}


def frame(name: str):
    img = cv2.imread(str(FIX / name))
    assert img is not None, name
    return img


def test_victory_splash_agrees_and_midmatch_does_not():
    end = C.CodEndScreenReader()
    splash = end.read(frame("end_173922_017.jpg"))
    assert splash.ok and splash.calibrated and splash.confirm
    assert splash.reason == "victory"
    assert splash.home is None and splash.away is None
    done = end.read(frame("end_173922_019.jpg"))
    assert done.ok and done.calibrated and done.confirm
    assert done.home is None and done.away is None
    for name in ("mid_173922_120s.jpg", "end_173922_007.jpg", "end_173922_010.jpg"):
        rd = end.read(frame(name))
        assert rd.confirm is False and rd.ok is False and rd.calibrated is False
        assert rd.reason == "not_present"


# Header word crops, pasted at the corner they occupied on the 720p frame.
# The crops are the word and the red field. They do not include the scoreboard.
# 030/031/033 are Domination match-20261101-092413. tdm is Team Deathmatch 162225.
# kc is the sliced Kill Confirmed header, match-20261104-202724.
HEADER_ROIS = (
    "defeat_header_030.png",
    "defeat_header_031.png",
    "defeat_header_033.png",
    "defeat_header_tdm_030.png",
    "defeat_header_kc_030.png",
)
HEADER_ORIGIN = H.DEFEAT_HEADER_ORIGIN
# The glitched splash. It stays in the tree as a negative: the letters do not clear the header.
SPLASH_ROI = "defeat_splash_028.png"
SPLASH_ORIGIN = (340, 250)
# Kill Confirmed splash from match-20261104-202724. Same miss: the centered word does not agree.
KC_SPLASH_ROI = "defeat_splash_kc_028.png"
KC_SPLASH_ORIGIN = (340, 240)
VICTORY_OR_MID = (
    "end_173922_017.jpg",
    "end_173922_019.jpg",
    "end_173922_007.jpg",
    "end_173922_010.jpg",
    "mid_173922_120s.jpg",
)


def place(name: str, origin: tuple[int, int]):
    roi = frame(name)
    canvas = np.zeros((720, 1280, 3), np.uint8)
    x, y = origin
    h, w = roi.shape[:2]
    canvas[y:y + h, x:x + w] = roi
    return canvas


def test_defeat_confirms_and_does_not_cross_victory():
    """DEFEAT agrees on Domination, Team Deathmatch, and the sliced Kill Confirmed header.

    Home and away stay unread. The glitched splash does not clear the header
    letters, so it does not agree. A VICTORY or mid-match frame does not agree
    as DEFEAT, and a DEFEAT frame does not agree as VICTORY.
    """
    end = C.CodEndScreenReader()
    for name in HEADER_ROIS:
        rd = end.read(place(name, HEADER_ORIGIN))
        assert rd.ok and rd.calibrated and rd.confirm, name
        assert rd.reason == "defeat"
        assert rd.home is None and rd.away is None
    splash = end.read(place(SPLASH_ROI, SPLASH_ORIGIN))
    assert splash.confirm is False and splash.ok is False and splash.reason == "not_present"
    kc_splash = end.read(place(KC_SPLASH_ROI, KC_SPLASH_ORIGIN))
    assert kc_splash.confirm is False and kc_splash.ok is False and kc_splash.reason == "not_present"
    for name in VICTORY_OR_MID:
        rd = end.read(frame(name))
        assert rd.reason != "defeat"
        assert rd.confirm is (rd.reason == "victory")
    assert end.read(frame("end_173922_017.jpg")).reason == "victory"
    assert end.read(frame("end_173922_019.jpg")).reason == "victory"
    wiped = place(HEADER_ROIS[0], HEADER_ORIGIN)
    wiped[30:130, 980:1260] = 0
    missing = end.read(wiped)
    assert missing.confirm is False and missing.ok is False and missing.reason == "not_present"
    wiped_kc = place("defeat_header_kc_030.png", HEADER_ORIGIN)
    wiped_kc[30:130, 980:1260] = 0
    assert end.read(wiped_kc).confirm is False and end.read(wiped_kc).reason == "not_present"
    cut = place(HEADER_ROIS[1], HEADER_ORIGIN)
    cut[40:120, 1155:1200] = 0
    assert end.read(cut).confirm is False and end.read(cut).home is None
    cut_kc = place("defeat_header_kc_030.png", HEADER_ORIGIN)
    cut_kc[40:120, 1080:1120] = 0
    assert end.read(cut_kc).confirm is False and end.read(cut_kc).home is None


def test_blur_does_not_confirm():
    """A sigma-2 blur still looks like the letters and does not agree.

    A clean word sits near 0.45 on the sharpness gate. This blur sits near 0.20.
    """
    end = C.CodEndScreenReader()
    for name in HEADER_ROIS:
        sharp = place(name, HEADER_ORIGIN)
        assert end.read(sharp).reason == "defeat"
        blur = cv2.GaussianBlur(sharp, (0, 0), 2)
        rd = end.read(blur)
        assert rd.confirm is False and rd.ok is False and rd.reason == "not_present", name
    for name in ("end_173922_017.jpg", "end_173922_019.jpg"):
        assert end.read(frame(name)).reason == "victory"
        blur = cv2.GaussianBlur(frame(name), (0, 0), 2)
        rd = end.read(blur)
        assert rd.confirm is False and rd.ok is False and rd.reason == "not_present", name


def test_defeat_does_not_post_a_final_without_scores():
    posted = []
    runner = C.CodSetRunner(posted.append)
    img = place(HEADER_ROIS[1], HEADER_ORIGIN)
    for t in (0, 1000, 4000):
        runner.on_frame(t, img)
    assert posted == []
    assert runner.final is None
    assert runner.stats["posted_final"] == 0
    splash_posts = []
    splash_runner = C.CodSetRunner(splash_posts.append)
    for t in (0, 1500, 4500):
        splash_runner.on_frame(t, place(SPLASH_ROI, SPLASH_ORIGIN))
    assert splash_posts == []
    assert splash_runner.final is None


def test_victory_does_not_post_a_final_without_scores():
    posted = []
    runner = C.CodSetRunner(posted.append)
    img = frame("end_173922_017.jpg")
    for t in (0, 1000, 4000):
        runner.on_frame(t, img)
    assert posted == []
    assert runner.final is None
    assert runner.stats["posted_final"] == 0


def test_hitmarker_popup_and_red_x_presence():
    hit = H.read_hitmarker(frame("kill17_hitmarker.png"))
    assert hit.ok and hit.calibrated and hit.reason == "hitmarker"
    for name in ("v2_redx_chk1.png", "v2_redx_chk2.png"):
        red = H.read_red_x(frame(name))
        assert red.ok and red.calibrated and red.reason == "red_x"
        assert H.read_hitmarker(frame(name)).reason == "red_x"
    popup = H.read_kill_popup(frame("kill17_popup.png"))
    sheet = H.read_kill_popup(frame("v2_numbox_asst1.png"))
    assert popup.ok and popup.calibrated and popup.reason == "xp_plate"
    assert sheet.ok and sheet.calibrated
    assert popup.kills is None and popup.assists is None
    assert sheet.board_kills is None and sheet.board_assists is None
    for name in ("mid_173922_120s.jpg", "end_173922_007.jpg", "end_173922_017.jpg", "sb_002.png"):
        assert H.read_hitmarker(frame(name)).ok is False
        assert H.read_red_x(frame(name)).ok is False
        assert H.read_kill_popup(frame(name)).ok is False


def test_killfeed_icon_presence_and_gameplay_absent():
    icons = H.read_killfeed(frame("v2_kf_icons.png"))
    assert icons.ok and icons.calibrated and icons.reason == "killfeed_icon"
    for name in ("mid_173922_120s.jpg", "end_173922_007.jpg", "end_173922_010.jpg", "end_173922_017.jpg"):
        assert H.read_killfeed(frame(name)).ok is False
    sheet = frame("v2_kf_icons.png")
    piece = sheet[0:100, 90:200]
    canvas = np.zeros((720, 1280, 3), np.uint8)
    # Top-right killfeed box on a 16:9 frame starts near x=819, y=7.
    canvas[10:10 + piece.shape[0], 830:830 + piece.shape[1]] = piece
    placed = H.read_killfeed(canvas, cropped=True)
    assert placed.ok and placed.calibrated and placed.reason == "killfeed_icon"


def test_scoreboard_reads_kills_and_assists(tmp_path):
    for name, (kills, assists) in BOARDS.items():
        rd = H.read_scoreboard(frame(name))
        assert rd.ok and rd.calibrated and rd.reason == "scoreboard_ka"
        assert (rd.board_kills, rd.board_assists) == (kills, assists)
        assert rd.kills is None and rd.assists is None
    im = frame("sb_002.png")
    shifted = np.zeros_like(im)
    shifted[4:, 6:] = im[:-4, :-6]
    moved = H.read_scoreboard(shifted)
    assert (moved.board_kills, moved.board_assists) == (49, 12)
    canvas = np.zeros((720, 1280, 3), np.uint8)
    canvas[400:400 + im.shape[0], 200:200 + im.shape[1]] = im
    pasted = H.read_scoreboard(canvas, cropped=True)
    assert (pasted.board_kills, pasted.board_assists) == (49, 12)
    posted = []
    runner = C.CodSetRunner(posted.append, log_path=tmp_path / "cod_signals.jsonl")
    runner.on_frame(10, im)
    assert posted == []
    row = json.loads((tmp_path / "cod_signals.jsonl").read_text(encoding="utf-8").splitlines()[0])
    board = next(s for s in row["signals"] if s["signal"] == "scoreboard")
    assert board["status"] == C.NOT_COUNTED


def test_missing_or_swapped_crops_stay_not_counted():
    im = frame("sb_002.png")
    wiped = im.copy()
    wiped[50:95, 340:450] = 0
    missing = H.read_scoreboard(wiped)
    assert missing.ok is False and missing.calibrated is False
    assert missing.board_kills is None and missing.board_assists is None
    blank = np.zeros((720, 1280, 3), np.uint8)
    end = C.CodEndScreenReader()
    assert end.read(blank).confirm is False
    assert H.read_hitmarker(blank).ok is False
    assert H.read_kill_popup(blank).ok is False
    assert H.read_killfeed(blank).ok is False
    assert H.read_scoreboard(blank).ok is False
    assert H.read_red_x(blank).ok is False
    assert end.read(frame("sb_002.png")).confirm is False
    assert end.read(frame("kill17_popup.png")).confirm is False
    assert H.read_kill_popup(frame("v2_redx_chk1.png")).ok is False
    assert H.read_scoreboard(frame("end_173922_019.jpg")).ok is False
    hud = {rd.region: rd for rd in C.CodHudReader().read(frame("mid_173922_120s.jpg"))}
    assert hud["pause"].reason == "stub_not_calibrated" and hud["pause"].ok is False
    assert hud["crosshair"].reason == "stub_not_calibrated" and hud["crosshair"].ok is False
    for name in ("hitmarker", "kill_popup", "killfeed", "scoreboard"):
        assert hud[name].ok is False and hud[name].calibrated is False


def test_centered_hitmarker_and_popup_sit_in_the_regions():
    hit = frame("kill17_hitmarker.png")
    canvas = np.zeros((720, 1280, 3), np.uint8)
    # The strokes sit near (210, 210) in the crop. This offset lands them in the center aim box.
    canvas[170:170 + hit.shape[0], 430:430 + hit.shape[1]] = hit
    placed = H.read_hitmarker(canvas, cropped=True)
    assert placed.ok and placed.calibrated and placed.reason == "hitmarker"
    popup = frame("kill17_popup.png")
    canvas = np.zeros((720, 1280, 3), np.uint8)
    canvas[210:210 + popup.shape[0], 400:400 + popup.shape[1]] = popup
    plate = H.read_kill_popup(canvas, cropped=True)
    assert plate.ok and plate.calibrated and plate.reason == "xp_plate"


def test_info_only_timing_stays_not_counted():
    for row in C.INFO_ONLY.values():
        assert row["label"] == "INFO-ONLY"
        assert row["status"] == C.NOT_COUNTED
    assert C.INFO_ONLY["power_aware_timing"]["power"] == "unknown"
