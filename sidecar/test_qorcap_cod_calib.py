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


NORMAL_INK = (235, 235, 235)
HIGHLIGHT_INK = (30, 220, 240)
LONGER_VALUES = ("1250", "1000", "2048", "9999", "100000", "123456")


def _text_pack():
    return json.loads(H.TEXT_PACK_PATH.read_text(encoding="utf-8"))


def _mask(rows):
    mask = np.zeros((len(rows), len(rows[0])), np.uint8)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "1":
                mask[y, x] = 255
    return mask


def _canonical_masks():
    masks = {}
    for item in _text_pack()["digits"]:
        if item.get("canonical"):
            masks[item["label"]] = _mask(item["mask_rows"])
    return masks


def _paint(text, masks, color):
    glyphs = [masks[ch] for ch in text]
    gap = 2
    height = max(g.shape[0] for g in glyphs)
    width = sum(g.shape[1] for g in glyphs) + gap * (len(glyphs) - 1)
    canvas = np.full((max(height + 8, 24), max(width + 8, 24), 3), 12, np.uint8)
    x = 4
    for glyph in glyphs:
        view = canvas[4:4 + glyph.shape[0], x:x + glyph.shape[1]]
        view[glyph > 0] = color
        x += glyph.shape[1] + gap
    return canvas


def _read_painted(text, masks, color):
    img = _paint(text, masks, color)
    return H._row_digit_string(img, 0, img.shape[1], 0, img.shape[0], 130, 1.0)


def test_row_digit_gates_and_consensus_stay_put():
    assert H._ROW_DIGIT_MIN == 0.75 and H._ROW_DIGIT_MARGIN == 0.15
    assert H._OWNER_MIN == 0.80 and H._ROW_HEADER_MIN == 0.70
    assert H.CONSENSUS_FRAMES == 5
    assert H._ROW_GLYPH_MAX == 6


def test_synthetic_digits_are_labeled_and_the_bank_is_text():
    data = _text_pack()
    assert H.TEXT_PACK_PATH.suffix == ".json"
    assert H.TEXT_PACK_PATH.read_bytes()[:1] == b"{"
    shipped = np.load(H.PACK_PATH, allow_pickle=False)
    assert "assists_header_mask" not in shipped.files
    assert "deaths_column_header_mask" not in shipped.files
    labels = {item["label"] for item in data["digits"]}
    assert labels == set("0123456789")
    synthetic = [item for item in data["digits"] if item["provenance"] == "synthetic"]
    assert sorted(item["label"] for item in synthetic) == ["0", "6", "8"]
    for item in synthetic:
        assert item["note"] == "synthetic sample, real-match recall UNMEASURED"
    pack = H._pack()
    assert "assists_header_mask" in pack and "deaths_column_header_mask" in pack
    assert set(H._labels(pack, "row_digit_labels")) >= set("0123456789")


def test_each_stored_digit_reads_in_normal_and_highlighted_ink():
    data = _text_pack()
    for item in data["digits"]:
        masks = {item["label"]: _mask(item["mask_rows"])}
        for color in (NORMAL_INK, HIGHLIGHT_INK):
            assert _read_painted(item["label"], masks, color) == item["label"]


def test_synthetic_sweep_covers_0_to_100_and_every_three_digit_value():
    masks = _canonical_masks()
    assert set(masks) == set("0123456789")
    for color in (NORMAL_INK, HIGHLIGHT_INK):
        for number in range(0, 101):
            assert _read_painted(str(number), masks, color) == str(number)
        for number in range(100, 1000):
            assert _read_painted(str(number), masks, color) == str(number)
        for text in LONGER_VALUES:
            assert _read_painted(text, masks, color) == text
        assert _read_painted("1234567", masks, color) is None


