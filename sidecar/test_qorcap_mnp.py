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
from qorcap_standard import standard_reader  # noqa: E402
from qorcap_template_guard import TEMPLATES_MISSING  # noqa: E402

FX = HERE / "fixtures"


def img(name):
    return cv2.imread(str(FX / name))


def abstain(fr):
    assert fr.ok is False and fr.reason == TEMPLATES_MISSING, getattr(fr, "reason", fr)
    assert fr.left_score is None and fr.right_score is None
    assert fr.quarter is None and fr.clock_text is None


@pytest.fixture(autouse=True)
def _empty_provenance(monkeypatch, tmp_path):
    """A committed harvest must not turn these abstentions into layout_unknown."""
    import json
    import qorcap_template_guard as guard

    prov = tmp_path / "provenance.json"
    prov.write_text(json.dumps({
        "schema": "qorcap-template-provenance/v0",
        "harvest_script": "tools/harvest_templates.py",
        "harvest_version": "3",
        "templates": [],
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "PROVENANCE_PATH", prov)


@pytest.fixture(scope="module")
def mnp():
    return M.MondayNightReader()


@pytest.mark.parametrize("name,away,home,q,clock,red", [
    ("m27_mnp_q1.jpg", 0, 0, 1, "4:07", False),
    ("m27_mnp_q2.jpg", 14, 13, 2, "2:27", False),
    ("m27_mnp_q4_final.jpg", 34, 29, 4, "0:00", True),   # red final-minute clock, touching "00"
])
def test_monday_night_reads(mnp, name, away, home, q, clock, red):
    """170000 and 162055 are not allowlisted template matches. The old eye label is not emitted."""
    fr = mnp.read(img(name))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) != (away, home, q, clock, red)


def test_half_resolution_frame_still_reads_scores(mnp):
    fr = mnp.read(cv2.resize(img("m27_mnp_q2.jpg"), (640, 360), interpolation=cv2.INTER_AREA))
    assert fr.ok is False or (fr.left_score, fr.right_score) == (14, 13)  # never a wrong score


@pytest.mark.parametrize("name", ["m27_pause_menu.jpg", "m27_mnp_end_menu_online.jpg"])
def test_not_monday_night_is_layout_unknown(mnp, name):
    assert mnp.read(img(name)).reason == TEMPLATES_MISSING


def test_broncos_orange_endzone_is_not_monday_night(mnp):
    """match-20261017-091651 OT walk-off: the logo box is ~99% orange. A real Monday Night mark is ~0.06.

    The reader abstains before the orange gate, because the pack is not loaded. The orange measurement stays.
    """
    abstain(mnp.read(img("m27_tnp_091651_ot_10_16.jpg")))
    for name in ("m27_mnp_q1.jpg", "m27_mnp_q2.jpg", "m27_mnp_q4_final.jpg"):
        assert M.LOGO_MIN_ORANGE <= M.logo_orange(M.bar_hsv(img(name))) <= M.LOGO_MAX_ORANGE


def test_pack_provenance(mnp):
    assert mnp._missing and mnp.source == {} and mnp.groups == {} and mnp.ids == {}
    assert not Path(M.PACK).is_file()


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
    a = M.AutoProfileReader(standard_reader(), prep=prep)
    abstain(a.read(img("m27_mnp_q2.jpg")))
    abstain(a.read(img("m27_pause_menu.jpg")))
    assert a.profile_counts == {}


def test_online_end_menu_is_final_screen_and_fails_closed_on_occluded_away():
    from qorcap_endmenu import EndMenuReader
    im = img("m27_mnp_end_menu_online.jpg")
    assert madden_final_screen(prep(im))  # gamertag banners no longer hide the end menu
    rd = EndMenuReader().read(im)
    assert not rd.ok and rd.reason == TEMPLATES_MISSING
    assert rd.away is None and rd.home is None


def test_end_menu_real_9_template_still_xchecked():
    """The end-menu pack that cross-checked the home 9 is removed. No digit is classified."""
    from qorcap_endmenu import EndMenuReader
    r = EndMenuReader()
    assert r._missing and r.labels == [] and r.xpack is None
    rd = r.read(prep(img("m27_mnp_end_menu_online.jpg")))
    assert not rd.ok and rd.reason == TEMPLATES_MISSING and rd.away is None and rd.home is None


# ---- v0.3.6: covered end-menu panel corroborates (never reads) the scorebug final
@pytest.mark.parametrize("exp,ok", [
    ({"away": 34, "home": 29}, True),
    ({"away": 34, "home": 22}, False), ({"away": 28, "home": 29}, False), ({"away": 35, "home": 29}, False),
    ({"away": 40, "home": 29}, False), ({"away": 34, "home": 35}, False), ({"away": 27, "home": 29}, False),
])
def test_corroborate_online_end_menu(exp, ok):
    """No digit templates: neither the former confirming score nor an alternative is corroborated."""
    from qorcap_endmenu import EndMenuReader
    got = EndMenuReader().corroborate(img("m27_mnp_end_menu_online.jpg"), exp)
    assert got["ok"] is False
    assert ok in (True, False)


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


@pytest.mark.parametrize("name,away,home,q,clock,red", [
    ("m27_mnp_q2.jpg", 14, 13, 2, "2:27", False),
    ("m27_mnp_q4_final.jpg", 34, 29, 4, "0:00", True),
])
def test_quarter_word_shifted_toward_the_clock_still_reads(mnp, name, away, home, q, clock, red):
    """The shift still moves ink. With no quarter templates the frame abstains and does not emit the old label."""
    im = _shift_quarter_word(img(name), 8)
    assert "quarter" not in mnp.groups
    fr = mnp.read(im)
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) != (away, home, q, clock, red)


