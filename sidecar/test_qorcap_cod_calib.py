"""Calibrated Call of Duty reads on Con's private practice frames.

A present signal is ok and calibrated. An empty or wrong crop stays not read.
VICTORY and DEFEAT agree as an end screen and do not invent home and away.
They post a final only when Qortrola30's own scoreboard row is readable on
the same frames for 3 timestamps across 3 seconds. DEFEAT does not agree on
a VICTORY frame, and VICTORY does not agree on a DEFEAT frame. Scoreboard
K/A is read when the digits are there, and the comparison stays not Counted
without an offline count. That K/A row is not the owner row.
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
HEADER_ROIS = (
    "defeat_header_030.png",
    "defeat_header_031.png",
    "defeat_header_033.png",
)
HEADER_ORIGIN = H.DEFEAT_HEADER_ORIGIN
# Kill Confirmed word crop. Thinner letters, same gates. No scoreboard.
KC_HEADER = H.DEFEAT_KC_ROI
KC_ORIGIN = H.DEFEAT_KC_ORIGIN
# The glitched splash. It stays in the tree as a negative: the letters do not clear the header.
SPLASH_ROI = "defeat_splash_028.png"
SPLASH_ORIGIN = (340, 250)
VICTORY_OR_MID = (
    "end_173922_017.jpg",
    "end_173922_019.jpg",
    "end_173922_007.jpg",
    "end_173922_010.jpg",
    "mid_173922_120s.jpg",
)


def place(name: str, origin: tuple[int, int], base=None):
    roi = frame(name)
    canvas = np.zeros((720, 1280, 3), np.uint8) if base is None else base.copy()
    x, y = origin
    h, w = roi.shape[:2]
    canvas[y:y + h, x:x + w] = roi
    return canvas


def test_defeat_confirms_and_does_not_cross_victory():
    """DEFEAT agrees on the top-right header crops.

    Home and away stay unread. The Kill Confirmed header agrees on the same
    gates after its split letters are joined. The glitched splash does not
    clear the header letters, so it does not agree. A VICTORY or mid-match
    frame does not agree as DEFEAT, and a DEFEAT frame does not agree as VICTORY.
    """
    end = C.CodEndScreenReader()
    for name in HEADER_ROIS:
        rd = end.read(place(name, HEADER_ORIGIN))
        assert rd.ok and rd.calibrated and rd.confirm, name
        assert rd.reason == "defeat"
        assert rd.home is None and rd.away is None
    kc = end.read(place(KC_HEADER, KC_ORIGIN))
    assert kc.ok and kc.calibrated and kc.confirm
    assert kc.reason == "defeat"
    assert kc.home is None and kc.away is None
    splash = end.read(place(SPLASH_ROI, SPLASH_ORIGIN))
    assert splash.confirm is False and splash.ok is False and splash.reason == "not_present"
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
    cut = place(HEADER_ROIS[1], HEADER_ORIGIN)
    cut[40:120, 1155:1200] = 0
    assert end.read(cut).confirm is False and end.read(cut).home is None


def test_blur_does_not_confirm():
    """A sigma-2 blur still looks like the letters and does not agree.

    A clean word sits near 0.45 on the sharpness gate. This blur sits near 0.20.
    A one-pixel blur of the splash, and that splash scaled from 720p up to
    1080p, still agree. A sigma-2 blur of the scaled splash does not. The
    letter score is not what rejects the blur.
    """
    end = C.CodEndScreenReader()
    samples = [(name, HEADER_ORIGIN) for name in HEADER_ROIS]
    samples.append((KC_HEADER, KC_ORIGIN))
    for name, origin in samples:
        sharp = place(name, origin)
        assert end.read(sharp).reason == "defeat", name
        blur = cv2.GaussianBlur(sharp, (0, 0), 2)
        rd = end.read(blur)
        assert rd.confirm is False and rd.ok is False and rd.reason == "not_present", name
    splash = frame("end_173922_017.jpg")
    header = frame("end_173922_019.jpg")
    assert end.read(splash).reason == "victory"
    assert end.read(header).reason == "victory"
    assert end.read(cv2.GaussianBlur(splash, (0, 0), 1)).reason == "victory"
    up = cv2.resize(splash, (1920, 1080), interpolation=cv2.INTER_LINEAR)
    scaled = end.read(up)
    assert scaled.ok and scaled.calibrated and scaled.confirm and scaled.reason == "victory"
    for img, name in ((splash, "splash"), (header, "header"), (up, "upscaled")):
        blur = cv2.GaussianBlur(img, (0, 0), 2)
        rd = end.read(blur)
        assert rd.confirm is False and rd.ok is False and rd.reason == "not_present", name
    mid = end.read(frame("mid_173922_120s.jpg"))
    assert mid.confirm is False and mid.ok is False and mid.reason == "not_present"
    defeat = end.read(place(HEADER_ROIS[0], HEADER_ORIGIN))
    assert defeat.reason == "defeat"
    assert end.read(splash).reason != "defeat"


def test_defeat_does_not_post_a_final_without_scores():
    posted = []
    runner = C.CodSetRunner(posted.append, side="home")
    img = place(HEADER_ROIS[1], HEADER_ORIGIN)
    for t in (0, 1000, 4000):
        runner.on_frame(t, img)
    assert posted == []
    assert runner.final is None
    assert runner.stats["posted_final"] == 0
    assert runner.stats["end_direction_mismatch"] == 0
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
    assert "stick_turn_vs_camera" not in C.INFO_ONLY
    assert C.COUNTED_SIGNALS["stick_turn_vs_camera"]["status"] == "Counted"


# Pastes that agreed before the location gate. None of these is the header box.
_OFF_HEADER = ((40, 8), (500, 250), (980, 300), (40, 400), (819, 7))


def _not_read(rd, name):
    assert rd.confirm is False and rd.ok is False and rd.calibrated is False, name
    assert rd.reason == "not_present", name
    assert rd.home is None and rd.away is None


def test_word_outside_its_box_is_not_read():
    """A word pasted off its box stays not read, including on a mid-match frame.

    The header at its measured corner still agrees. The same crop on the
    killfeed side of that box, or lower in the wide end-screen crop, does not.
    """
    end = C.CodEndScreenReader()
    mid = frame("mid_173922_120s.jpg")
    for origin in _OFF_HEADER:
        _not_read(end.read(place(HEADER_ROIS[0], origin)), f"blank {origin}")
        _not_read(end.read(place(HEADER_ROIS[0], origin, mid)), f"mid {origin}")
    held = end.read(place(HEADER_ROIS[0], HEADER_ORIGIN))
    assert held.reason == "defeat" and held.confirm is True
    # VICTORY header letters, lifted off end_173922_019 and pasted elsewhere.
    src = frame("end_173922_019.jpg")
    word = src[30:130, 40:300]
    home = np.zeros((720, 1280, 3), np.uint8)
    home[30:30 + word.shape[0], 40:40 + word.shape[1]] = word
    assert end.read(home).reason == "victory"
    for origin in ((980, 40), (500, 300), (40, 400), (700, 80)):
        canvas = np.zeros((720, 1280, 3), np.uint8)
        canvas[origin[1]:origin[1] + word.shape[0], origin[0]:origin[0] + word.shape[1]] = word
        _not_read(end.read(canvas), f"victory {origin}")
        on_mid = mid.copy()
        on_mid[origin[1]:origin[1] + word.shape[0], origin[0]:origin[0] + word.shape[1]] = word
        _not_read(end.read(on_mid), f"victory mid {origin}")


def test_both_words_on_one_frame_are_ambiguous():
    """VICTORY and DEFEAT are both evaluated. Both agreeing is not a win."""
    end = C.CodEndScreenReader()
    roi = frame(HEADER_ROIS[0])
    x, y = HEADER_ORIGIN
    for name in ("end_173922_017.jpg", "end_173922_019.jpg"):
        both = frame(name)
        both = both.copy()
        both[y:y + roi.shape[0], x:x + roi.shape[1]] = roi
        rd = end.read(both)
        assert rd.reason == "ambiguous_result", name
        assert rd.confirm is False and rd.ok is False and rd.calibrated is False
        assert rd.home is None and rd.away is None
    assert end.read(frame("end_173922_017.jpg")).reason == "victory"
    assert end.read(place(HEADER_ROIS[0], HEADER_ORIGIN)).reason == "defeat"


def _hue(img, degrees: float):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    shift = int(round(degrees / 2.0))
    hsv[:, :, 0] = ((hsv[:, :, 0].astype(np.int16) + shift) % 180).astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _gray(img):
    return cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


def _jpeg(img, quality: int):
    _ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def test_hue_gray_and_jpeg_treat_both_words_alike():
    """The shared field and letter gates. Clean frames and JPEG 25 still agree.

    A grayscale frame and a 60 degree hue shift do not agree for either word.
    Sigma 2 drops both. The splash still drops earlier under a lighter blur
    than the DEFEAT header; that stroke-width gap is still open.
    """
    end = C.CodEndScreenReader()
    samples = {
        "victory-splash": frame("end_173922_017.jpg"),
        "victory-header": frame("end_173922_019.jpg"),
        "defeat": place(HEADER_ROIS[0], HEADER_ORIGIN),
        "defeat-031": place(HEADER_ROIS[1], HEADER_ORIGIN),
    }
    for name, img in samples.items():
        clean = end.read(img)
        assert clean.confirm is True and clean.reason in ("victory", "defeat"), name
        _not_read(end.read(_gray(img)), f"gray {name}")
        _not_read(end.read(_hue(img, 60)), f"hue {name}")
        _not_read(end.read(cv2.GaussianBlur(img, (0, 0), 2)), f"blur {name}")
        jpeg = end.read(_jpeg(img, 25))
        assert jpeg.confirm is True and jpeg.reason == clean.reason, name
    assert H._LETTER_MIN == 0.85 and H._LETTER_MEAN == 0.90 and H._LETTER_MARGIN == 0.08
    assert H._FIELD_MIN == 0.20 and H._SHARP_MIN == 0.26


def test_same_owner_tag_is_case_insensitive():
    assert C.same_owner_tag("Qortrola30")
    assert C.same_owner_tag("qortrola30")
    assert C.same_owner_tag("QORTROLA30")
    assert C.same_owner_tag(" Qortrola30 ")
    assert C.same_owner_tag("Qortrola31") is False
    assert C.same_owner_tag("") is False
    assert C.same_owner_tag(None) is False


def test_owner_row_reads_qortrola30_kills_and_deaths():
    """The end scoreboard row is Con's. Kills and deaths are read. Assists are not on this board.

    Confirms are not assists. Home and away stay empty. A strip with no gamertag,
    the splash, and a wiped name or wiped kill digits stay not read.
    """
    board = frame("end_173922_019.jpg")
    row = H.read_owner_row(board)
    assert row.ok and row.calibrated and row.reason == "owner_row"
    assert C.same_owner_tag(row.player_tag)
    assert row.player_tag == "Qortrola30"
    assert row.kills == 13 and row.deaths == 13
    assert row.assists is None and row.confirms == 7
    assert row.home is None and row.away is None
    end = C.CodEndScreenReader().read(board)
    assert end.reason == "victory" and end.confirm is True
    assert end.home is None and end.away is None
    shifted = np.zeros_like(board)
    shifted[4:, 6:] = board[:-4, :-6]
    moved = H.read_owner_row(shifted)
    assert moved.ok and (moved.kills, moved.deaths, moved.confirms) == (13, 13, 7)
    jpeg = cv2.imdecode(cv2.imencode(".jpg", board, [int(cv2.IMWRITE_JPEG_QUALITY), 40])[1], cv2.IMREAD_COLOR)
    again = H.read_owner_row(jpeg)
    assert again.ok and (again.kills, again.deaths) == (13, 13)
    up = cv2.resize(board, (1920, 1080), interpolation=cv2.INTER_LINEAR)
    scaled = H.read_owner_row(up)
    assert scaled.ok and (scaled.kills, scaled.deaths, scaled.assists, scaled.confirms) == (13, 13, None, 7)
    for name in ("end_173922_017.jpg", "mid_173922_120s.jpg", "sb_001.png", "sb_002.png", "sb_003.png", "end_173922_007.jpg"):
        missed = H.read_owner_row(frame(name))
        assert missed.ok is False and missed.calibrated is False, name
        assert missed.kills is None and missed.deaths is None and missed.player_tag is None
    wiped = board.copy()
    wiped[528:556, 160:250] = 0
    gone = H.read_owner_row(wiped)
    assert gone.ok is False and gone.player_tag is None and gone.kills is None
    no_kills = board.copy()
    no_kills[530:552, 420:460] = 0
    blank_k = H.read_owner_row(no_kills)
    assert blank_k.ok is False and blank_k.kills is None and blank_k.deaths is None
    blur = H.read_owner_row(cv2.GaussianBlur(board, (0, 0), 1.2))
    assert blur.ok is False and blur.kills is None


def test_owner_row_and_victory_reach_the_three_frame_bar(tmp_path):
    """Word plus the owner row, held for 3 timestamps across 3 seconds, posts.

    Home and away stay unread. The receipt path keeps null scores and winner
    unknown. The row is logged and copied onto the session sidecar.
    """
    img = frame("end_173922_019.jpg")
    repeated = []
    same = C.CodSetRunner(repeated.append)
    for t in (0, 1000, 2000, 3000, 4000):
        same.on_frame(t, img)
    assert repeated == []
    posted = []
    runner = C.CodSetRunner(posted.append, log_path=tmp_path / "cod_signals.jsonl")

    def variant(n):
        out = img.copy()
        out[0, n] = (n * 40) % 256
        return out

    for n, t in enumerate((0, 1000, 2000, 3000)):
        runner.on_frame(t, variant(n), source={"frame_id": f"f{n}", "seq": n, "captured_us": t * 1000})
        assert posted == []
    runner.on_frame(4000, variant(4), source={"frame_id": "f4", "seq": 4, "captured_us": 4_000_000})
    assert len(posted) == 1
    event = posted[0]
    assert event["basis"] == "end_menu" and event["method"] == "end_menu_read"
    assert event["final_confirmed"] is False
    assert event["home"] is None and event["away"] is None
    assert event["quarter"] == 4
    assert event["owner_row"]["tag"] == "Qortrola30"
    assert event["owner_row"]["kills"] == 13 and event["owner_row"]["deaths"] == 13
    assert event["owner_row"]["assists"] is None and event["owner_row"]["confirms"] == 7
    assert event["owner_row"]["result"] == "victory"
    text = " ".join(event["notes"]).lower()
    assert "qortrola30" in text and "victory" in text
    assert "verified" not in text and "cheat" not in text
    assert "not a home or away score" in text
    lines = (tmp_path / "cod_signals.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "set"
    end_row = json.loads(lines[-1])
    assert end_row["kind"] == "end_screen" and end_row["owner_row"]["kills"] == 13
    from qorcap_match import apply_final
    result = apply_final({"notes": []}, event)
    assert result["home_score"] is None and result["away_score"] is None
    assert result["winner"] == "unknown"
    assert result["final_confirmed"] is False
    confirmed = []
    ready = C.CodSetRunner(confirmed.append)
    ready.set_signals(
        [{"type": "in", "t_us": 0, "b": 1 << 9}, {"type": "in", "t_us": 2_000_000, "b": 0}],
        [], [], 0)
    ready.menu_exit_us = 2_100_000
    ready.lag_us_override = 100_000
    ready.capture_epoch = 1
    ready.capture_generation = 1
    ready.session_id = "s1"
    ready.seal_durable = True
    ready.anchor_validated = True
    ready.independent_anchor = "independent-v1:" + ("ab" * 32)
    for n, t in enumerate((0, 1000, 2000, 3000, 4000)):
        ready.on_frame(t, variant(n + 5), source={
            "frame_id": f"g{n}",
            "capture_id": f"g{n}",
            "seq": n,
            "epoch": 1,
            "clock": "capture_pts",
            "mono_us": 3_000_000 + n * 1_000,
            "captured_us": 3_000_000 + n * 1_000,
        })
    assert confirmed[0]["final_confirmed"] is False
    assert result["method"] == "end_menu_read"
    assert "owner_row" not in result
    C.write_session(tmp_path)
    session = json.loads((tmp_path / "cod_session.json").read_text(encoding="utf-8"))
    assert session["owner_row"]["tag"] == "Qortrola30"
    assert session["owner_row"]["kills"] == 13 and session["owner_row"]["deaths"] == 13
    assert "not a receipt field" in session["note"]
    assert "not a verification verdict" in session["owner_row"]["note"].lower()


def test_owner_row_fails_closed_and_the_probe_keeps_the_last_five_frames(tmp_path):
    """No row, one frame, or five frames inside a second do not post.

    The probe looks only at the last five frames. Earlier agreeing frames
    outside that tail do not post a final.
    """
    board = frame("end_173922_019.jpg")
    splash = frame("end_173922_017.jpg")
    posted = []
    runner = C.CodSetRunner(posted.append)
    for t in (0, 1500, 3000):
        runner.on_frame(t, splash)
    assert posted == [] and runner.final is None
    one = []
    C.CodSetRunner(one.append).on_frame(0, board)
    assert one == []
    tight = []
    runner = C.CodSetRunner(tight.append)
    for t in (0, 200, 400, 600, 800):
        runner.on_frame(t, board)
    assert tight == [] and runner.final is None
    # Three early agreeing frames, then five frames with no row. The probe
    # keeps the tail, so the early agreement does not post.
    frames = [(0, board), (1500, board), (3000, board)]
    frames += [(4000 + i * 200, splash) for i in range(5)]
    probe = C.probe_end_frames(frames)
    assert probe["frames"] == 5
    assert probe["posted"] is False
    assert probe["reason"] == "the owner row was not read on the last frames"
    assert all(row["owner_row"] is None for row in probe["reads"])
    held = [(i * 1000, board) for i in range(5)]
    repeated_probe = C.probe_end_frames(held, own_kills=13)
    assert repeated_probe["posted"] is False
    assert repeated_probe["reason"] == "the last frames are not five distinct captures"
    distinct = []
    for i in range(5):
        out = board.copy()
        out[0, i] = (i * 40) % 256
        distinct.append((i * 1000, out, {"frame_id": f"p{i}", "seq": i, "captured_us": i * 1_000_000}))
    posted_probe = C.probe_end_frames(distinct, own_kills=13)
    assert posted_probe["posted"] is True
    assert posted_probe["frames"] == 5
    assert posted_probe["reads"][-1]["result"] == "victory"
    assert posted_probe["reads"][-1]["owner_row"]["deaths"] == 13
    C.record_end_probe(tmp_path, distinct, own_kills=13)
    saved = json.loads((tmp_path / "cod_end_probe.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "qorcap-cod-end-probe/v0"
    assert saved["posted"] is True
    assert "not a verification verdict" in saved["note"].lower()
    blob = json.dumps(saved)
    assert "Qortrola30" in blob
    tags = [row.get("owner_row", {}).get("tag") for row in saved["reads"] if row.get("owner_row")]
    assert tags and set(tags) == {"Qortrola30"}