def test_headers_do_not_cross_map_and_old_row_stays_13():
    board = frame("end_173922_019.jpg")
    row = H.read_owner_row(board)
    assert (row.kills, row.deaths, row.assists, row.confirms) == (13, 13, None, 7)
    pack = H._pack()
    assists = pack["assists_header_mask"]
    deaths_col = pack["deaths_column_header_mask"]
    others = (
        pack["kills_header_mask"],
        pack["confirms_header_mask"],
        pack["deaths_header_mask"],
        deaths_col,
    )
    for mask in others:
        painted = np.zeros((48, 96), np.uint8)
        painted[8:8 + mask.shape[0], 8:8 + mask.shape[1]] = mask
        hit = H._best_mask(painted, assists, 1.0, 0.0)
        score = 0.0 if hit is None else hit[0]
        if mask is deaths_col:
            assert score < H._ROW_HEADER_MIN
        else:
            assert score < H._ROW_HEADER_MIN
    painted = np.zeros((48, 96), np.uint8)
    painted[8:8 + assists.shape[0], 8:8 + assists.shape[1]] = assists
    hit = H._best_mask(painted, pack["kills_header_mask"], 1.0, 0.0)
    assert hit is None or hit[0] < H._ROW_HEADER_MIN
    hit = H._best_mask(painted, pack["deaths_header_mask"], 1.0, 0.0)
    assert hit is None or hit[0] < H._ROW_HEADER_MIN


def _stamp(canvas, mask, x, y, color):
    view = canvas[y:y + mask.shape[0], x:x + mask.shape[1]]
    view[mask > 0] = color


def _synthetic_tdm_board(
    kills_header_x=420,
    deaths_header_x=560,
    kills_x=432,
    deaths_digits=((566, "2"), (574, "1")),
):
    """Synthetic 720p board. Shipped name and KILLS masks, text ASSISTS and DEATHS masks.

    Digits are the committed canonical masks. This is not a captured frame.
    Eliminations 11 sit below the row and are not a kill column.
    Default placement is K9 under KILLS and D21 under the TDM DEATHS column.
    """
    pack = H._pack()
    masks = _canonical_masks()
    board = np.zeros((720, 1280, 3), np.uint8)
    _stamp(board, pack["owner_mask"], 172, 534, (255, 255, 255))
    _stamp(board, pack["kills_header_mask"], kills_header_x, 400, (255, 255, 255))
    _stamp(board, pack["assists_header_mask"], 484, 403, (255, 255, 255))
    _stamp(board, pack["deaths_column_header_mask"], deaths_header_x, 403, (255, 255, 255))
    _stamp(board, masks["9"], kills_x, 536, HIGHLIGHT_INK)
    _stamp(board, masks["2"], 496, 536, HIGHLIGHT_INK)
    for x, label in deaths_digits:
        _stamp(board, masks[label], x, 536, HIGHLIGHT_INK)
    _stamp(board, masks["1"], 760, 640, HIGHLIGHT_INK)
    _stamp(board, masks["1"], 766, 640, HIGHLIGHT_INK)
    return board


def _header_pair(board):
    scale = H._board_scale(board)
    pack = H._pack()
    name = H._best_mask(
        H._value_mask(board, H._OWNER_SEARCH_VALUE), pack["owner_mask"], scale, H._OWNER_MIN)
    assert name is not None
    _score, row_x, row_y, _row_w, row_h = name
    kills_h, deaths_h, _assists_h, _confirms_h, _personal = H._owner_row_header_hits(
        board, scale, pack, row_x, row_y, row_h)
    return kills_h, deaths_h


def _mask_on_region_score(region, templ, scale):
    """NCC of ``templ`` on ``region`` after the reader's own scale. None is a miss."""
    region_s = H._scaled_u8(region, scale)
    templ_s = H._scaled_u8(templ, scale)
    pad_y = max(4, int(templ_s.shape[0]))
    pad_x = max(4, int(templ_s.shape[1]))
    canvas = np.zeros((region_s.shape[0] + 2 * pad_y, region_s.shape[1] + 2 * pad_x), np.uint8)
    canvas[pad_y:pad_y + region_s.shape[0], pad_x:pad_x + region_s.shape[1]] = region_s
    hit = H._best_mask(canvas, templ_s, 1.0, 0.0)
    return 0.0 if hit is None else hit[0]


_HEADER_CROSS_MARGIN = 0.10