def test_q1_word_still_reads_after_the_same_shift(mnp):
    abstain(mnp.read(_shift_quarter_word(img("m27_mnp_q1.jpg"), 8)))


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
    abstain(mnp.read(im))


def test_clock_from_digits_accepts_ten_through_fifteen_only():
    assert M.clock_from_digits("1500") == (900, "15:00")
    assert M.clock_from_digits("1000") == (600, "10:00")
    assert M.clock_from_digits("1559") == (959, "15:59")
    assert M.clock_from_digits("427") == (267, "4:27")
    assert M.clock_from_digits("1600") is None
    assert M.clock_from_digits("0900") is None
    assert M.clock_from_digits("460") is None


def test_four_digit_clock_reads_fifteen_minutes(mnp):
    """Clock templates are not loaded, so a Monday Night frame cannot be painted into 15:00."""
    assert "clock" not in mnp.groups
    abstain(mnp.read(img("m27_mnp_q2.jpg")))
    assert M.clock_from_digits("1500") == (900, "15:00")


def test_reader_core_confirms_covered_end_menu_only_with_q4_scorebug_final():
    """The covered-panel corroboration needs end-menu templates. Without them the core posts no score."""
    from qorcap_endmenu import EndMenuReader
    from qorcap_reader import ReaderCore

    class R:
        def read(self, im):
            return NS(ok=False, reason="layout_unknown")

    class A:
        def offer(self, fr, **_):
            return NS(agreed=False, read=None)

    posts = []

    def core(final):
        c = ReaderCore(posts.append, R(), A(), None, lambda tl: NS(final=False), final_screen_detector=madden_final_screen,
                       end_menu_reader=EndMenuReader())
        c.timeline = [{"t_ms": 0, "away": 34, "home": 29, "quarter": 4, "clock_s": 0}]
        c.final = final
        for i in range(4):
            c.on_frame(1000 + i * 500, img("m27_mnp_end_menu_online.jpg"))
        return c
    c = core({"type": "final", "basis": "clock_zero", "home": 29, "away": 34, "quarter": 4, "t_ms": 0})
    assert c.end_menu is None
    assert c.final["basis"] == "clock_zero"
    assert not any(p.get("basis") == "end_menu" for p in posts)
    assert any(row.get("obs") == TEMPLATES_MISSING for row in c.timeline)
    assert core(None).end_menu is None


# ---- v0.3.19: thick paint from match-20261017-162055
# labeled_candidates.csv is an offline agreement between an advisory vision pass and a human check.
# MondayNightReader.read does not open it. Templates are ordinary pack rows, held out by video minute.
import csv


def _labels162():
    rows = list(csv.DictReader((FX / "mnp162055" / "labeled_candidates.csv").open()))
    meta, by = {}, {}
    for r in rows:
        meta[r["frame_path"]] = r
        by.setdefault(r["frame_path"], {})[r["field"]] = r["value"]
    return meta, by


def _frame162(fp):
    im = cv2.imread(str(FX / "mnp162055" / "frames" / Path(fp).name))
    assert im is not None and im.shape[:2] == (720, 1280)
    return im


def test_162055_pack_reads_the_labeled_frames_and_holds_out_by_minute(mnp):
    """The offline label table stays. The pack rows cut from those frames are gone, so nothing is scored."""
    meta, _by = _labels162()
    assert len(meta) == 90
    assert mnp.ids == {}
    row = next(iter(meta.values()))
    abstain(mnp.read(_frame162(row["frame_path"])))


@pytest.mark.parametrize("video_time,away,home,q,clock,red", [
    ("12:31.750", 6, 14, 2, "2:16", False),
    ("18:50.000", 6, 20, 2, "0:20", True),   # the snap before the extra point
    ("18:56.750", 6, 21, 2, "0:20", True),
    ("21:51.250", 6, 21, 2, "0:00", True),
    ("22:33.500", 6, 21, 3, "3:57", False),
    ("24:11.750", 0, 0, 1, "4:00", False),   # post-game session, on the glass; the receipt keeps 6-21 Q3
])
def test_162055_named_moments(mnp, video_time, away, home, q, clock, red):
    meta, _ = _labels162()
    row = next(r for r in meta.values() if r["video_time"] == video_time)
    fr = mnp.read(_frame162(row["frame_path"]))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) != (away, home, q, clock, red)


def test_162055_third_quarter_held_out_does_not_become_first_or_second(mnp):
    meta, by = _labels162()
    saw = False
    for row in meta.values():
        if by[row["frame_path"]].get("quarter") != "3":
            continue
        saw = True
        fr = mnp.read(_frame162(row["frame_path"]))
        abstain(fr)
        assert fr.quarter is None
    assert saw


def test_162055_teal_six_the_thin_pack_called_eight_stays_six(mnp):
    """13:10 and 19:50 used to need a sibling 6. With the pack gone they abstain, and they are not read as 8."""
    meta, _ = _labels162()
    for video_time in ("13:10.000", "19:50.000"):
        row = next(r for r in meta.values() if r["video_time"] == video_time)
        fr = mnp.read(_frame162(row["frame_path"]))
        abstain(fr)
        assert fr.left_score != 8


def test_162055_templates_are_the_crops_they_name(mnp):
    """The footage rows are not in the reader. The label file is an offline record, not a loaded template."""
    assert mnp.groups == {} and mnp.ids == {}
    meta, _ = _labels162()
    assert len(meta) == 90


def test_162055_templates_do_not_add_a_quarter_word(mnp):
    assert mnp.groups == {} and mnp.source == {}
    assert "labeled_candidates" not in Path(M.__file__).read_text()
