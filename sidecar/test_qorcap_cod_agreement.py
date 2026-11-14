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


def test_owner_row_abstain_carries_the_owner_step():
    """Synthetic reads. The abstain row names the owner step; it posts nothing."""
    posted = []
    runner = CodSetRunner(posted.append)
    end = RegionRead(region="end_screen", status="read", ok=True, calibrated=True, confirm=True, reason="defeat")
    owner = RegionRead(region="owner_row", status="not_read", ok=False, calibrated=False, reason="not_present")
    owner.stage = "tag_not_matched"
    runner._post_owner_end(3000, 3000, end, owner)
    assert posted == []
    assert runner._frame_end == {"status": "abstain", "reason": "owner_row_not_read", "owner_stage": "tag_not_matched"}
    assert runner.owner_row_ever_read is False
    bare = RegionRead(region="owner_row", status="not_read", ok=False, calibrated=False, reason="not_present")
    runner._post_owner_end(3500, 3500, end, bare)
    assert runner._frame_end == {"status": "abstain", "reason": "owner_row_not_read"}


def _owner_sequence(monkeypatch, reads):
    """Synthetic end-board reads. Only the agreement rule and stats bookkeeping are under test."""
    import qorcap_cod_hud
    from qorcap_cod import observation_from_runner, reader_stats_document
    posted = []
    runner = CodSetRunner(posted.append)
    runner.frame_stamps = [1000000]
    monkeypatch.setattr(runner, "_screen_us", lambda: 1000000)
    monkeypatch.setattr(runner, "final_eligibility", lambda **kw: {"confirmed": False})
    monkeypatch.setattr(qorcap_cod_hud, "consensus_owner_row", lambda *a: {"status": "accepted"})
    end = RegionRead(region="end_screen", status="read", ok=True, calibrated=True, confirm=True, reason="defeat")
    states = []
    for t, owner in reads:
        runner._post_owner_end(t, t, end, owner)
        states.append(runner._frame_end["status"])
    doc = reader_stats_document(observation_from_runner(runner), reader_join="joined")
    return runner, posted, states, doc


def _good_owner():
    from qorcap_cod import OWNER_TAG
    return RegionRead(region="owner_row", status="read", ok=True, calibrated=True, reason="synthetic",
                      player_tag=OWNER_TAG, kills=14, deaths=17)


def _missed_owner(stage):
    owner = RegionRead(region="owner_row", status="not_read", ok=False, calibrated=False, reason="not_present")
    owner.stage = stage
    if stage == "kills_not_read":
        owner.stage_detail = {"column": "kills", "check": "glyph_gate", "top": 0.7, "second": 0.6}
    return owner


def test_reader_stats_count_five_good_owner_frames_then_accept(monkeypatch):
    """210612 shape: two tag misses, then five good rows in a row across 3011 ms."""
    times = [557848, 558381, 558873, 559861, 560605, 561097, 561884]
    reads = [(times[0], _missed_owner("tag_not_matched")), (times[1], _missed_owner("tag_not_matched"))]
    reads += [(t, _good_owner()) for t in times[2:]]
    runner, posted, states, doc = _owner_sequence(monkeypatch, reads)
    assert states == ["abstain", "abstain", "not_ready", "not_ready", "not_ready", "not_ready", "accepted"]
    assert len(posted) == 1 and runner.final is not None
    assert (runner.owner_row_ever_read, runner.owner_row_good_frames, runner.owner_row_accepted) == (True, 5, True)
    assert (doc["owner_row_ever_read"], doc["owner_row_good_frames"], doc["owner_row_accepted"]) == (True, 5, True)


def test_reader_stats_keep_one_good_owner_frame_unaccepted(monkeypatch):
    """211713 shape: one good row among nine tag-matched frames is read once, not accepted."""
    times = [748124, 748409, 748900, 749639, 750379, 751119, 751865, 752650, 753392, 754135, 754884]
    stages = ["tag_not_matched", "tag_not_matched"] + ["kills_not_read"] * 5
    reads = [(t, _missed_owner(s)) for t, s in zip(times, stages)]
    reads.append((times[7], _good_owner()))
    reads += [(t, _missed_owner("kills_not_read")) for t in times[8:]]
    runner, posted, states, doc = _owner_sequence(monkeypatch, reads)
    assert states == ["abstain"] * 7 + ["not_ready"] + ["abstain"] * 3
    assert posted == [] and runner.final is None
    assert runner._frame_end["owner_stage_detail"] == {
        "column": "kills", "check": "glyph_gate", "top": 0.7, "second": 0.6}
    assert (runner.owner_row_ever_read, runner.owner_row_good_frames, runner.owner_row_accepted) == (True, 1, False)
    assert (doc["owner_row_ever_read"], doc["owner_row_good_frames"], doc["owner_row_accepted"]) == (True, 1, False)


def _unknown_deaths_owner(card_match=True):
    from qorcap_cod import OWNER_TAG
    owner = RegionRead(region="owner_row", status="read", ok=True, calibrated=True, reason="synthetic",
                       player_tag=OWNER_TAG, kills=17, deaths=None)
    owner.deaths_status = "UNKNOWN_NO_COLUMN"
    owner.card_name_match = card_match
    owner.deaths_unknown_reason = "card_digits_failed" if card_match else "card_name_mismatch"
    return owner


