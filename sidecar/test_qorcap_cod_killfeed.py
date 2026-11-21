"""Killfeed ownership by gamertag: synthetic frames drawn from the font pack.

No player capture. Lines are laid out from packs/cod_killfeed_font.json on a
noisy background, so these tests check the reader's rules, not its accuracy
on real frames.
"""
import json
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

import qorcap_cod_killfeed as K  # noqa: E402
from qorcap_cod import CodSetRunner, OWNER_TAG, cod_final_agreement  # noqa: E402

GOLD = (80, 200, 235)
CYAN = (225, 215, 60)
RED = (50, 50, 220)


def _put(img, templ, x, y, colour):
    a = np.clip(templ / max(float(templ.max()), 1e-6), 0, 1)[..., None]
    h, w = a.shape[:2]
    reg = img[y:y + h, x:x + w].astype(np.float32)
    img[y:y + h, x:x + w] = (reg * (1 - a) + np.array(colour, np.float32) * a).astype(np.uint8)


def frame(lines, seed=0):
    """lines: (y, killer, killer colour, victim, victim colour) at 720p."""
    rng = np.random.default_rng(seed)
    img = rng.normal(45, 6, (720, 1280, 3)).clip(0, 255).astype(np.uint8)
    for y, killer, kc, victim, vc in lines:
        _put(img, K.word_template(killer, 7.05), 30, y, kc)
        ix = 30 + int(round(7.05 * len(killer))) + 16
        cv2.rectangle(img, (ix, y + 3), (ix + 26, y + 8), (245, 245, 245), -1)
        cv2.rectangle(img, (ix + 14, y + 8), (ix + 18, y + 12), (245, 245, 245), -1)
        if victim:
            _put(img, K.word_template(victim, 6.6), ix + 44, y, vc)
    return img


def one(killer, kc, victim="Limit_Jaayyy", vc=RED):
    lines = K.read_killfeed_lines(frame([(416, killer, kc, victim, vc)]), OWNER_TAG)["lines"]
    assert len(lines) == 1
    return lines[0]


def test_font_pack_is_rendered_from_a_font_and_covers_the_tag():
    doc = json.loads((Path(K.__file__).parent / "packs" / "cod_killfeed_font.json").read_text(encoding="utf-8"))
    assert doc["kind"] == "cod_killfeed_font_v1"
    assert "rendered from font" in json.dumps(doc["provenance"])
    assert all(ch in K.font_bank()["glyphs"] for ch in OWNER_TAG)


def test_own_name_as_killer_is_self_with_confidence():
    line = one(OWNER_TAG, GOLD)
    assert line["killer"] == K.SELF
    assert 0.7 <= line["killer_conf"] <= 1.0
    assert line["victim"] == K.NOT_SELF


@pytest.mark.parametrize("name", ["Qortrola31", "Qortrola3", "Qortrola30x"])
def test_near_miss_names_are_never_self(name):
    line = one(name, GOLD)
    assert line["killer"] != K.SELF


def test_other_player_and_teammate_kills_are_not_self():
    assert one("HowlingMad90", RED)["killer"] == K.NOT_SELF
    assert one("Alley Beejs", CYAN)["killer"] == K.NOT_SELF


def test_font_twin_without_own_colour_is_not_self():
    # Q and O are near twins in the font. Without the local player's gold the
    # reader does not settle the cell.
    assert one("Oortrola30", CYAN)["killer"] != K.SELF


def test_own_name_as_victim_is_a_death_not_a_kill():
    line = one("HowlingMad90", RED, OWNER_TAG, GOLD)
    assert line["killer"] == K.NOT_SELF
    assert line["victim"] == K.SELF


def test_faint_line_is_unreadable_not_guessed():
    img = frame([(416, OWNER_TAG, (52, 52, 52), "Luna11", (50, 50, 52))])
    lines = K.read_killfeed_lines(img, OWNER_TAG)["lines"]
    assert all(ln["killer"] != K.SELF for ln in lines)


def test_bad_frames_are_not_read():
    assert K.read_killfeed_lines(object())["ok"] is False
    assert K.read_killfeed_lines(np.zeros((480, 480, 3), np.uint8))["reason"] == "layout_unknown"


def _play(tally, frames, t0=1_000_000, step=250_000):
    t = t0
    for img in frames:
        tally.update(t, K.read_killfeed_lines(img, OWNER_TAG))
        t += step
    return t