def test_deaths_column_header_does_not_match_the_kills_header():
    """DEATHS column ink is not the KILLS header. Same x is not both stats.

    end_173922_019 is 13 and 13, so a column swap would still look right.
    The cross score stays under the header gate by a margin, and the deaths
    hit the reader keeps is never the kills hit.
    """
    pack = H._pack()
    kills = pack["kills_header_mask"]
    deaths = pack["deaths_column_header_mask"]
    assert _mask_on_region_score(kills, kills, 1.0) >= H._ROW_HEADER_MIN
    for scale in (1.0, 1.5):
        onto_kills = _mask_on_region_score(kills, deaths, scale)
        onto_deaths = _mask_on_region_score(deaths, kills, scale)
        assert onto_kills < H._ROW_HEADER_MIN - _HEADER_CROSS_MARGIN
        assert onto_deaths < H._ROW_HEADER_MIN - _HEADER_CROSS_MARGIN
    real = frame("end_173922_019.jpg")
    region = H._value_mask(real, H._HEADER_SEARCH_VALUE)[400:417, 427:456]
    assert _mask_on_region_score(region, deaths, 1.0) < H._ROW_HEADER_MIN - _HEADER_CROSS_MARGIN
    for board in (
        real,
        _synthetic_tdm_board(),
        _synthetic_tdm_board(
            kills_header_x=560,
            deaths_header_x=420,
            kills_x=572,
            deaths_digits=((428, "2"), (436, "1")),
        ),
    ):
        kills_h, deaths_h = _header_pair(board)
        assert kills_h is not None
        if deaths_h is not None:
            assert not H._columns_share_x(deaths_h, kills_h, H._board_scale(board))
            assert deaths_h[1] != kills_h[1]


def test_swapped_tdm_columns_keep_k9_and_d21():
    """Synthetic board with the two headers exchanged. Kills stay 9, deaths stay 21."""
    board = _synthetic_tdm_board(
        kills_header_x=560,
        deaths_header_x=420,
        kills_x=572,
        deaths_digits=((428, "2"), (436, "1")),
    )
    row = H.read_owner_row(board)
    assert row.ok and (row.kills, row.assists, row.deaths) == (9, 2, 21)
    assert row.kills != row.deaths
    kills_h, deaths_h = _header_pair(board)
    assert deaths_h is not None and deaths_h[1] < kills_h[1]


def test_tdm_owner_row_is_k9_a2_d21_and_not_the_other_columns():
    board = _synthetic_tdm_board()
    row = H.read_owner_row(board)
    assert row.ok and row.player_tag == "Qortrola30"
    assert (row.kills, row.assists, row.deaths) == (9, 2, 21)
    assert row.confirms is None
    assert row.kills != 11
    wiped = board.copy()
    wiped[534:550, 172:240] = 0
    gone = H.read_owner_row(wiped)
    assert gone.ok is False and gone.kills is None and gone.player_tag is None
    no_kills = board.copy()
    no_kills[532:548, 428:442] = 0
    assert H.read_owner_row(no_kills).ok is False
    no_assists = board.copy()
    no_assists[532:548, 490:508] = 0
    assert H.read_owner_row(no_assists).ok is False
    no_deaths = board.copy()
    no_deaths[532:548, 560:590] = 0
    missed = H.read_owner_row(no_deaths)
    assert missed.ok is False and missed.deaths is None


def _glyph_boxes(text, masks):
    gap = 2
    x = 4
    boxes = []
    for ch in text:
        glyph = masks[ch]
        boxes.append((x, 4, int(glyph.shape[1]), int(glyph.shape[0])))
        x += int(glyph.shape[1]) + gap
    return boxes


def _paint_speck(text, masks, color, x, y, width, height):
    img = _paint(text, masks, color)
    img[y:y + height, x:x + width] = color
    return img


def test_a_speck_does_not_change_a_multi_digit_row_value():
    """A few pixels beside, between, or just above or below the glyphs.

    Area stays in 3..7. The read is the real value, or it abstains.
    A speck that only sits beside the ink still reads the real value.
    """
    masks = _canonical_masks()
    for color in (NORMAL_INK, HIGHLIGHT_INK):
        for text in ("13", "100", "1250"):
            boxes = _glyph_boxes(text, masks)
            x0, y0, w0, h0 = boxes[0]
            wide = max(boxes, key=lambda box: box[2])
            xw, yw, ww, hw = wide
            x1, y1, w1, h1 = boxes[-1]
            # Beside the value, one pixel clear of the ink so it is its own speck.
            # Between: overlaps the first glyph and reaches the next one.
            # Above and below: on a wide glyph, inside its x span, one pixel clear.
            placements = (
                (x0 - 3, y0 + 1, 2, 3),
                (x1 + w1 + 1, y1 + 1, 2, 2),
                (x0 + w0 - 2, y0 + h0 + 1, 5, 1),
                (xw + 1, max(0, yw - 3), 2, 2),
                (xw + 1, yw + hw + 1, 2, 2),
            )
            for sx, sy, sw, sh in placements:
                assert 3 <= sw * sh <= 7
                assert sx >= 0 and sy >= 0
                img = _paint_speck(text, masks, color, sx, sy, sw, sh)
                assert H._row_digit_string(img, 0, img.shape[1], 0, img.shape[0], 130, 1.0) == text


