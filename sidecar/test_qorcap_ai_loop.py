"""On-device learning loop. Fixtures are synthetic: this repo has no published match bundle."""
import hashlib
import json
from pathlib import Path

import pytest

import qorcap_ai_loop as loop

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FX = json.loads((HERE / "fixtures" / "ai_loop" / "synthetic_loop.json").read_text(encoding="utf-8"))
SCHEMA = ROOT / "schema"


def _proposal(name):
    return json.loads(json.dumps(FX["proposals"][name]))


def _view(name):
    return json.loads(json.dumps(FX["views"][name]))


def test_fixture_is_labelled_synthetic():
    assert FX["synthetic"] is True
    assert "Invented" in FX["note"]
    assert "not played" in FX["note"]


def test_confirmed_and_contradicted_pairing(tmp_path):
    proposal = _proposal("confirmed")
    path = tmp_path / loop.PROPOSALS_FILENAME
    written = loop.write_proposals(path, [proposal])
    assert loop.read_jsonl(path) == written

    confirmed = loop.join_outcomes([proposal], referee=_view("pass_match"))[0]
    assert confirmed["outcome"] == "confirmed"
    assert confirmed["pairing"] == "confirmed"
    assert confirmed["reason"] == "value_match"
    assert confirmed["source"] == "referee"
    assert confirmed["weight"] == loop.REFEREE_WEIGHT
    assert confirmed["trainable"] is True
    assert confirmed["grounding"] == "not_applicable"
    assert confirmed["checks"] == [{"id": "final_reread", "ran": True, "state": "pass"}]
    assert confirmed["referee_outcome"] == "confirmed"

    failed = loop.join_outcomes([proposal], referee=_view("fail_match"))[0]
    assert failed["outcome"] == "contradicted"
    assert failed["reason"] == "check_failed"
    assert failed["trainable"] is True

    mismatch = loop.join_outcomes([proposal], referee=_view("value_mismatch"))[0]
    assert mismatch["outcome"] == "contradicted"
    assert mismatch["reason"] == "value_mismatch"
    assert mismatch["trainable"] is True

    partial = loop.join_outcomes([proposal], referee=_view("partial"))[0]
    assert partial["outcome"] == "unverifiable"
    assert partial["reason"] == "partial_check"
    assert partial["trainable"] is False

    prose = loop.join_outcomes([proposal], referee=_view("prose_only"))[0]
    assert prose["outcome"] == "unverifiable"
    assert prose["reason"] == "no_independent_value"
    assert prose["trainable"] is False


def test_abstain_is_not_a_label(tmp_path):
    proposal = _proposal("abstain")
    out_path = tmp_path / loop.OUTCOMES_FILENAME
    rows = loop.write_outcomes(out_path, [proposal], referee=_view("pass_match"))
    assert rows[0]["abstain"] is True
    assert rows[0]["outcome"] == "unverifiable"
    assert rows[0]["reason"] == "abstain"
    assert rows[0]["trainable"] is False
    assert "confidence_permille" not in rows[0]
    aligned = loop.align_for_gate(rows, rows)
    assert aligned[0]["candidate_correct"] is False
    assert aligned[0]["current_correct"] is False
    with pytest.raises(loop.SchemaError):
        loop.align_for_gate(rows, loop.join_outcomes([_proposal("confirmed")], referee=_view("pass_match")))


def test_ungrounded_explanation_is_a_miss(tmp_path):
    missing = loop.write_outcomes(tmp_path / "miss-run.jsonl", [_proposal("miss_not_run")], referee=_view("pass_match"))[0]
    assert missing["grounding"] == "miss"
    assert missing["outcome"] == "unverifiable"
    assert missing["reason"] == "grounding_miss"
    assert missing["trainable"] is False
    assert missing["misses"] == [{
        "check_id": "motion", "reason": "not_run", "claimed": "pass", "actual": None,
    }]

    wrong = loop.write_outcomes(tmp_path / "miss-state.jsonl", [_proposal("miss_state")], referee=_view("fail_match"))[0]
    assert wrong["grounding"] == "miss"
    assert wrong["trainable"] is False
    assert wrong["misses"] == [{
        "check_id": "final_reread", "reason": "state_mismatch", "claimed": "pass", "actual": "fail",
    }]

    grounded = loop.join_outcomes([_proposal("grounded")], referee=_view("fail_match"))[0]
    assert grounded["grounding"] == "grounded"
    assert grounded["outcome"] == "confirmed"
    assert grounded["pairing"] == "not_applicable"
    assert grounded["reason"] == "citations_match"
    assert grounded["trainable"] is True
    assert grounded["misses"] == []


