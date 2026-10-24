"""Sidecar logic tests. Uses QorTime's real detect_final/AgreementWindow when QORTIME is importable."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
from qorcap_reader import ReaderCore  # noqa: E402
from qor_receipt.final_detect import detect_final  # noqa: E402


class Agree:  # passthrough agreement (the real AgreementWindow is tested in qortime)
    def offer(self, fr, **_):
        return NS(agreed=fr.ok, read=fr if fr.ok else None)


class Reader:
    def __init__(self):
        self.next = None

    def read(self, img):
        return self.next


def fr(left, right, q, clock_s, red=False):
    return NS(ok=True, reason="ok", left_score=left, right_score=right, quarter=q, clock_seconds=clock_s, clock_red=red)


def run(frames):
    posts, rd = [], Reader()
    core = ReaderCore(posts.append, rd, Agree(), None, detect_final)
    for t, f in frames:
        rd.next = f
        core.on_frame(t, None)
    return core, posts


class FR(NS):
    """Stand-in FrameRead with the pair the quarter-soft lock needs."""

    @property
    def pair(self):
        if self.left_score is None or self.right_score is None:
            return None
        return (self.left_score, self.right_score)


def test_quarter_one_after_the_clock_runs_down_fails_closed():
    """match-20261017-091651: 27 reads labelled Q1 during the real 2nd quarter.

    The clock had already posted 0:01. A later glyph that still matches "1" is unknown. The score stays.
    A real 2nd quarter after that still posts, and a 1st quarter with no prior expiry still posts.
    """
    posts, rd = [], Reader()
    core = ReaderCore(posts.append, rd, Agree(), None, detect_final)

    def feed(t, left, right, q, clock):
        rd.next = FR(ok=True, reason="ok", left_score=left, right_score=right, quarter=q,
                     clock_seconds=clock, clock_red=False)
        core.on_frame(t, None)

    feed(0, 0, 0, 1, 120)
    assert posts[-1]["quarter"] == 1 and core._period_ended is False
    feed(1000, 0, 0, 1, 1)
    assert (posts[-1]["quarter"], posts[-1]["clock_s"]) == (1, 1) and core._period_ended
    for t in (3000, 3250, 3500):
        feed(t, 0, 0, 1, 120)
    last = [p for p in posts if p["type"] == "score"][-1]
    assert (last["away"], last["home"], last["quarter"], last["clock_s"]) == (0, 0, None, 120)
    for t in (3600, 3850, 4100):
        feed(t, 3, 0, 1, 40)
    last = [p for p in posts if p["type"] == "score"][-1]
    assert (last["away"], last["home"], last["quarter"]) == (3, 0, None)
    for t in (6000, 6250, 6500):
        feed(t, 3, 0, 2, 80)
    last = [p for p in posts if p["type"] == "score"][-1]
    assert (last["away"], last["home"], last["quarter"]) == (3, 0, 2)


def test_q1_to_q2_on_the_standard_bar_releases_when_the_clock_resets_to_15_00():
    """match-20261017-162055: Q1 ended, Q2 opened at 15:00, and every later read lost quarter and clock.

    A "1" inside the suppression window stays unknown (091651), including at 15:00. A real 2ND at
    15:00 posts as Q2 with the clock. Scores lock with no quarter. The window does not last the game.
    """
    from qorcap_reader import QUARTER_ONE_SUPPRESS_MS

    posts, rd = [], Reader()
    core = ReaderCore(posts.append, rd, Agree(), None, detect_final)

    def feed(t, left, right, q, clock, *, ok=True, reason="ok"):
        rd.next = FR(ok=ok, reason=reason, left_score=left, right_score=right, quarter=q,
                     clock_seconds=clock, clock_red=False)
        core.on_frame(t, None)

    def score():
        return [p for p in posts if p["type"] == "score"][-1]

    feed(0, 0, 0, 1, 30)
    feed(1_000, 0, 0, 1, 0)
    assert (score()["quarter"], score()["clock_s"], core._period_ended) == (1, 0, True)
    for t in (2_000, 2_250, 2_500):
        feed(t, 0, 0, 1, 900)  # 2ND still matching "1" at the 15:00 reset: fail closed, clock kept
    assert (score()["away"], score()["home"], score()["quarter"], score()["clock_s"]) == (0, 0, None, 900)
    assert core.periods.periods_completed == 1
    for t in (4_000, 4_250, 4_500):
        feed(t, 0, 0, 2, 900)  # the standard bar's 2ND, clock 0:00 -> 15:00
    assert (score()["away"], score()["home"], score()["quarter"], score()["clock_s"]) == (0, 0, 2, 900)
    assert core._period_ended is False
    # True final was away 6, home 21. The quarter glyph is gone; the digits still lock.
    for i in range(9):
        feed(20_000 + i * 250, 6, 21, None, None, ok=False, reason="quarter_unsure")
    assert (score()["away"], score()["home"], score()["quarter"], score()["clock_s"]) == (6, 21, None, None)
    # A "1" after a real 2nd quarter is still unknown. The window expiring must not relabel Q2 as Q1.
    for t in (30_000, 30_250, 30_500):
        feed(t, 6, 21, 1, 400)
    assert score()["quarter"] is None and score()["clock_s"] == 400


def test_quarter_one_suppression_window_closes_on_its_own():
    """The rundown guard is not a latch. Once the window ends, and no later quarter was read, a "1" posts."""
    from qorcap_reader import QUARTER_ONE_SUPPRESS_MS

    posts, rd = [], Reader()
    core = ReaderCore(posts.append, rd, Agree(), None, detect_final)

    def feed(t, q, clock):
        rd.next = FR(ok=True, reason="ok", left_score=0, right_score=0, quarter=q,
                     clock_seconds=clock, clock_red=False)
        core.on_frame(t, None)

    feed(0, 1, 1)
    for t in (1_000, 1_250, 1_500):
        feed(t, 1, 120)
    assert [p for p in posts if p["type"] == "score"][-1]["quarter"] is None
    feed(QUARTER_ONE_SUPPRESS_MS + 1_000, 1, 400)
    last = [p for p in posts if p["type"] == "score"][-1]
    assert (last["quarter"], last["clock_s"]) == (1, 400) and core._period_ended is False


def test_open_shared_text_reads_on_this_platform(tmp_path):
    from qorcap_reader import open_shared_text
    p = tmp_path / "inputs.jsonl"
    p.write_text('{"type":"in"}\n', encoding="utf-8")
    with open_shared_text(p) as fh:
        assert '"type":"in"' in fh.read()


def test_left_is_away_and_score_events():
    _, posts = run([(0, fr(3, 7, 1, 600)), (250, fr(3, 7, 1, 600))])
    assert posts[0] == {"type": "score", "t_ms": 0, "away": 3, "home": 7, "quarter": 1, "clock_s": 600, "clock_red": False}
    assert len(posts) == 1  # same read within 1 s is not re-posted


def test_red_clock_needs_five_reads():
    frames = [(i * 1100, fr(24, 21, 4, 0, red=True)) for i in range(4)]
    _, posts = run(frames)
    assert not any(p["type"] == "final" for p in posts)
    _, posts = run(frames + [(4 * 1100, fr(24, 21, 4, 0, red=True))])
    fin = [p for p in posts if p["type"] == "final"]
    assert fin == [{"type": "final", "basis": "clock_zero", "home": 21, "away": 24, "quarter": 4, "t_ms": 4400}]


def test_scorebug_gone_fallback():
    frames = [(i * 1100, fr(10, 17, 4, 3)) for i in range(3)]
    gone = NS(ok=False, reason="layout_unknown")
    frames += [(3300 + i * 1000, gone) for i in range(32)]
    core, posts = run(frames)
    assert core.final and core.final["basis"] == "scorebug_gone" and core.final["home"] == 17


def test_tie_is_not_final():
    _, posts = run([(i * 1100, fr(14, 14, 4, 0)) for i in range(6)])
    assert not any(p["type"] == "final" for p in posts)


def test_real_reader_on_qortime_fixture():
    import pytest
    cv2 = pytest.importorskip("cv2")
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    root = Path(os.environ.get("QORTIME", "/workspace/qortime-src"))
    import numpy as np
    from qorcap_reader import prep
    band = cv2.imread(str(root / "tests/fixtures/madden27_live/q1_6-0.png"))
    if band is None:
        pytest.skip("qortime fixture not found")
    f = np.full((360, 640, 3), 40, np.uint8)
    f[330:] = band  # same composition as qortime tests/test_live_madden27.py
    img = prep(cv2.resize(f, (1280, 720), interpolation=cv2.INTER_CUBIC))  # card res -> reader res
    posts = []
    core = ReaderCore(posts.append, ScorebugReader(), AgreementWindow(), None, detect_final)
    for i in range(6):
        core.on_frame(i * 250, img)
    scores = [p for p in posts if p["type"] == "score"]
    assert scores, f"no agreed read: {core.stats}"
    assert (scores[0]["away"], scores[0]["home"], scores[0]["quarter"]) == (6, 0, 1)


def _menu_frame(items: int, bg: int):
    import numpy as np
    img = np.full((360, 640, 3), bg, np.uint8)
    img[22:42, 20:150] = 230                       # highlighted first menu item
    for k in range(1, items):                      # remaining menu items, 15 px apart
        y = 30 + 15 * k
        img[y:y + 6, 25:110] = 200
    img[30:60, 300:420] = 220                      # score panel digits
    return img


def test_madden_final_screen_vs_pause_menu():
    from qorcap_reader import madden_final_screen
    assert madden_final_screen(_menu_frame(6, 2))          # end-of-game menu: 6 items, black
    assert not madden_final_screen(_menu_frame(10, 2))     # pause menu: ~10 items reach below y=120
    assert not madden_final_screen(_menu_frame(6, 40))     # faded game behind (not black)
    import numpy as np
    assert not madden_final_screen(np.zeros((360, 640, 3), np.uint8))  # plain black (no menu bar)
    assert madden_final_screen(np.repeat(np.repeat(_menu_frame(6, 2), 2, 0), 2, 1))  # 1280x720


def test_final_screen_bypasses_pause_and_confirms_final():
    import numpy as np
    from qor_receipt.final_detect import detect_final
    from qorcap_reader import ReaderCore, madden_final_screen

    class Paused:  # the static end menu looks like a pause to the pause detector
        paused = True
        def update(self, t, img): pass

    posts = []
    core = ReaderCore(posts.append, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen)
    core.timeline = [{"t_ms": 1000 + i * 500, "home": 14, "away": 7, "quarter": 4, "clock_s": c, "clock_red": True}
                     for i, c in enumerate([5, 5, 3, 0])]
    end = _menu_frame(6, 2)
    for k in range(20):
        core.on_frame(5000 + k * 250, end)
    fin = [p for p in posts if p.get("type") == "final"]
    assert fin and fin[0]["basis"] == "final_screen" and (fin[0]["home"], fin[0]["away"]) == (14, 7)
    # a pause menu stays a pause
    posts2 = []
    core2 = ReaderCore(posts2.append, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen)
    core2.timeline = list(core.timeline[:4])
    for k in range(20):
        core2.on_frame(5000 + k * 250, _menu_frame(10, 2))
    assert not [p for p in posts2 if p.get("type") == "final"]


FIX = Path(__file__).parent / "fixtures"


def _fixture(name):
    import cv2
    img = cv2.imread(str(FIX / name))
    assert img is not None, name
    return img


def test_end_menu_detector_is_cursor_independent_on_real_frames():
    """match-20261016-143212: cursor on RESTART (missed live by v0.3.2); match 125430: cursor on FINISH GAME."""
    from qorcap_reader import madden_final_screen
    assert madden_final_screen(_fixture("m27_end_menu_restart.jpg"))
    assert madden_final_screen(_fixture("m27_end_menu_finish.jpg"))
    assert not madden_final_screen(_fixture("m27_pause_menu.jpg"))
    assert not madden_final_screen(_fixture("m27_pause_menu_start.jpg"))
    import numpy as np
    big = np.repeat(np.repeat(_fixture("m27_end_menu_restart.jpg"), 2, 0), 2, 1)  # card resolution 1280x720
    assert madden_final_screen(big)


def test_menu_cursor_on_last_item_synthetic():
    import numpy as np
    from qorcap_reader import madden_final_screen
    img = _menu_frame(6, 2)
    img[22:42, 20:150] = 2
    img[22:28, 25:110] = 200            # first item as plain text
    img[98:125, 20:150] = 230           # highlight + description box on the 6th item
    assert madden_final_screen(img)


def test_real_end_menu_confirms_final_through_pause_gate():
    from qorcap_reader import ReaderCore, madden_final_screen

    class Paused:
        paused = True
        def update(self, t, img): pass

    posts = []
    core = ReaderCore(posts.append, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen)
    # live timeline tail of match-20261016-143212
    core.timeline = [{"t_ms": t, "home": 17, "away": 0, "quarter": 4, "clock_s": c, "clock_red": True}
                     for t, c in [(1792180043566, 3), (1792180044735, 2), (1792180045623, None), (1792180046539, 0)]]
    core.timeline.append({"t_ms": 1792180050062, "until_ms": 1792180067515, "obs": "scorebug_absent", "n": 60})
    end = _fixture("m27_end_menu_restart.jpg")
    for k in range(8):
        core.on_frame(1792180070000 + k * 1000, end)  # static screen fed ~1/s
    fin = [p for p in posts if p.get("type") == "final"]
    assert fin and fin[0]["basis"] == "final_screen" and (fin[0]["away"], fin[0]["home"]) == (0, 17)


class _Band:
    def tobytes(self):
        return b"synthetic-band"


class _Syn:
    """Always-ok scorebug read. The numbers are synthetic, not from a recorded match."""

    ok = True
    reason = "ok"
    left_score = 0
    right_score = 3
    quarter = 1
    clock_seconds = 100
    clock_red = False

    @property
    def pair(self):
        return (self.left_score, self.right_score)


class _StubReader:
    def __init__(self, fr):
        self.fr = fr

    def read(self, _img):
        return self.fr

    def reference_band(self, _img):
        return _Band(), None


def _lags(core):
    return [h for h in core.health_observations() if h.get("obs") == "reader_lag"]


def test_score_rows_inside_the_agreement_gap_and_reader_lag_outside(capsys):
    """1.9 s still agrees. 2.1 s agrees nothing and logs one reader_lag. No score is guessed."""
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow

    from qorcap_reader import ReaderCore

    def drive(step, n):
        posts = []
        core = ReaderCore(posts.append, _StubReader(_Syn()), AgreementWindow(), None, detect_final)
        written = []
        core.health_write = written.append
        for i in range(n):
            core.on_frame(i * step, None)
        return core, posts, written

    core, posts, written = drive(1900, 4)
    scores = [p for p in posts if p.get("type") == "score"]
    assert scores and (scores[0]["away"], scores[0]["home"]) == (0, 3)
    assert _lags(core) == []
    assert not any(p.get("obs") == "reader_lag" for p in posts)
    err = capsys.readouterr().err
    assert "Score reader behind" not in err

    core, posts, written = drive(2100, 4)
    assert [p for p in posts if p.get("type") == "score"] == []
    assert not any(p.get("type") == "score" or p.get("obs") in ("reader_lag", "score_unagreed") for p in posts)
    open_lag = _lags(core)
    assert len(open_lag) == 1 and "home" not in open_lag[0] and "away" not in open_lag[0]
    assert open_lag[0]["n"] == 3 and open_lag[0]["max_gap_ms"] == 2100
    assert written == []  # still open: the closed line is the one record
    err = capsys.readouterr().err
    assert err.count("Score reader behind") == 1
    assert "Score reader behind: frames 2.1 s apart; scores held" in err
    # One in-budget frame must not close the span (a flap would emit a row per pair).
    # The recovery frames are not a readable bar, so they also do not mint a score.
    from qorcap_reader import HEALTH_CLEAR_FRAMES, HEALTH_WARN_MS
    core.reader.fr = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                        clock_seconds=None, clock_red=False, pair=None)
    for i in range(HEALTH_CLEAR_FRAMES - 1):
        core.on_frame(3 * 2100 + (i + 1) * 400, None)
    assert core._lag is not None and [h for h in core.health if h.get("obs") == "reader_lag"] == []
    core.on_frame(3 * 2100 + HEALTH_CLEAR_FRAMES * 400, None)
    held = _lags(core)
    assert len(held) == 1 and core._lag is None
    assert held[0]["until_ms"] == 3 * 2100 and held[0]["n"] == 3 and held[0]["count"] == 1
    # Held for 30 s of frame time, so a flap does not append a reader_health.jsonl row yet.
    assert [h for h in core.health if h.get("obs") == "reader_lag"] == []
    assert written == []
    core.on_frame(held[0]["until_ms"] + HEALTH_WARN_MS, None)
    assert len(written) == 1
    assert written[0]["until_ms"] == 3 * 2100 and written[0]["n"] == 3 and written[0]["count"] == 1
    assert [h for h in core.health if h.get("obs") == "reader_lag"] == written
    assert [p for p in posts if p.get("type") == "score"] == []


def test_visible_bar_without_agreement_logs_score_unagreed():
    """A readable bar that never locks for more than 10 s is one rate-limited span, not a score."""
    from qorcap_reader import ReaderCore

    class Never:
        def __init__(self):
            self.config = NS(max_gap_ns=2_000_000_000)

        def offer(self, fr, **_):
            return NS(agreed=False, read=None, reason="agreeing")

    posts = []
    core = ReaderCore(posts.append, _StubReader(_Syn()), Never(), None, detect_final)
    for i in range(31):  # 0 .. 12_000 ms at 400 ms: under the 2 s gap, past 10 s unlocked
        core.on_frame(i * 400, None)
    spans = [h for h in core.health_observations() if h.get("obs") == "score_unagreed"]
    assert len(spans) == 1
    assert spans[0]["reasons"].get("agreeing", 0) > 1
    assert "home" not in spans[0] and "away" not in spans[0]
    assert _lags(core) == []
    assert [p for p in posts if p.get("type") == "score"] == []
    core.on_frame(12_400, None)  # still visible and still unlocked: the same span, not a second one
    assert len([h for h in core.health_observations() if h.get("obs") == "score_unagreed"]) == 1
    from qorcap_reader import HEALTH_CLEAR_FRAMES
    core.reader.fr = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                        clock_seconds=None, clock_red=False, pair=None)
    for i in range(HEALTH_CLEAR_FRAMES):
        core.on_frame(12_800 + i * 400, None)
    posted = [h for h in core.health_observations() if h.get("obs") == "score_unagreed"]
    assert len(posted) == 1 and posted[0]["count"] == 1 and core._unagreed is None
    assert [h for h in core.health if h.get("obs") == "score_unagreed"] == []
    assert [p for p in posts if p.get("type") == "score"] == []


def test_lag_flap_is_one_open_span_and_one_warning(capsys):
    """Gaps that alternate around 2 s stay one span. A second warning inside 30 s is dropped."""
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow

    from qorcap_reader import HEALTH_CLEAR_FRAMES, ReaderCore

    posts = []
    core = ReaderCore(posts.append, _StubReader(_Syn()), AgreementWindow(), None, detect_final)
    t = 0
    core.on_frame(t, None)
    for _ in range(8):  # stays inside the 30 s warning window
        t += 2100
        core.on_frame(t, None)
        t += 400
        core.on_frame(t, None)
    err = capsys.readouterr().err
    assert err.count("Score reader behind") == 1
    assert [h for h in core.health if h.get("obs") == "reader_lag"] == []
    assert len(_lags(core)) == 1
    # Quiet frames are not a readable bar, so closing the span does not mint a score.
    core.reader.fr = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                        clock_seconds=None, clock_red=False, pair=None)
    for _ in range(HEALTH_CLEAR_FRAMES):
        t += 400
        core.on_frame(t, None)
    closed = _lags(core)
    assert len(closed) == 1 and closed[0]["count"] == 1 and core._lag is None
    assert [h for h in core.health if h.get("obs") == "reader_lag"] == []
    t += 2100
    core.on_frame(t, None)
    assert capsys.readouterr().err.count("Score reader behind") == 0
    assert len(_lags(core)) == 2  # the held row plus the new open span
    assert [p for p in posts if p.get("type") == "score"] == []


def test_health_rows_merge_inside_thirty_seconds_and_keep_milliseconds(capsys):
    """Two closed lag spans inside 30 s are one row with a count. 2001 ms prints as 2.001 s."""
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow

    from qorcap_reader import HEALTH_CLEAR_FRAMES, HEALTH_WARN_MS, ReaderCore

    posts = []
    core = ReaderCore(posts.append, _StubReader(_Syn()), AgreementWindow(), None, detect_final)
    written = []
    core.health_write = written.append
    core.on_frame(0, None)
    core.on_frame(2001, None)
    err = capsys.readouterr().err
    assert "Score reader behind: frames 2.001 s apart; scores held" in err
    assert "2.0 s" not in err
    core.reader.fr = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                        clock_seconds=None, clock_red=False, pair=None)
    t = 2001
    for _ in range(HEALTH_CLEAR_FRAMES):
        t += 400
        core.on_frame(t, None)
    t += 2100
    core.on_frame(t, None)
    for _ in range(HEALTH_CLEAR_FRAMES):
        t += 400
        core.on_frame(t, None)
    assert written == []
    held = _lags(core)
    assert len(held) == 1 and held[0]["count"] == 2 and held[0]["n"] == 2
    assert held[0]["max_gap_ms"] == 2100
    core.on_frame(held[0]["until_ms"] + HEALTH_WARN_MS, None)
    assert len(written) == 1 and written[0]["count"] == 2 and written[0]["max_gap_ms"] == 2100
    assert [p for p in posts if p.get("type") == "score"] == []


def test_fold_skips_one_bad_row_and_keeps_the_rest(tmp_path, capsys):
    """A row that raises is logged once per inputs file. The rows around it still fold."""
    from qorcap_reader import InputsShare, _fold_activity, _fold_options, options_presses_ms

    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("bad row")

    presses = options_presses_ms([
        {"type": "in", "b": 512, "t_ms": 10},
        Boom(),
        {"type": "in", "b": 0, "t_ms": 15},
        {"type": "in", "b": 512, "t_ms": 20},
    ])
    assert presses == [10, 20]
    assert capsys.readouterr().err.count("inputs fold skipped a row") == 1
    # A later fold is not silenced by an earlier one. The flag is not process-wide.
    options_presses_ms([Boom()])
    assert capsys.readouterr().err.count("inputs fold skipped a row") == 1

    first = InputsShare(tmp_path / "match-a" / "inputs.jsonl")
    second = InputsShare(tmp_path / "match-b" / "inputs.jsonl")
    _fold_options([Boom()], [], False, None, first._fold_log)
    kept = _fold_activity([
        {"type": "in", "b": 1, "t_ms": 1},
        Boom(),
        {"type": "in", "b": 2, "t_ms": 2},
    ], first._fold_log)
    assert [row["t_ms"] for row in kept] == [1, 2]
    assert capsys.readouterr().err.count("inputs fold skipped a row") == 1
    _fold_options([Boom()], [], False, None, first._fold_log)
    assert capsys.readouterr().err.count("inputs fold skipped a row") == 0
    _fold_activity([Boom()], second._fold_log)
    assert capsys.readouterr().err.count("inputs fold skipped a row") == 1


def test_lag_flap_longer_than_a_minute_writes_at_least_two_rows():
    """A flap past 60 s writes during the match. The window starts at the first t_ms.

    Anchoring the wait on the span's growing end merges every new span and writes nothing.
    """
    from qorcap_reader import HEALTH_CLEAR_FRAMES, ReaderCore

    posts = []
    unread = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                clock_seconds=None, clock_red=False, pair=None)
    core = ReaderCore(posts.append, _StubReader(unread), Agree(), None, detect_final)
    written = []
    core.health_write = written.append
    t = 0
    core.on_frame(t, None)
    while t <= 70_000:
        t += 2100
        core.on_frame(t, None)
        for _ in range(HEALTH_CLEAR_FRAMES):
            t += 400
            core.on_frame(t, None)
    lags = [row for row in written if row.get("obs") == "reader_lag"]
    assert t > 60_000
    assert len(lags) >= 2
    assert [p for p in posts if p.get("type") == "score"] == []


def test_flush_health_writes_a_span_that_is_still_open():
    """Stopping mid-span still records it. An open stall is not dropped."""
    from qorcap_reader import ReaderCore

    posts = []
    unread = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                clock_seconds=None, clock_red=False, pair=None)
    core = ReaderCore(posts.append, _StubReader(unread), Agree(), None, detect_final)
    written = []
    core.health_write = written.append
    core.on_frame(0, None)
    core.on_frame(2100, None)
    assert written == [] and core._lag is not None
    core.flush_health()
    assert len(written) == 1 and written[0]["obs"] == "reader_lag" and written[0]["n"] >= 1
    assert written[0]["open_at_exit"] is True
    assert core._lag is None and core._health_pending == {}


def test_exit_flush_marks_only_a_span_that_is_still_open():
    """open_at_exit is for a span the reader was still in. A closed span has no such field."""
    from qorcap_reader import HEALTH_CLEAR_FRAMES, ReaderCore

    unread = NS(ok=False, reason="layout_unknown", left_score=None, right_score=None, quarter=None,
                clock_seconds=None, clock_red=False, pair=None)

    def closed_core():
        posts = []
        core = ReaderCore(posts.append, _StubReader(unread), Agree(), None, detect_final)
        written = []
        core.health_write = written.append
        t = 0
        core.on_frame(t, None)
        t += 2100
        core.on_frame(t, None)
        for _ in range(HEALTH_CLEAR_FRAMES):
            t += 400
            core.on_frame(t, None)
        return core, written

    core, written = closed_core()
    assert written == [] and core._lag is None
    core.flush_health()
    assert len(written) == 1 and "open_at_exit" not in written[0]

    core, written = closed_core()
    # The close left the clock at 3300. One more late frame is still open.
    core.on_frame(5400, None)
    assert core._lag is not None and written == []
    core.flush_health()
    assert len(written) == 1
    assert written[0]["open_at_exit"] is True and written[0]["count"] == 2


def _no_final(_timeline):
    return NS(final=False)


def test_reader_stalled_notice_and_resume():
    """Quiet frames write one reader_stalled, then one reader_resumed. A timing line does not clear it.

    Emptying consider_stall leaves ``written`` without the notice.
    """
    from qorcap_reader import READER_STALL_MS, ReaderCore

    posts = []
    core = ReaderCore(posts.append, None, None, None, _no_final)
    written = []
    core.health_write = written.append
    core.consider_stall(0)
    core.consider_stall(READER_STALL_MS - 1)
    assert written == []
    core.consider_stall(READER_STALL_MS)
    stalls = [row for row in written if row.get("kind") == "reader_stalled"]
    assert len(stalls) == 1
    assert stalls[0]["t_ms"] == READER_STALL_MS
    assert stalls[0]["last_row_age_s"] == READER_STALL_MS / 1000.0
    assert "obs" not in stalls[0]
    core.consider_stall(READER_STALL_MS + 10_000)
    assert len([row for row in written if row.get("kind") == "reader_stalled"]) == 1
    # The 30 s timing summary is not a row. Appending one must not resume the reader.
    written.append({"kind": "timing", "t_ms": READER_STALL_MS + 10_000})
    core.consider_stall(READER_STALL_MS + 15_000)
    assert [row for row in written if row.get("kind") == "reader_resumed"] == []
    core.note_progress(READER_STALL_MS + 20_000)
    resumed = [row for row in written if row.get("kind") == "reader_resumed"]
    assert len(resumed) == 1
    assert resumed[0]["last_row_age_s"] == (READER_STALL_MS + 20_000) / 1000.0
    assert posts == []
    core.consider_stall(READER_STALL_MS + 20_000 + READER_STALL_MS)
    assert len([row for row in written if row.get("kind") == "reader_stalled"]) == 2


def test_open_scorebug_absent_is_not_a_stall():
    """A span that is still growing has been written. That is not a silent reader."""
    from qorcap_reader import READER_STALL_MS, ReaderCore

    posts = []
    core = ReaderCore(posts.append, None, None, None, _no_final)
    written = []
    core.health_write = written.append
    core.consider_stall(0)
    core._observe(0, None, True)
    core._observe(READER_STALL_MS, None, True)
    core.consider_stall(READER_STALL_MS)
    assert [row for row in written if row.get("kind") == "reader_stalled"] == []
    core.consider_stall(READER_STALL_MS + READER_STALL_MS)
    stalls = [row for row in written if row.get("kind") == "reader_stalled"]
    assert len(stalls) == 1
    assert not any(p.get("obs") == "reader_stalled" or p.get("kind") == "reader_stalled" for p in posts)


def test_inputs_view_times_out_while_the_lock_is_held(tmp_path):
    """A refresh stuck in the inputs read must not block the other caller forever."""
    import threading
    import time

    from qorcap_reader import InputsBusy, shared_inputs

    path = tmp_path / "inputs.jsonl"
    path.write_text('{"type":"in","b":0,"t_ms":1}\n', encoding="utf-8")
    share = shared_inputs(path)
    assert share._lock.acquire(timeout=1)
    done = {}

    def worker():
        t0 = time.perf_counter()
        try:
            share.view(timeout=0.2)
            done["err"] = None
        except InputsBusy as exc:
            done["err"] = exc
        done["dt"] = time.perf_counter() - t0

    th = threading.Thread(target=worker)
    th.start()
    try:
        th.join(1.0)
        assert not th.is_alive()
        assert isinstance(done.get("err"), InputsBusy)
        assert done["dt"] < 1.0
    finally:
        share._lock.release()
        th.join(2)