def test_unknown_deaths_row_posts_with_null_deaths_and_says_so(monkeypatch):
    """214647 shape: kills 17 on a board with no DEATHS column. Deaths stay None, never zero."""
    times = [1000, 2000, 3000, 4100]
    runner, posted, states, doc = _owner_sequence(monkeypatch, [(t, _unknown_deaths_owner()) for t in times])
    assert states[-1] == "accepted" and len(posted) == 1
    owner = posted[0]["owner_row"]
    assert owner["kills"] == 17 and owner["deaths"] is None
    assert owner["deaths_status"] == "UNKNOWN_NO_COLUMN" and owner["card_name_match"] is True
    assert owner["deaths_unknown_reason"] == "card_digits_failed"
    assert any("Deaths are UNKNOWN" in note for note in posted[0]["notes"])
    assert not any("deaths None" in note for note in posted[0]["notes"])
    assert posted[0]["counted"] is False
    assert (doc["owner_row_good_frames"], doc["owner_row_accepted"]) == (len(times), True)


def test_null_deaths_without_unknown_status_stays_unread(monkeypatch):
    from qorcap_cod import OWNER_TAG
    owner = RegionRead(region="owner_row", status="read", ok=True, calibrated=True, reason="synthetic",
                       player_tag=OWNER_TAG, kills=17, deaths=None)
    runner, posted, states, _doc = _owner_sequence(monkeypatch, [(t, owner) for t in (1000, 2000, 3000, 4100)])
    assert states == ["abstain"] * 4 and posted == []
    assert runner.owner_row_good_frames == 0


def test_deaths_status_change_restarts_the_agreement(monkeypatch):
    """Stability is kills plus the deaths status: a column read and an UNKNOWN read do not agree."""
    reads = [(1000, _good_owner()), (2000, _unknown_deaths_owner()), (3000, _good_owner()),
             (4100, _unknown_deaths_owner())]
    _runner, posted, states, _doc = _owner_sequence(monkeypatch, reads)
    assert "accepted" not in states and posted == []


def test_column_board_owner_doc_keeps_its_shape(monkeypatch):
    times = [1000, 2000, 3000, 4100]
    _runner, posted, _states, _doc = _owner_sequence(monkeypatch, [(t, _good_owner()) for t in times])
    assert set(posted[0]["owner_row"]) == {"tag", "kills", "deaths", "assists", "confirms", "result"}
    assert posted[0]["owner_row"]["deaths"] == 17


def test_unknown_deaths_agreement_abstains_with_an_honest_reason():
    args = legs(); args["feed"]["kills"] = 12
    args["scoreboard"].update({"deaths": None, "deaths_status": "UNKNOWN_NO_COLUMN"})
    from qorcap_cod import NOT_COUNTED
    assert cod_final_agreement(**args) == {
        "counted": False, "status": NOT_COUNTED, "reason": "scoreboard_deaths_unknown"}
    args = legs(); args["scoreboard"]["deaths"] = None
    assert cod_final_agreement(**args)["reason"] == "scoreboard_not_read"


def test_public_owner_row_accepts_unknown_deaths_only_with_the_status():
    from qorcap_cod import _public_owner_row
    base = {"tag": "Qortrola30", "kills": 17, "deaths": None, "assists": None, "confirms": 5, "result": "victory"}
    assert _public_owner_row(base) == {}
    out = _public_owner_row({**base, "deaths_status": "UNKNOWN_NO_COLUMN", "card_name_match": True})
    assert out["kills"] == 17 and out["deaths"] is None
    assert out["deaths_status"] == "UNKNOWN_NO_COLUMN" and out["card_name_match"] is True
    assert "UNKNOWN" in out["note"]
    column = _public_owner_row({**base, "deaths": 4})
    assert "deaths_status" not in column and "card_name_match" not in column


@pytest.mark.parametrize("bad", [
    {"deaths": 4, "deaths_status": "UNKNOWN_NO_COLUMN"},
    {"deaths": None},
    {"deaths": None, "deaths_status": "column"},
    {"deaths": 4, "deaths_status": "personal_card", "card_name_match": False},
    {"deaths": 4, "deaths_status": "personal_card"},
    {"deaths": None, "deaths_status": "personal_card", "card_name_match": True},
    {"deaths": "4", "deaths_status": "UNKNOWN_NO_COLUMN"},
    {"deaths": 4, "deaths_status": "guessed"},
])
def test_public_owner_row_checks_deaths_and_status_both_ways(bad):
    """A hand-edited or corrupt signals row gives no public owner row, never a mislabelled number."""
    from qorcap_cod import _public_owner_row
    base = {"tag": "Qortrola30", "kills": 17, "assists": None, "confirms": 5, "result": "victory"}
    assert _public_owner_row({**base, **bad}) == {}


def test_public_owner_row_carries_the_unknown_reason():
    from qorcap_cod import _public_owner_row
    base = {"tag": "Qortrola30", "kills": 17, "deaths": None, "assists": None, "confirms": 5,
            "result": "victory", "deaths_status": "UNKNOWN_NO_COLUMN", "card_name_match": False}
    assert _public_owner_row({**base, "deaths_unknown_reason": "card_name_mismatch"})[
        "deaths_unknown_reason"] == "card_name_mismatch"
    assert _public_owner_row({**base, "deaths_unknown_reason": "made_up"})["deaths_unknown_reason"] is None
    card = _public_owner_row({**base, "deaths": 13, "deaths_status": "personal_card", "card_name_match": True})
    assert card["deaths"] == 13 and "deaths_unknown_reason" not in card
