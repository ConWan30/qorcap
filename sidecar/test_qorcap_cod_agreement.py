"""Synthetic three-leg agreement; no player capture or authority claims."""
import pytest
from qorcap_cod import cod_final_agreement, CodSetRunner, RegionRead, AGREED


def legs():
    return dict(feed={"coverage": "covered", "ownership_qualified": True, "deduped": True, "kills": 12, "t_us": 1000000},
                scoreboard={"calibrated": True, "kills": 12, "deaths": 3, "t_us": 1200000},
                end_screen={"calibrated": True, "outcome": "victory", "t_us": 1500000},
                frame_stamps=[1000000, 1200000, 1500000])


def test_three_legs_agree():
    assert cod_final_agreement(**legs()) == {"counted": True, "status": AGREED, "reason": "three_leg_agreement"}


@pytest.mark.parametrize("leg", ["feed", "scoreboard", "end_screen"])
def test_any_missing_leg_abstains(leg):
    args = legs(); args[leg] = None
    result = cod_final_agreement(**args)
    assert not result["counted"]
    assert result["reason"].startswith("missing_")


@pytest.mark.parametrize("change, reason", [
    ({"kills": 13}, "killfeed_scoreboard_mismatch"),
    ({"coverage": "unknown"}, "killfeed_coverage_unknown"),
    ({"ownership_qualified": False}, "killfeed_coverage_unknown"),
    ({"deduped": False}, "killfeed_coverage_unknown"),
    ({"kills": True}, "killfeed_coverage_unknown"),
    ({"t_us": 999999}, "agreement_window_expired"),
    ({"t_us": None}, "frame_clock_unknown"),
])
def test_bad_feed_abstains_with_reason(change, reason):
    args = legs(); args["feed"].update(change)
    assert cod_final_agreement(**args)["reason"] == reason
    assert not cod_final_agreement(**args)["counted"]


def test_missing_clock_and_conflicting_outcome():
    args = legs(); args["frame_stamps"] = []
    assert cod_final_agreement(**args)["reason"] == "frame_clock_unmatched"
    args = legs(); args["scoreboard"]["outcome"] = "defeat"
    assert cod_final_agreement(**args)["reason"] == "outcome_mismatch"


@pytest.mark.parametrize("cue", ["hitmarker", "kill_popup"])
def test_lone_cue_is_advisory(cue):
    runner = CodSetRunner(lambda event: None)
    signals = runner._signals(0, {cue: RegionRead(region=cue, status="read", reason="synthetic", ok=True, calibrated=True)})
    row = next(s for s in signals if s["signal"] == cue)
    assert row["advisory"] is True
    assert row["counted"] is False
    assert runner.final is None


def test_live_generic_icons_cannot_supply_own_kill_tally():
    runner = CodSetRunner(lambda event: None)
    runner._own_kills = [{"t_us": 1000000, "source_id": "synthetic-frame", "ownership_qualified": False}]
    args = legs(); args["feed"] = {**runner.own_kill_coverage(), "t_us": 1000000}
    assert cod_final_agreement(**args)["reason"] == "killfeed_coverage_unknown"


@pytest.mark.parametrize("feed_ok, eligibility_ok, counted", [(True, True, True), (False, True, False), (True, False, False)])
def test_posted_final_requires_both_agreement_and_existing_eligibility(monkeypatch, feed_ok, eligibility_ok, counted):
    import qorcap_cod_hud
    from qorcap_cod import OWNER_TAG
    posted = []
    runner = CodSetRunner(posted.append)
    runner.frame_stamps = [1000000]
    monkeypatch.setattr(runner, "_screen_us", lambda: 1000000)
    monkeypatch.setattr(runner, "_offer", lambda *a, **kw: {"t_ms": 0, "until_ms": 3000, "frames": 3})
    monkeypatch.setattr(runner, "final_eligibility", lambda **kw: {"confirmed": eligibility_ok})
    monkeypatch.setattr(qorcap_cod_hud, "consensus_owner_row", lambda *a: {"status": "accepted"})
    runner._last_feed_us = 1000000
    runner._own_kills = [{"t_us": 1000000, "source_id": "synthetic", "ownership_qualified": feed_ok}]
    end = RegionRead(region="end_screen", status="read", ok=True, calibrated=True, confirm=True, reason="victory")
    owner = RegionRead(region="owner_row", status="read", ok=True, calibrated=True, reason="synthetic",
                       player_tag=OWNER_TAG, kills=1, deaths=3)
    runner._post_owner_end(3000, 3000, end, owner)
    assert len(posted) == 1
    assert posted[0]["final_confirmed"] is counted
    assert posted[0]["counted"] is counted
    if not feed_ok:
        assert posted[0]["agreement"]["reason"] == "killfeed_coverage_unknown"
        assert any("killfeed_coverage_unknown" in note for note in posted[0]["notes"])