def test_split_glyph_recovers_and_a_cut_glyph_abstains():
    """A gap inside a full 2 is joined from the pixels that are there.

    The six-row deaths piece measured on the last private frame is not a
    sample and is not added to the bank. Alone, it stays unread.
    """
    full = _canonical_masks()["2"]
    assert full.shape[0] == 9
    top = full[:6]
    bottom = full[6:]
    img = np.full((32, 32, 3), 12, np.uint8)
    img[4:10, 8:8 + top.shape[1]][top > 0] = NORMAL_INK
    img[11:11 + bottom.shape[0], 8:8 + bottom.shape[1]][bottom > 0] = NORMAL_INK
    assert H._row_digit_string(img, 0, 32, 0, 32, 130, 1.0) == "2"
    cut = np.full((32, 32, 3), 12, np.uint8)
    cut[4:10, 8:8 + top.shape[1]][top > 0] = NORMAL_INK
    assert H._row_digit_string(cut, 0, 32, 0, 32, 130, 1.0) is None
    truncated = _mask(["0110", "1001", "0001", "0001", "0010", "0110"])
    alone = np.full((32, 32, 3), 12, np.uint8)
    alone[4:10, 8:12][truncated > 0] = NORMAL_INK
    assert H._row_digit_string(alone, 0, 32, 0, 32, 130, 1.0) is None


def test_rebuild_writes_a_temporary_pack_only(tmp_path):
    before = H.PACK_PATH.read_bytes()
    dest = tmp_path / "cod_mw4_hud_v1.npz"
    H.build_calibration_pack(out_path=dest)
    assert dest.is_file()
    assert H.PACK_PATH.read_bytes() == before
    raw = np.load(dest, allow_pickle=False)
    assert "assists_header_mask" not in raw.files


def test_owner_row_not_read_names_the_step_that_stopped_it():
    """Synthetic board only. Each missing piece names its own step; a full row has no step.

    The step is diagnostic. It is not a score, it does not loosen any gate, and the
    reason stays ``not_present`` as before.
    """
    board = _synthetic_tdm_board()
    row = H.read_owner_row(board)
    assert row.ok and row.stage is None
    cases = {
        "tag_not_matched": (534, 550, 172, 240),
        "kills_not_read": (532, 548, 428, 442),
        "assists_not_read": (532, 548, 490, 508),
        "deaths_not_read": (532, 548, 560, 590),
    }
    for stage, (y0, y1, x0, x1) in cases.items():
        cut = board.copy()
        cut[y0:y1, x0:x1] = 0
        miss = H.read_owner_row(cut)
        assert miss.ok is False and miss.calibrated is False, stage
        assert miss.reason == "not_present", stage
        assert miss.stage == stage, (stage, miss.stage)
        assert miss.kills is None and miss.deaths is None and miss.player_tag is None
    no_header = board.copy()
    pack = H._pack()
    kh, kw = pack["kills_header_mask"].shape
    no_header[400:400 + kh, 420:420 + kw] = 0
    assert H.read_owner_row(no_header).stage == "kills_header_not_found"
    strip = np.zeros((40, 1280, 3), np.uint8)
    assert H.read_owner_row(strip).stage == "board_scale"


def test_thinner_owner_tag_stays_not_read_and_says_so():
    """A synthetic tag drawn thinner than the shipped mask stays under the tag gate.

    The gate is unchanged. The read names the tag step, so a missed row
    can be told apart from a missed kill or death digit.
    """
    board = _synthetic_tdm_board()
    pack = H._pack()
    thin = cv2.erode(pack["owner_mask"], np.ones((1, 2), np.uint8))
    board[534:550, 172:238] = 0
    _stamp(board, thin, 172, 534, (255, 255, 255))
    hit = H._best_mask(H._value_mask(board, H._OWNER_SEARCH_VALUE), pack["owner_mask"], 1.0, 0.0)
    assert hit is not None and hit[0] < H._OWNER_MIN
    assert H._OWNER_MIN == 0.80
    miss = H.read_owner_row(board)
    assert miss.ok is False and miss.stage == "tag_not_matched"
