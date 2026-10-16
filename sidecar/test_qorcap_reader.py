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