def test_own_output_is_rejected_as_a_label(tmp_path):
    proposal = _proposal("confirmed")
    path = tmp_path / loop.OUTCOMES_FILENAME
    rejected = [
        {"schema": loop.LABEL_VIEW_SCHEMA, "source": "ai", "match_id": "synthetic-match", "checks": {}},
        {"schema": loop.LABEL_VIEW_SCHEMA, "source": "proposal", "match_id": "synthetic-match", "checks": {}},
        {"schema": loop.LABEL_VIEW_SCHEMA, "source": "model", "match_id": "synthetic-match", "checks": {}},
        proposal,
        {"schema": "qortime-referee-verdict/v0", "source": "referee", "verdict": "accept",
         "match_id": "synthetic-match", "checks": {}},
        {"schema": loop.STANCE_SCHEMA, "source": "referee", "stance": "accept",
         "match_id": "synthetic-match", "checks": {}},
        {"schema": loop.OUTCOME_SCHEMA, "source": "referee", "match_id": "synthetic-match", "checks": {}},
        {"schema": loop.CONSOLIDATION_SCHEMA, "source": "referee", "match_id": "synthetic-match", "checks": {}},
    ]
    for view in rejected:
        with pytest.raises(loop.OwnOutputRejected):
            loop.write_outcomes(path, [proposal], referee=view)
    assert not path.exists()


def test_owner_review_outranks_the_referee():
    proposal = _proposal("confirmed")
    row = loop.join_outcomes([proposal], referee=_view("pass_match"), human=_view("owner_disagrees"))[0]
    assert row["source"] == "human_review"
    assert row["weight"] == loop.HUMAN_REVIEW_WEIGHT
    assert row["outcome"] == "contradicted"
    assert row["reason"] == "value_mismatch"
    assert row["referee_outcome"] == "confirmed"
    assert row["trainable"] is True
    assert row["weight"] > loop.REFEREE_WEIGHT


def _gate_rows():
    hold = ["dualsense_edge", "xbox", "switch_pro", "pc"]
    rows = []
    for family in hold:
        rows.append({
            "match_id": "synthetic-match",
            "source": "referee",
            "game": "synthetic-game",
            "controller_family": family,
            "console": "hdmi-console",
            "candidate_correct": True,
            "current_correct": True,
        })
    rows.append({
        "match_id": "synthetic-match",
        "source": "referee",
        "game": "synthetic-game",
        "controller_family": "dualsense",
        "console": "hdmi-console",
        "candidate_correct": True,
        "current_correct": False,
    })
    return rows


def _versions():
    return {
        "candidate": {"model_version": "reader-1", "calibration_version": "cal-1"},
        "current": {"model_version": "reader-0", "calibration_version": "cal-0"},
    }


