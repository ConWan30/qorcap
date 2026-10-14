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