def test_tally_counts_each_own_line_once_and_ignores_others():
    tally = K.KillfeedTally()
    a = (440, OWNER_TAG, GOLD, "Limit_Jaayyy", RED)
    b = (440, "HowlingMad90", RED, "Luna11", CYAN)
    frames = [frame([a], s) for s in range(6)]
    frames += [frame([(424,) + a[1:], b], s) for s in range(6, 12)]
    frames += [frame([(408,) + a[1:], (424,) + b[1:], (440, OWNER_TAG, GOLD, "Luna11", RED)], s) for s in range(12, 18)]
    _play(tally, frames)
    cov = tally.coverage()
    assert cov["coverage"] == "covered" and cov["kills"] == 2
    assert cov["ownership_qualified"] is True and cov["deduped"] is True
    assert tally.summary()["not_self_lines"] == 1


def test_tally_counts_deaths_but_not_as_kills():
    tally = K.KillfeedTally()
    _play(tally, [frame([(440, "HowlingMad90", RED, OWNER_TAG, GOLD)], s) for s in range(6)])
    cov = tally.coverage()
    assert cov["kills"] == 0 and cov["deaths"] == 1


def test_gap_unplaced_and_fault_make_coverage_unknown():
    line = [(440, OWNER_TAG, GOLD, "Luna11", RED)]
    gap = K.KillfeedTally()
    gap.update(1_000_000, K.read_killfeed_lines(frame(line), OWNER_TAG))
    gap.update(3_000_000, K.read_killfeed_lines(frame(line, 1), OWNER_TAG))
    assert gap.coverage()["reason"] == "feed_gap"
    unplaced = K.KillfeedTally()
    unplaced.update(None, K.read_killfeed_lines(frame(line), OWNER_TAG))
    assert unplaced.coverage()["kills"] is None
    fault = K.KillfeedTally()
    _play(fault, [frame(line, s) for s in range(4)])
    fault.fault()
    assert fault.coverage()["reason"] == "feed_read_fault"
    assert K.KillfeedTally().coverage()["reason"] == "no_feed_frames"


def test_unreadable_lasting_line_makes_coverage_unknown():
    tally = K.KillfeedTally()
    _play(tally, [frame([(440, "Qortrola31", GOLD, "Luna11", RED)], s) for s in range(6)])
    cov = tally.coverage()
    assert cov["coverage"] == "unknown" and cov["kills"] is None
    assert cov["reason"] == "unreadable_lines"


def test_lines_after_the_end_board_are_not_counted():
    tally = K.KillfeedTally()
    t = _play(tally, [frame([], s) for s in range(4)])
    tally.hold(t)
    _play(tally, [frame([(440, OWNER_TAG, GOLD, "Luna11", RED)], s) for s in range(6)], t0=t + 250_000)
    assert tally.coverage()["kills"] == 0


def test_killfeed_tally_feeds_the_three_leg_gate():
    tally = K.KillfeedTally()
    t = _play(tally, [frame([(440, OWNER_TAG, GOLD, "Luna11", RED)], s) for s in range(6)])
    tally.hold(t)
    feed = tally.coverage()
    assert feed["t_us"] == t
    board = {"calibrated": True, "kills": 1, "deaths": 0, "t_us": t + 100_000}
    end = {"calibrated": True, "outcome": "victory", "t_us": t + 200_000}
    stamps = [t, t + 100_000, t + 200_000]
    ok = cod_final_agreement(feed=feed, scoreboard=board, end_screen=end, frame_stamps=stamps)
    assert ok["counted"] is True
    bad = cod_final_agreement(feed=feed, scoreboard={**board, "kills": 2}, end_screen=end, frame_stamps=stamps)
    assert bad == {"counted": False, "status": bad["status"], "reason": "killfeed_scoreboard_mismatch"}
    late = cod_final_agreement(feed=feed, scoreboard={**board, "t_us": t + 900_000}, end_screen=end,
                               frame_stamps=stamps + [t + 900_000])
    assert late["reason"] == "agreement_window_expired"


def test_runner_uses_the_gamertag_tally_once_it_has_frames(monkeypatch):
    runner = CodSetRunner(lambda event: None)
    times = iter(range(1_000_000, 10_000_000, 250_000))
    monkeypatch.setattr(runner, "_screen_us", lambda: next(times))
    assert runner.own_kill_coverage()["coverage"] == "unknown"
    for s in range(6):
        runner._read_kill_feed(frame([(440, OWNER_TAG, GOLD, "Luna11", RED)], s), end_screen=False)
    runner._read_kill_feed(None, end_screen=True)
    cov = runner.own_kill_coverage()
    assert cov["source"] == "killfeed_gamertag"
    assert cov["coverage"] == "covered" and cov["kills"] == 1


def test_runner_keeps_legacy_path_without_an_image():
    runner = CodSetRunner(lambda event: None)
    runner._read_kill_feed(object(), end_screen=False)
    assert runner._kill_tally.frames == 0
    assert runner.own_kill_coverage()["ownership_qualified"] is False
