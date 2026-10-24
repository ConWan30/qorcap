"""Standard Madden bar, match-20261017-110028 (v0.3.14).

The QorTime pack's "2" and "3" miss this broadcast (under the 0.70 gate). The overlay variants read them.
A "1ST DOWNS" banner drawn in the score cells must not become a score.
"""
import os
import sys
from pathlib import Path

import cv2

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
from qorcap_standard import standard_reader  # noqa: E402
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader  # noqa: E402

FX = HERE / "fixtures"


def img(name):
    return cv2.imread(str(FX / name))


def _clock_band(reader, digits):
    """Paint clock digits into the standard clock box. ``digits`` is four labels plus a colon kind."""
    import numpy as np
    p = reader.profile
    top = reader._band_top()
    band = np.zeros((p.ref_h - top, p.ref_w, 3), np.uint8)
    x1, x2, y1, y2 = p.clock_text
    patch = band[y1 - top:y2 - top, x1:x2]
    canvas = np.zeros((y2 - y1, x2 - x1), np.uint8)

    def paint(x, label):
        t = reader.pack.groups["clock"][label][0].reshape(9, 6).astype(np.float32)
        img = (t - t.min()) / (t.max() - t.min() + 1e-9)
        canvas[0:9, x:x + 6] = (img * 255).astype(np.uint8)

    paint(0, digits[0])
    paint(7, digits[1])
    if digits[2] == "dots":
        canvas[1:3, 14:16] = 255
        canvas[6:8, 14:16] = 255
    else:
        canvas[1:8, 14:16] = 255  # one bar: not a colon
    paint(17, digits[3])
    paint(24, digits[4])
    patch[:] = canvas[:, :, None]
    return band


def test_standard_bar_reads_15_00_and_keeps_the_score_when_the_quarter_fails():
    """Q2 opens at 15:00. The vendored segmenter calls that clock_layout and drops the pair with the quarter.

    The rescue keeps away 6 / home 21 and reads 15:00. 16:00 and a bar-shaped colon stay unread.
    """
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead, ScorebugReader
    from qorcap_standard import clock_seconds_mmss, rescue_standard_read

    raw = ScorebugReader()
    band = _clock_band(raw, ("1", "5", "dots", "0", "0"))
    assert raw._read_clock(band, 0, 0)[0] is None  # two minute digits are not a learned M:SS
    assert clock_seconds_mmss(raw, band, 0, 0) == (900, "15:00")
    assert clock_seconds_mmss(raw, _clock_band(raw, ("1", "6", "dots", "0", "0")), 0, 0) is None
    assert clock_seconds_mmss(raw, _clock_band(raw, ("1", "5", "bar", "0", "0")), 0, 0) is None

    class Inner:
        def __init__(self):
            self.profile = raw.profile
            self.pack = raw.pack

        def read(self, _frame):
            return FrameRead(ok=False, reason="quarter_unsure", profile_id="madden27_standard",
                             offset=(0, 0), detail={"partial_pair": [6, 21]})

        def reference_band(self, _frame):
            return band, "ok"

        def _read_clock(self, b, dx, dy):
            return raw._read_clock(b, dx, dy)

        def _crop(self, b, box):
            return raw._crop(b, box)

        def clock_is_red(self, patch):
            return raw.clock_is_red(patch)

        def clock_intensity(self, patch, red):
            return raw.clock_intensity(patch, red)

    import numpy as np
    fr = rescue_standard_read(Inner(), np.zeros((360, 640, 3), np.uint8))
    assert not fr.ok and fr.reason == "quarter_unsure" and fr.quarter is None
    assert (fr.left_score, fr.right_score, fr.clock_seconds) == (6, 21, 900)