def test_promotion_gate_promotes_only_on_strict_improvement(tmp_path):
    versions = _versions()
    promoted = loop.write_consolidation(
        tmp_path / loop.CONSOLIDATION_FILENAME,
        rows=_gate_rows(),
        idle=True,
        during_capture=False,
        referee_identical=True,
        lock_age_s=30,
        **versions,
    )
    assert promoted["decision"] == "promoted"
    assert promoted["reason"] == "strict_improvement"
    assert promoted["during_capture"] is False
    assert promoted["metrics"]["candidate"]["accuracy_permille"] == 1000
    assert promoted["metrics"]["current"]["accuracy_permille"] == 800
    families = {row["controller_family"] for row in promoted["per_context"]}
    assert families == {"dualsense", "dualsense_edge", "xbox", "switch_pro", "pc"}
    assert all(row["regression"] is False for row in promoted["per_context"])
    on_disk = json.loads((tmp_path / loop.CONSOLIDATION_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["decision"] == "promoted"

    regressed_rows = [
        {
            "match_id": "synthetic-match",
            "source": "human_review",
            "game": "synthetic-game",
            "controller_family": "dualsense",
            "console": "hdmi-console",
            "candidate_correct": True,
            "current_correct": False,
        },
        {
            "match_id": "synthetic-match",
            "source": "referee",
            "game": "synthetic-game",
            "controller_family": "xbox",
            "console": "hdmi-console",
            "candidate_correct": False,
            "current_correct": True,
        },
    ]
    regressed = loop.promotion_gate(rows=regressed_rows, idle=True, during_capture=False,
                                    referee_identical=True, **versions)
    assert regressed["decision"] == "discarded"
    assert regressed["reason"] == "context_regression"
    assert regressed["metrics"]["candidate"]["correct_weight"] == 3
    assert regressed["metrics"]["candidate"]["total_weight"] == 4
    assert regressed["metrics"]["current"]["correct_weight"] == 1
    by_family = {row["controller_family"]: row for row in regressed["per_context"]}
    assert by_family["xbox"]["regression"] is True
    assert by_family["dualsense"]["regression"] is False
    assert regressed["labelled_matches"][0]["weight"] == loop.HUMAN_REVIEW_WEIGHT
    assert regressed["labelled_matches"][1]["weight"] == loop.REFEREE_WEIGHT

    tied = loop.promotion_gate(rows=[
        {"match_id": "synthetic-a", "source": "referee", "game": "synthetic-game",
         "controller_family": "dualsense", "console": "hdmi-console",
         "candidate_correct": True, "current_correct": True},
        {"match_id": "synthetic-b", "source": "referee", "game": "synthetic-game",
         "controller_family": "dualsense", "console": "hdmi-console",
         "candidate_correct": False, "current_correct": False},
    ], idle=True, during_capture=False, referee_identical=True, **versions)
    assert tied["decision"] == "discarded"
    assert tied["reason"] == "not_strict_improvement"
    assert tied["metrics"]["candidate"]["accuracy_permille"] == tied["metrics"]["current"]["accuracy_permille"]

    idle_off = loop.promotion_gate(rows=_gate_rows(), idle=False, during_capture=False,
                                   referee_identical=True, **versions)
    assert idle_off["decision"] == "discarded"
    assert idle_off["reason"] == "not_idle"
    assert idle_off["metrics"]["candidate"]["accuracy_permille"] == 1000

    live = loop.promotion_gate(rows=_gate_rows(), idle=True, during_capture=True,
                               referee_identical=True, **versions)
    assert live["reason"] == "during_capture"
    fresh_lock = loop.promotion_gate(rows=_gate_rows(), idle=True, during_capture=False,
                                     referee_identical=True, lock_age_s=5, **versions)
    assert fresh_lock["reason"] == "during_capture"
    assert fresh_lock["during_capture"] is True

    drift = loop.promotion_gate(rows=_gate_rows(), idle=True, during_capture=False,
                                referee_identical=False, **versions)
    assert drift["reason"] == "referee_checks_differ"
    assert drift["decision"] == "discarded"

    empty = loop.promotion_gate(rows=[], idle=True, during_capture=False,
                                referee_identical=True, **versions)
    assert empty["reason"] == "no_labelled_matches"


def test_ai_off_baseline_requires_identical_check_states():
    states = {"final_reread": "pass", "motion": "partial", "input_integrity": "pass"}
    assert loop.referee_checks_identical(states, dict(states)) is True
    assert loop.referee_checks_identical(states, {**states, "motion": "pass"}) is False
    assert loop.referee_checks_identical(states, {"final_reread": "pass", "motion": "partial"}) is False


def test_stance_file_stays_out_of_the_receipt(tmp_path):
    stance = loop.write_stance(
        tmp_path / loop.STANCE_FILENAME,
        match_id="synthetic-match",
        model_version="reader-0",
        calibration_version="cal-0",
        stance="abstain",
        proposal_ids=["p-abstain"],
    )
    assert stance["schema"] == loop.STANCE_SCHEMA
    assert stance["stance"] == "abstain"
    assert stance["note"] == loop.STANCE_NOTE
    assert json.loads((tmp_path / loop.STANCE_FILENAME).read_text(encoding="utf-8"))["note"] == loop.STANCE_NOTE
    with pytest.raises(loop.OwnOutputRejected):
        loop.join_outcomes([_proposal("confirmed")], referee=stance)


def test_receipt_v0_is_untouched(tmp_path):
    receipt = ROOT / "contracts" / "receipt.v0.json"
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    sums = (ROOT / "contracts" / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"{digest}  receipt.v0.json" in sums
    schema = json.loads(receipt.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    for name in ("ai_proposals", "ai_outcomes", "consolidation", "ai_stance", "explanation"):
        assert name not in schema["properties"]

    bundle = tmp_path / "match"
    bundle.mkdir()
    raw = b'{"schema":"qor-receipt/v0"}\n'
    (bundle / "receipt.json").write_bytes(raw)
    loop.write_proposals(bundle / loop.PROPOSALS_FILENAME, [_proposal("confirmed")])
    loop.write_outcomes(bundle / loop.OUTCOMES_FILENAME, [_proposal("confirmed")], referee=_view("pass_match"))
    loop.write_stance(bundle / loop.STANCE_FILENAME, match_id="synthetic-match", model_version="reader-0",
                      calibration_version="cal-0", stance="accept", proposal_ids=["p-confirmed"])
    loop.write_consolidation(bundle / loop.CONSOLIDATION_FILENAME, rows=_gate_rows(), idle=True,
                             during_capture=False, referee_identical=True, **_versions())
    assert (bundle / "receipt.json").read_bytes() == raw
    assert hashlib.sha256(receipt.read_bytes()).hexdigest() == digest
    with pytest.raises(loop.LoopError):
        loop.write_proposals(bundle / "receipt.json", [_proposal("confirmed")])
    assert (bundle / "receipt.json").read_bytes() == raw


def test_json_schemas_match_the_module():
    def load(name):
        schema = json.loads((SCHEMA / name).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        return schema

    proposal = load("ai_proposal.v0.json")
    assert set(proposal["properties"]) == set(loop.PROPOSAL_PROPERTIES)
    assert set(proposal["required"]) == set(loop.PROPOSAL_REQUIRED)
    assert set(proposal["$defs"]["explanation"]["properties"]) == set(loop.EXPLANATION_PROPERTIES)
    cite = proposal["$defs"]["explanation"]["properties"]["citations"]["items"]
    assert set(cite["properties"]) == set(loop.CITATION_PROPERTIES)

    outcome = load("ai_outcome.v0.json")
    assert set(outcome["properties"]) == set(loop.OUTCOME_PROPERTIES)
    assert set(outcome["required"]) == set(loop.OUTCOME_PROPERTIES)
    assert set(outcome["properties"]["checks"]["items"]["properties"]) == set(loop.CHECK_ROW_PROPERTIES)
    assert set(outcome["properties"]["misses"]["items"]["properties"]) == set(loop.MISS_PROPERTIES)

    consolidation = load("ai_consolidation.v0.json")
    assert set(consolidation["properties"]) == set(loop.CONSOLIDATION_PROPERTIES)
    assert set(consolidation["required"]) == set(loop.CONSOLIDATION_PROPERTIES)
    assert set(consolidation["$defs"]["version"]["properties"]) == set(loop.VERSION_PROPERTIES)
    assert set(consolidation["$defs"]["metric"]["properties"]) == set(loop.METRIC_PROPERTIES)
    assert set(consolidation["$defs"]["labelled"]["properties"]) == set(loop.LABELLED_MATCH_PROPERTIES)
    assert set(consolidation["$defs"]["context_row"]["properties"]) == set(loop.PER_CONTEXT_PROPERTIES)

    stance = load("ai_stance.v0.json")
    assert set(stance["properties"]) == set(loop.STANCE_PROPERTIES)
    assert stance["properties"]["note"]["const"] == loop.STANCE_NOTE

    label = load("ai_label_view.v0.json")
    assert set(label["properties"]) == set(loop.LABEL_VIEW_PROPERTIES)
    assert set(label["$defs"]["check"]["properties"]) == set(loop.LABEL_CHECK_PROPERTIES)
    assert set(label["$defs"]["check"]["required"]) == set(loop.LABEL_CHECK_REQUIRED)

    assert loop.REVIEW_CHECK_IDS == (
        "bundle_structure", "capture_chain", "video_bytes", "final_reread",
        "challenges", "motion", "input_integrity",
    )
    doc = (ROOT / "docs" / "AI_LOOP.md").read_text(encoding="utf-8")
    for check_id in loop.REVIEW_CHECK_IDS:
        assert check_id in doc


def test_new_text_avoids_banned_status_words():
    paths = [
        ROOT / "docs" / "AI_LOOP.md",
        HERE / "qorcap_ai_loop.py",
        HERE / "fixtures" / "ai_loop" / "synthetic_loop.json",
        *SCHEMA.glob("ai_*.json"),
    ]
    blob = "\n".join(p.read_text(encoding="utf-8") for p in paths).lower()
    assert "attested" not in blob
    assert "hardware-verified" not in blob
    assert "hardware verified" not in blob
    src = (HERE / "qorcap_ai_loop.py").read_text(encoding="utf-8")
    for needle in ("SendOutputReport", "set_output_report", "write_feature", "hid_write"):
        assert needle not in src
