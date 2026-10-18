"""Monday Night (primetime) scorebug profile + auto profile selection (v0.3.5). Frames: match-20261016-170000."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
import qorcap_mnp as M  # noqa: E402
from qorcap_reader import madden_final_screen, prep  # noqa: E402

FX = HERE / "fixtures"


def img(name):
    return cv2.imread(str(FX / name))


@pytest.fixture(scope="module")
def mnp():
    return M.MondayNightReader()


@pytest.mark.parametrize("name,away,home,q,clock,red", [
    ("m27_mnp_q1.jpg", 0, 0, 1, "4:07", False),
    ("m27_mnp_q2.jpg", 14, 13, 2, "2:27", False),
    ("m27_mnp_q4_final.jpg", 34, 29, 4, "0:00", True),   # red final-minute clock, touching "00"
])
def test_monday_night_reads(mnp, name, away, home, q, clock, red):
    fr = mnp.read(img(name))
    assert fr.ok, fr.reason
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) == (away, home, q, clock, red)
    assert fr.profile_id == "madden27_monday_night"


def test_half_resolution_frame_still_reads_scores(mnp):
    fr = mnp.read(cv2.resize(img("m27_mnp_q2.jpg"), (640, 360), interpolation=cv2.INTER_AREA))
    assert fr.ok is False or (fr.left_score, fr.right_score) == (14, 13)  # never a wrong score


@pytest.mark.parametrize("name", ["m27_end_menu_finish.jpg", "m27_pause_menu.jpg", "m27_mnp_end_menu_online.jpg"])
def test_not_monday_night_is_layout_unknown(mnp, name):
    assert mnp.read(img(name)).reason == "layout_unknown"


def test_broncos_orange_endzone_is_not_monday_night(mnp):
    """match-20261017-091651 OT walk-off: the logo box is ~99% orange. A real Monday Night mark is ~0.06."""
    assert mnp.read(img("m27_tnp_091651_ot_10_16.jpg")).reason == "layout_unknown"
    for name in ("m27_mnp_q1.jpg", "m27_mnp_q2.jpg", "m27_mnp_q4_final.jpg"):
        assert M.LOGO_MIN_ORANGE <= M.logo_orange(M.bar_hsv(img(name))) <= M.LOGO_MAX_ORANGE


def test_pack_provenance(mnp):
    assert mnp.source["score:5"].startswith("synthetic")
    assert all(mnp.source[f"score:{d}"].startswith("real") for d in "012346789")
    assert set(mnp.groups["quarter"][0]) == {"1ST", "2ND", "3RD", "4TH"}


class Fixed:
    def __init__(self, reason, profile_id, ok=False):
        self.reason, self.profile_id, self.ok = reason, profile_id, ok
        self.calls = 0

    def read(self, im):
        self.calls += 1
        return NS(ok=self.ok, reason=self.reason, profile_id=self.profile_id)


def test_auto_prefers_standard_and_keeps_it():
    std, mn = Fixed("ok", "madden27_standard", ok=True), Fixed("layout_unknown", "madden27_monday_night")
    a = M.AutoProfileReader(std, [mn])
    for _ in range(3):
        assert a.read(None).profile_id == "madden27_standard"
    assert mn.calls == 0 and a.active_profile == "madden27_standard"


def test_auto_falls_back_to_monday_night_and_sticks():
    std, mn = Fixed("layout_unknown", "madden27_standard"), Fixed("ok", "madden27_monday_night", ok=True)
    a = M.AutoProfileReader(std, [mn])
    assert a.read(None).profile_id == "madden27_monday_night"
    std.calls = 0
    a.read(None)
    assert std.calls == 0  # active profile tried first
    assert a.active_profile == "madden27_monday_night" and a.profile_counts == {"madden27_monday_night": 2}


def test_auto_standard_misread_is_not_masked():
    std, mn = Fixed("score_unsure", "madden27_standard"), Fixed("ok", "madden27_monday_night", ok=True)
    assert M.AutoProfileReader(std, [mn]).read(None).reason == "score_unsure"  # standard layout seen: no fallback


def test_auto_with_real_readers():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    a = M.AutoProfileReader(ScorebugReader(), prep=prep)
    fr = a.read(img("m27_mnp_q2.jpg"))
    assert fr.ok and (fr.left_score, fr.right_score) == (14, 13) and a.active_profile == "madden27_monday_night"
    assert a.read(img("m27_end_menu_finish.jpg")).reason == "layout_unknown"


def test_online_end_menu_is_final_screen_and_fails_closed_on_occluded_away():
    from qorcap_endmenu import EndMenuReader
    im = img("m27_mnp_end_menu_online.jpg")
    assert madden_final_screen(prep(im))  # gamertag banners no longer hide the end menu
    rd = EndMenuReader().read(im)
    assert not rd.ok and rd.reason.startswith("away_")  # name tags cover the away score: never guessed


def test_end_menu_real_9_template_still_xchecked():
    from qorcap_endmenu import EndMenuReader, side_glyphs
    r, im = EndMenuReader(), prep(img("m27_mnp_end_menu_online.jpg"))
    d2, d9 = (r._digit(g) for g in side_glyphs(im, "home"))
    assert d2[0] == "2"
    assert d9[1]["label"] == "9" and d9[1]["source"] == "real" and d9[1]["best"] > 0.9
    # QorTime's scorebug pack calls this glyph "0": the cross-check still fails it closed (v0.3.4 rule kept)
    assert d9[0] is None and d9[1]["why"] == "xcheck_disagrees"


# ---- v0.3.6: covered end-menu panel corroborates (never reads) the scorebug final
@pytest.mark.parametrize("exp,ok", [
    ({"away": 34, "home": 29}, True),
    ({"away": 34, "home": 22}, False), ({"away": 28, "home": 29}, False), ({"away": 35, "home": 29}, False),
    ({"away": 40, "home": 29}, False), ({"away": 34, "home": 35}, False), ({"away": 27, "home": 29}, False),
])
def test_corroborate_online_end_menu(exp, ok):
    from qorcap_endmenu import EndMenuReader
    assert EndMenuReader().corroborate(img("m27_mnp_end_menu_online.jpg"), exp)["ok"] is ok


def _shift_quarter_word(im, dx):
    """Move only the grey quarter word right by ``dx``. The clock and the scores stay put."""
    out = im.copy()
    y0, y1 = M.BAR_Y
    x0, x1 = M.QUARTER_X[0], 890
    src = im[y0:y1, x0:x1]
    hsv = cv2.cvtColor(src, cv2.COLOR_BGR2HSV)
    ink = hsv[..., 2] > M.QUARTER_INK
    if not ink.any():
        return out
    bg = np.median(src[~ink], axis=0).astype(np.uint8)
    cleared = src.copy()
    cleared[ink] = bg
    out[y0:y1, x0:x1] = cleared
    ys, xs = np.nonzero(ink)
    for y, x in zip(ys, xs):
        out[y0 + y, x0 + x + dx] = src[y, x]
    return out


def _old_box_label(mnp, im):
    """The v0.3.17 crop: a fixed box ending at x=882, which clips a 2ND that has moved right."""
    hsv = M.bar_hsv(im)
    v = hsv[..., 2]
    y0, y1 = M.QUARTER_Y[0] - M.BAR_Y[0], M.QUARTER_Y[1] - M.BAR_Y[0]
    q = (v[y0:y1, M.QUARTER_X[0]:M.QUARTER_X[1]] > M.QUARTER_INK).astype(np.uint8)
    ys, xs = np.nonzero(q)
    if len(xs) < 20:
        return None
    lab, _, _ = mnp.classify("quarter", q[ys.min():ys.max() + 1, xs.min():xs.max() + 1], M.QUARTER_SIZE)
    return lab


@pytest.mark.parametrize("name,away,home,q,clock,red", [
    ("m27_mnp_q2.jpg", 14, 13, 2, "2:27", False),
    ("m27_mnp_q4_final.jpg", 34, 29, 4, "0:00", True),
])
def test_quarter_word_shifted_toward_the_clock_still_reads(mnp, name, away, home, q, clock, red):
    """2ND/4TH are wider than 1ST. Eight pixels toward the clock clips the old box and used to go quarter-null."""
    im = _shift_quarter_word(img(name), 8)
    assert _old_box_label(mnp, im) is None
    fr = mnp.read(im)
    assert fr.ok, fr.reason
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) == (away, home, q, clock, red)


def test_q1_word_still_reads_after_the_same_shift(mnp):
    fr = mnp.read(_shift_quarter_word(img("m27_mnp_q1.jpg"), 8))
    assert fr.ok and (fr.quarter, fr.clock_text, fr.left_score, fr.right_score) == (1, "4:07", 0, 0)


def test_clock_keeps_reading_when_the_quarter_word_is_gone(mnp):
    im = img("m27_mnp_q2.jpg").copy()
    y0, y1 = M.BAR_Y
    x0, x1 = M.QUARTER_X[0], 890
    src = im[y0:y1, x0:x1]
    hsv = cv2.cvtColor(src, cv2.COLOR_BGR2HSV)
    ink = hsv[..., 2] > M.QUARTER_INK
    bg = np.median(src[~ink], axis=0).astype(np.uint8)
    src[ink] = bg
    im[y0:y1, x0:x1] = src
    fr = mnp.read(im)
    assert fr.ok is False and fr.reason in ("quarter_missing", "quarter_unsure")
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text) == (14, 13, None, "2:27")


def test_clock_from_digits_accepts_ten_through_fifteen_only():
    assert M.clock_from_digits("1500") == (900, "15:00")
    assert M.clock_from_digits("1000") == (600, "10:00")
    assert M.clock_from_digits("1559") == (959, "15:59")
    assert M.clock_from_digits("427") == (267, "4:27")
    assert M.clock_from_digits("1600") is None
    assert M.clock_from_digits("0900") is None
    assert M.clock_from_digits("460") is None


def test_four_digit_clock_reads_fifteen_minutes(mnp):
    """Paint 1 5 0 0 from the clock templates into the clock band of a real Monday Night frame."""
    import numpy as np
    im = img("m27_mnp_q2.jpg").copy()
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    v = hsv[..., 2]
    # Clear the existing 3-digit clock so only the painted 15:00 remains.
    v[M.QUARTER_Y[0]:M.QUARTER_Y[1], 880:940] = 20
    labels, vecs = mnp.groups["clock"]
    glyphs = {}
    for lab, vec in zip(labels, vecs):
        if lab in glyphs:
            continue
        imgv = vec.reshape(M.CLOCK_SIZE[1], M.CLOCK_SIZE[0])
        glyphs[lab] = (imgv > 0.02).astype(np.uint8)
    x = 880  # inside the one-digit box, four digits: the old reader required exactly three
    for lab in "1500":
        g = glyphs[lab]
        h, w = g.shape
        y = 692
        patch = v[y:y + h, x:x + w]
        patch[g.astype(bool)] = 255
        v[y:y + h, x:x + w] = patch
        x += w + 3
    hsv[..., 2] = v
    painted = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    fr = mnp.read(painted)
    assert fr.clock_text == "15:00" and fr.clock_seconds == 900, (fr.reason, fr.clock_text, fr.quarter)
    assert fr.quarter == 2 and (fr.left_score, fr.right_score) == (14, 13)


def test_reader_core_confirms_covered_end_menu_only_with_q4_scorebug_final():
    from qorcap_endmenu import EndMenuReader
    from qorcap_reader import ReaderCore

    class R:
        def read(self, im):
            return NS(ok=False, reason="layout_unknown")

    class A:
        def offer(self, fr, **_):
            return NS(agreed=False, read=None)

    def core(final):
        c = ReaderCore([].append, R(), A(), None, lambda tl: NS(final=False), final_screen_detector=madden_final_screen,
                       end_menu_reader=EndMenuReader())
        c.timeline = [{"t_ms": 0, "away": 34, "home": 29, "quarter": 4, "clock_s": 0}]
        c.final = final
        for i in range(4):
            c.on_frame(1000 + i * 500, img("m27_mnp_end_menu_online.jpg"))
        return c
    c = core({"type": "final", "basis": "clock_zero", "home": 29, "away": 34, "quarter": 4, "t_ms": 0})
    assert c.final["method"] == "end_menu_read" and (c.final["away"], c.final["home"]) == (34, 29)
    assert c.end_menu["template_sources"] == ["corroborated"] and "partly covered" in c.final["notes"][0]
    assert core(None).end_menu is None  # no scorebug final yet: nothing to corroborate