def test_standard_bar_reads_23_and_30_and_refuses_the_downs_banner():
    raw, aug = ScorebugReader(), standard_reader()
    q3 = img("m27_std_110028_q3_23_0.jpg")
    miss = raw.read(q3)
    assert not miss.ok  # the stall: stock templates do not clear 0.70 on this "2"/"3"
    hit = aug.read(q3)
    assert hit.ok and (hit.left_score, hit.right_score, hit.quarter) == (23, 0, 3), hit
    # 0:26 needs a full-res glyph the stock pack does not clear. Abstain. Do not guess.
    assert hit.clock_seconds in (None, 26)
    q4 = aug.read(img("m27_std_110028_q4_30_6.jpg"))
    assert q4.ok and (q4.left_score, q4.right_score, q4.quarter) == (30, 6, 4), q4
    assert q4.clock_seconds in (None, 67)  # 4th, 1:07, or abstain
    banner = aug.read(img("m27_std_110028_downs.jpg"))
    assert not banner.ok
    assert banner.reason in ("score_panel_covered", "score_slot_ink", "score_unsure", "layout_unknown")
    assert (banner.left_score, banner.right_score) == (None, None)
    assert (banner.quarter, banner.clock_seconds) == (4, 104)  # 4th, 1:44, scores stay blank


def test_standard_bar_reads_the_red_14_of_084523():
    """match-20261018-084523 (CHI–ATL): the QorTime "4" scored 0.57-0.64 under the 0.70 gate, so 0-14 and 6-14
    were score_unsure and the draft stopped at 0-13 Q2. The real-glyph "4" variant reads both; 0-13 is unchanged."""
    from qorcap_reader import prep
    r = standard_reader()
    for name, pair, q in (("m27_std_084523_q2_0_13.jpg", (0, 13), 2), ("m27_std_084523_q2_0_14.jpg", (0, 14), 2),
                          ("m27_std_084523_q4_6_14.jpg", (6, 14), 4)):
        fr = r.read(prep(img(name)))
        assert fr.ok, (name, fr.reason)
        assert tuple(fr.pair) == pair and fr.quarter == q, (name, fr.pair, fr.quarter)
        assert fr.score_margin >= 0.15
    clocks = {
        "m27_std_084523_q2_0_13.jpg": 36,   # 2nd, 0:36
        "m27_std_084523_q2_0_14.jpg": 32,   # 2nd, 0:32
        "m27_std_084523_q4_6_14.jpg": 80,   # 4th, 1:20
    }
    for name, secs in clocks.items():
        # These clocks were read only by the removed 170123 means. Stock glyphs abstain.
        got = r.read(prep(img(name))).clock_seconds
        assert got in (None, secs), (name, got)


def test_without_the_four_variant_084523_stays_unread():
    """Guard that the fixture really exercises the new template (plain QorTime pack fails closed)."""
    from qorcap_reader import prep
    fr = ScorebugReader().read(prep(img("m27_std_084523_q4_6_14.jpg")))
    assert not fr.ok and fr.reason == "score_unsure"


def test_model_id_covers_the_stacked_templates_and_the_grammar_and_clock():
    """The stock pack hash is not this reader. Templates, grammar, and clock parameters are in the id."""
    raw, aug = ScorebugReader(), standard_reader()
    assert aug.model_id != raw.model_id
    assert "3abf7beda13325e0" not in aug.model_id
    assert aug.model_id == standard_reader().model_id
    other = standard_reader()
    other.grammar.reanchor_reads = aug.grammar.reanchor_reads + 1
    assert other.model_id != aug.model_id
    other.grammar.reanchor_reads = aug.grammar.reanchor_reads
    other.clocks.reanchor_span_ms = aug.clocks.reanchor_span_ms + 1
    assert other.model_id != aug.model_id
    assert aug.inner.profile.anchor_search_px == 8
    assert aug.inner.profile.min_logo_ncc == raw.profile.min_logo_ncc == 0.90
    assert aug.wants_full_res is True
    assert "6" in aug.inner.pack.groups["clock"]


def test_downs_banner_is_not_a_field_goal_and_mints_no_score():
    from qorcap_banner import detect_banner
    from qorcap_reader import prep
    r = standard_reader()
    banner = r.read(img("m27_std_110028_downs.jpg"))
    assert not banner.ok
    assert (banner.left_score, banner.right_score) == (None, None)
    assert detect_banner(r.inner, prep(img("m27_std_110028_downs.jpg"))) is None


def test_human_label_for_084523_matches_the_q4_fixture():
    import json
    rows = [json.loads(x) for x in (HERE / "eval" / "human_labels.jsonl").read_text().splitlines() if x.strip()]
    lab = next(x for x in rows if x["match"] == "match-20261018-084523")
    assert (lab["away"], lab["home"], lab["quarter"], lab["source"]) == (6, 14, 4, "human")
