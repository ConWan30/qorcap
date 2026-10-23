"""On-device learning loop. See docs/AI_LOOP.md.

The model writes proposals and a stance. Referee check states and the owner's
review are the only label sources. This module does not score a receipt and
does not open a controller.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROPOSAL_SCHEMA = "qorcap-ai-proposal/v0"
OUTCOME_SCHEMA = "qorcap-ai-outcome/v0"
CONSOLIDATION_SCHEMA = "qorcap-ai-consolidation/v0"
STANCE_SCHEMA = "qorcap-ai-stance/v0"
LABEL_VIEW_SCHEMA = "qorcap-ai-label-view/v0"

PROPOSALS_FILENAME = "ai_proposals.jsonl"
OUTCOMES_FILENAME = "ai_outcomes.jsonl"
CONSOLIDATION_FILENAME = "consolidation.json"
STANCE_FILENAME = "ai_stance.json"

STANCE_NOTE = "Model stance. The receipt verdict stays with the referee."
MARKER_FRESH_S = 30  # sidecar/qorcap_highlights.py MARKER_FRESH_S
REFEREE_WEIGHT = 1
HUMAN_REVIEW_WEIGHT = 3

# review.checks keys from qor_receipt/review/engine.py (qortime 0204e6d and fe4b8dd).
REVIEW_CHECK_IDS = (
    "bundle_structure",
    "capture_chain",
    "video_bytes",
    "final_reread",
    "challenges",
    "motion",
    "input_integrity",
)

KINDS = ("score_read", "event_label", "clip_pick", "explanation")
STANCES = ("accept", "dispute", "abstain")
CHECK_STATES = ("pass", "partial", "fail")
OUTCOMES = ("confirmed", "contradicted", "unverifiable")
PAIRINGS = ("confirmed", "contradicted", "unverifiable", "not_applicable")
SOURCES = ("referee", "human_review")
GROUNDINGS = ("grounded", "miss", "not_applicable")
MISS_REASONS = ("not_run", "state_mismatch")
DECISIONS = ("promoted", "discarded")
GATE_REASONS = (
    "strict_improvement",
    "during_capture",
    "not_idle",
    "referee_checks_differ",
    "no_labelled_matches",
    "context_regression",
    "not_strict_improvement",
)
OUTCOME_REASONS = (
    "value_match",
    "value_mismatch",
    "check_failed",
    "check_not_run",
    "no_independent_value",
    "partial_check",
    "checks_incomplete",
    "abstain",
    "grounding_miss",
    "citations_match",
)

_FORBIDDEN_LABEL_SCHEMAS = frozenset({
    PROPOSAL_SCHEMA,
    OUTCOME_SCHEMA,
    STANCE_SCHEMA,
    CONSOLIDATION_SCHEMA,
    "qortime-referee-verdict/v0",
})
_FORBIDDEN_SOURCES = frozenset({
    "ai", "proposal", "model", "stance", "referee_ai", "referee-ai", "self",
})
_RECEIPT_NAMES = frozenset({"receipt.json", "receipt.v0.json", "receipt.draft.json"})

_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_LABEL = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_CHECK = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VALUE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SAFE_INT = 2**53 - 1

CONTEXT_KEYS = ("game", "controller_family", "console", "capture")
PROPOSAL_PROPERTIES = (
    "schema", "proposal_id", "match_id", "model_version", "calibration_version",
    "kind", "abstain", "confidence_permille", "context", "relates_to", "value",
    "explanation", "t_ms",
)
PROPOSAL_REQUIRED = (
    "schema", "proposal_id", "match_id", "model_version", "calibration_version",
    "kind", "abstain", "confidence_permille", "context", "relates_to", "value", "t_ms",
)
EXPLANATION_PROPERTIES = ("text", "citations")
CITATION_PROPERTIES = ("check_id", "state")
LABEL_VIEW_PROPERTIES = ("schema", "source", "match_id", "checks")
LABEL_CHECK_PROPERTIES = ("state", "value", "detail")
LABEL_CHECK_REQUIRED = ("state",)
OUTCOME_PROPERTIES = (
    "schema", "proposal_id", "match_id", "model_version", "calibration_version",
    "kind", "abstain", "context", "relates_to", "checks", "outcome", "pairing",
    "source", "weight", "trainable", "reason", "grounding", "misses", "referee_outcome",
)
CHECK_ROW_PROPERTIES = ("id", "ran", "state")
MISS_PROPERTIES = ("check_id", "reason", "claimed", "actual")
STANCE_PROPERTIES = (
    "schema", "match_id", "model_version", "calibration_version", "stance",
    "proposal_ids", "note",
)
VERSION_PROPERTIES = ("model_version", "calibration_version")
CONSOLIDATION_PROPERTIES = (
    "schema", "candidate", "current", "labelled_matches", "metrics", "per_context",
    "decision", "reason", "idle", "during_capture", "referee_identical",
)
LABELLED_MATCH_PROPERTIES = (
    "match_id", "source", "weight", "game", "controller_family", "console",
)
METRIC_PROPERTIES = ("correct_weight", "total_weight", "accuracy_permille")
PER_CONTEXT_PROPERTIES = (
    "game", "controller_family", "console", "candidate", "current", "regression",
)
ROW_PROPERTIES = (
    "match_id", "source", "game", "controller_family", "console",
    "candidate_correct", "current_correct",
)


class LoopError(ValueError):
    pass


class SchemaError(LoopError):
    pass


class OwnOutputRejected(LoopError):
    """A model document was offered as a label. The model's own outputs are not labels."""


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _dump(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _path(path: Path | str) -> Path:
    p = Path(path)
    if p.name in _RECEIPT_NAMES:
        raise LoopError(f"refusing to write {p.name}")
    return p


def _exact(obj: Any, allowed: tuple[str, ...], required: tuple[str, ...], what: str) -> dict:
    if not isinstance(obj, dict):
        raise SchemaError(what)
    extra = set(obj) - set(allowed)
    missing = set(required) - set(obj)
    if extra or missing:
        raise SchemaError(what)
    return obj


def _ident(v: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(v, str) or pattern.fullmatch(v) is None:
        raise SchemaError(what)
    return v


def _context(obj: Any) -> dict:
    raw = _exact(obj, CONTEXT_KEYS, CONTEXT_KEYS, "context")
    return {k: _ident(raw[k], _LABEL, f"context.{k}") for k in CONTEXT_KEYS}


def _atom(v: Any, what: str) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        if not v or len(v) > 64 or any(ord(ch) < 32 for ch in v):
            raise SchemaError(what)
        return v
    if _is_int(v) and 0 <= v <= _SAFE_INT:
        return v
    raise SchemaError(what)


def _value(obj: Any, what: str) -> dict | None:
    if obj is None:
        return None
    if not isinstance(obj, dict) or not obj or len(obj) > 16:
        raise SchemaError(what)
    out = {}
    for key, item in obj.items():
        if not isinstance(key, str) or _VALUE_KEY.fullmatch(key) is None:
            raise SchemaError(what)
        out[key] = _atom(item, what)
    return out


def _check_id_list(ids: Any, what: str) -> list[str]:
    if not isinstance(ids, list) or not ids or len(ids) > 16:
        raise SchemaError(what)
    out = []
    for item in ids:
        out.append(_ident(item, _CHECK, what))
    if len(set(out)) != len(out):
        raise SchemaError(what)
    return out


def _explanation(obj: Any) -> dict:
    raw = _exact(obj, EXPLANATION_PROPERTIES, EXPLANATION_PROPERTIES, "explanation")
    text = raw["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise SchemaError("explanation.text")
    cites = raw["citations"]
    if not isinstance(cites, list) or not cites or len(cites) > 16:
        raise SchemaError("explanation.citations")
    citations = []
    for item in cites:
        row = _exact(item, CITATION_PROPERTIES, CITATION_PROPERTIES, "citation")
        state = row["state"]
        if state not in CHECK_STATES:
            raise SchemaError("citation.state")
        citations.append({"check_id": _ident(row["check_id"], _CHECK, "citation.check_id"), "state": state})
    if len({c["check_id"] for c in citations}) != len(citations):
        raise SchemaError("explanation.citations")
    return {"text": text, "citations": citations}


def _versions(obj: Any, what: str) -> tuple[str, str]:
    return (
        _ident(obj.get("model_version") if isinstance(obj, dict) else None, _VERSION, what),
        _ident(obj.get("calibration_version") if isinstance(obj, dict) else None, _VERSION, what),
    )


def normalize_proposal(obj: Any) -> dict:
    raw = _exact(obj, PROPOSAL_PROPERTIES, PROPOSAL_REQUIRED, "proposal")
    if raw.get("schema") != PROPOSAL_SCHEMA:
        raise SchemaError("proposal.schema")
    kind = raw["kind"]
    if kind not in KINDS:
        raise SchemaError("proposal.kind")
    abstain = raw["abstain"]
    if not isinstance(abstain, bool):
        raise SchemaError("proposal.abstain")
    conf = raw["confidence_permille"]
    if abstain:
        if conf is not None:
            raise SchemaError("proposal.confidence_permille")
    elif not _is_int(conf) or not 0 <= conf <= 1000:
        raise SchemaError("proposal.confidence_permille")
    relates = _check_id_list(raw["relates_to"], "proposal.relates_to")
    value = _value(raw["value"], "proposal.value")
    if abstain and value is not None:
        raise SchemaError("proposal.value")
    explanation = None
    if "explanation" in raw:
        explanation = _explanation(raw["explanation"])
        if [c["check_id"] for c in explanation["citations"]] != relates:
            raise SchemaError("proposal.relates_to")
    if kind == "explanation" and explanation is None:
        raise SchemaError("proposal.explanation")
    if not abstain and kind != "explanation" and value is None:
        raise SchemaError("proposal.value")
    t_ms = raw["t_ms"]
    if not _is_int(t_ms) or t_ms < 0 or t_ms > _SAFE_INT:
        raise SchemaError("proposal.t_ms")
    model, calib = _versions(raw, "proposal.version")
    out = {
        "schema": PROPOSAL_SCHEMA,
        "proposal_id": _ident(raw["proposal_id"], _ID, "proposal.proposal_id"),
        "match_id": _ident(raw["match_id"], _ID, "proposal.match_id"),
        "model_version": model,
        "calibration_version": calib,
        "kind": kind,
        "abstain": abstain,
        "confidence_permille": None if abstain else conf,
        "context": _context(raw["context"]),
        "relates_to": relates,
        "value": value,
        "t_ms": t_ms,
    }
    if explanation is not None:
        out["explanation"] = explanation
    return out


def _label_checks(obj: Any) -> dict[str, dict]:
    if not isinstance(obj, dict) or len(obj) > 32:
        raise SchemaError("checks")
    out: dict[str, dict] = {}
    for key, item in obj.items():
        cid = _ident(key, _CHECK, "checks")
        raw = _exact(item, LABEL_CHECK_PROPERTIES, LABEL_CHECK_REQUIRED, f"checks.{cid}")
        state = raw["state"]
        if state not in CHECK_STATES:
            raise SchemaError(f"checks.{cid}.state")
        row: dict[str, Any] = {"state": state}
        if "value" in raw:
            row["value"] = _value(raw["value"], f"checks.{cid}.value")
        if "detail" in raw and raw["detail"] is not None:
            detail = raw["detail"]
            if not isinstance(detail, str) or len(detail) > 400:
                raise SchemaError(f"checks.{cid}.detail")
            row["detail"] = detail
        out[cid] = row
    return out


def normalize_label_view(obj: Any) -> dict:
    if not isinstance(obj, dict):
        raise SchemaError("label view")
    schema = obj.get("schema")
    source = obj.get("source")
    if schema in _FORBIDDEN_LABEL_SCHEMAS or source in _FORBIDDEN_SOURCES:
        raise OwnOutputRejected("model output cannot be a label")
    raw = _exact(obj, LABEL_VIEW_PROPERTIES, LABEL_VIEW_PROPERTIES, "label view")
    if raw["schema"] != LABEL_VIEW_SCHEMA:
        raise SchemaError("label view.schema")
    if raw["source"] not in SOURCES:
        raise OwnOutputRejected("model output cannot be a label")
    return {
        "schema": LABEL_VIEW_SCHEMA,
        "source": raw["source"],
        "match_id": _ident(raw["match_id"], _ID, "label view.match_id"),
        "checks": _label_checks(raw["checks"]),
    }


def _check_rows(relates: list[str], checks: dict[str, dict]) -> list[dict]:
    rows = []
    for cid in relates:
        ran = checks.get(cid)
        if ran is None:
            rows.append({"id": cid, "ran": False, "state": None})
        else:
            rows.append({"id": cid, "ran": True, "state": ran["state"]})
    return rows


def ground_explanation(proposal: dict, checks: dict[str, dict]) -> dict:
    """Compare citations to checks that ran. A miss is logged on the returned object."""
    explanation = proposal.get("explanation")
    if not explanation:
        if proposal["kind"] == "explanation":
            return {"grounding": "miss", "misses": []}
        return {"grounding": "not_applicable", "misses": []}
    misses = []
    for cite in explanation["citations"]:
        cid = cite["check_id"]
        claimed = cite["state"]
        ran = checks.get(cid)
        if ran is None:
            misses.append({"check_id": cid, "reason": "not_run", "claimed": claimed, "actual": None})
        elif ran["state"] != claimed:
            misses.append({
                "check_id": cid, "reason": "state_mismatch", "claimed": claimed, "actual": ran["state"],
            })
    if misses:
        return {"grounding": "miss", "misses": misses}
    return {"grounding": "grounded", "misses": []}


def _pair(proposal: dict, checks: dict[str, dict]) -> tuple[str, str]:
    if proposal["kind"] == "explanation":
        return "not_applicable", "citations_match"
    results = []
    for cid in proposal["relates_to"]:
        ran = checks.get(cid)
        if ran is None:
            results.append(("unverifiable", "check_not_run"))
            continue
        state = ran["state"]
        if state == "fail":
            results.append(("contradicted", "check_failed"))
            continue
        if state == "partial":
            results.append(("unverifiable", "partial_check"))
            continue
        if state != "pass":
            results.append(("unverifiable", "check_not_run"))
            continue
        value = ran.get("value") if "value" in ran else None
        if value is None or proposal["value"] is None:
            results.append(("unverifiable", "no_independent_value"))
        elif value == proposal["value"]:
            results.append(("confirmed", "value_match"))
        else:
            results.append(("contradicted", "value_mismatch"))
    if any(item[0] == "contradicted" for item in results):
        reason = "check_failed" if any(item[1] == "check_failed" for item in results) else "value_mismatch"
        return "contradicted", reason
    if results and all(item[0] == "confirmed" for item in results):
        return "confirmed", "value_match"
    order = ("check_not_run", "no_independent_value", "partial_check")
    reasons = [item[1] for item in results]
    for name in order:
        if name in reasons:
            return "unverifiable", name
    return "unverifiable", "checks_incomplete"


def _finish(proposal: dict, label_checks: dict, grounding_checks: dict, source: str,
            referee_outcome: str) -> dict:
    grounding = ground_explanation(proposal, grounding_checks)
    pairing, pair_reason = _pair(proposal, label_checks)
    if proposal["abstain"]:
        outcome = "unverifiable"
        reason = "grounding_miss" if grounding["grounding"] == "miss" else "abstain"
        trainable = False
        if proposal["kind"] == "explanation":
            pairing = "not_applicable"
    elif grounding["grounding"] == "miss":
        outcome = "unverifiable"
        reason = "grounding_miss"
        trainable = False
    elif proposal["kind"] == "explanation":
        outcome = "confirmed"
        reason = "citations_match"
        trainable = True
        pairing = "not_applicable"
    else:
        outcome = pairing
        reason = pair_reason
        trainable = outcome in ("confirmed", "contradicted")
    weight = HUMAN_REVIEW_WEIGHT if source == "human_review" else REFEREE_WEIGHT
    return {
        "schema": OUTCOME_SCHEMA,
        "proposal_id": proposal["proposal_id"],
        "match_id": proposal["match_id"],
        "model_version": proposal["model_version"],
        "calibration_version": proposal["calibration_version"],
        "kind": proposal["kind"],
        "abstain": proposal["abstain"],
        "context": proposal["context"],
        "relates_to": list(proposal["relates_to"]),
        "checks": _check_rows(proposal["relates_to"], grounding_checks),
        "outcome": outcome,
        "pairing": pairing,
        "source": source,
        "weight": weight,
        "trainable": trainable,
        "reason": reason,
        "grounding": grounding["grounding"],
        "misses": grounding["misses"],
        "referee_outcome": referee_outcome,
    }


def _referee_only_outcome(proposal: dict, view: dict) -> str:
    """Outcome word the referee view alone would have produced."""
    return _finish(proposal, view["checks"], view["checks"], "referee", "absent")["outcome"]


def join_outcomes(proposals: list[dict], *, referee: dict | None = None,
                  human: dict | None = None) -> list[dict]:
    """Pair proposals with referee and/or owner label views. Raises before returning on a bad label."""
    if referee is None and human is None:
        raise SchemaError("label view")
    ref = normalize_label_view(referee) if referee is not None else None
    own = normalize_label_view(human) if human is not None else None
    if ref is not None and own is not None and ref["match_id"] != own["match_id"]:
        raise SchemaError("match_id")
    match_id = (own or ref)["match_id"]
    if not isinstance(proposals, list) or not proposals:
        raise SchemaError("proposals")
    normalized = [normalize_proposal(p) for p in proposals]
    ids = [p["proposal_id"] for p in normalized]
    if len(set(ids)) != len(ids):
        raise SchemaError("proposal_id")
    for proposal in normalized:
        if proposal["match_id"] != match_id:
            raise SchemaError("match_id")
    label = own or ref
    assert label is not None
    grounding_checks = ref["checks"] if ref is not None else label["checks"]
    rows = []
    for proposal in normalized:
        referee_outcome = _referee_only_outcome(proposal, ref) if ref is not None else "absent"
        # Citations are judged against the referee whenever it ran. The owner's
        # view supplies the value label for score, event, and clip proposals.
        if proposal["kind"] == "explanation" and ref is not None:
            source = "referee"
            label_checks = ref["checks"]
        else:
            source = label["source"]
            label_checks = label["checks"]
        rows.append(_finish(proposal, label_checks, grounding_checks, source, referee_outcome))
    for row in rows:
        _validate_outcome(row)
    return rows


def _validate_outcome(obj: dict) -> None:
    raw = _exact(obj, OUTCOME_PROPERTIES, OUTCOME_PROPERTIES, "outcome")
    if raw["schema"] != OUTCOME_SCHEMA:
        raise SchemaError("outcome.schema")
    if raw["kind"] not in KINDS or raw["outcome"] not in OUTCOMES or raw["pairing"] not in PAIRINGS:
        raise SchemaError("outcome")
    if raw["source"] not in SOURCES or raw["reason"] not in OUTCOME_REASONS:
        raise SchemaError("outcome")
    if raw["grounding"] not in GROUNDINGS or not isinstance(raw["abstain"], bool):
        raise SchemaError("outcome")
    if not isinstance(raw["trainable"], bool):
        raise SchemaError("outcome")
    expect = HUMAN_REVIEW_WEIGHT if raw["source"] == "human_review" else REFEREE_WEIGHT
    if raw["weight"] != expect:
        raise SchemaError("outcome.weight")
    if raw["referee_outcome"] not in OUTCOMES and raw["referee_outcome"] != "absent":
        raise SchemaError("outcome.referee_outcome")
    if raw["trainable"] and raw["outcome"] not in ("confirmed", "contradicted"):
        raise SchemaError("outcome.trainable")
    if raw["grounding"] == "miss" and raw["trainable"]:
        raise SchemaError("outcome.trainable")
    if not isinstance(raw["misses"], list):
        raise SchemaError("outcome.misses")
    for miss in raw["misses"]:
        _exact(miss, MISS_PROPERTIES, MISS_PROPERTIES, "miss")
        if miss["reason"] not in MISS_REASONS or miss["claimed"] not in CHECK_STATES:
            raise SchemaError("miss")
        if miss["actual"] is not None and miss["actual"] not in CHECK_STATES:
            raise SchemaError("miss.actual")


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_dump(obj) + "\n")


def read_jsonl(path: Path | str) -> list[dict]:
    rows = []
    text = Path(path).read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"line {i}") from exc
        if not isinstance(obj, dict):
            raise SchemaError(f"line {i}")
        rows.append(obj)
    return rows


def write_proposals(path: Path | str, proposals: list[dict]) -> list[dict]:
    path = _path(path)
    if not isinstance(proposals, list) or not proposals:
        raise SchemaError("proposals")
    rows = [normalize_proposal(p) for p in proposals]
    if len({p["proposal_id"] for p in rows}) != len(rows):
        raise SchemaError("proposal_id")
    for row in rows:
        _append(path, row)
    return rows


def write_outcomes(path: Path | str, proposals: list[dict], *, referee: dict | None = None,
                   human: dict | None = None) -> list[dict]:
    rows = join_outcomes(proposals, referee=referee, human=human)
    path = _path(path)
    for row in rows:
        _append(path, row)
    return rows


def write_stance(path: Path | str, *, match_id: str, model_version: str, calibration_version: str,
                 stance: str, proposal_ids: list[str]) -> dict:
    if stance not in STANCES:
        raise SchemaError("stance")
    if not isinstance(proposal_ids, list) or not proposal_ids:
        raise SchemaError("proposal_ids")
    clean_ids = []
    for item in proposal_ids:
        clean_ids.append(_ident(item, _ID, "proposal_ids"))
    if len(set(clean_ids)) != len(clean_ids):
        raise SchemaError("proposal_ids")
    out = {
        "schema": STANCE_SCHEMA,
        "match_id": _ident(match_id, _ID, "match_id"),
        "model_version": _ident(model_version, _VERSION, "model_version"),
        "calibration_version": _ident(calibration_version, _VERSION, "calibration_version"),
        "stance": stance,
        "proposal_ids": clean_ids,
        "note": STANCE_NOTE,
    }
    _exact(out, STANCE_PROPERTIES, STANCE_PROPERTIES, "stance")
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(out) + "\n", encoding="utf-8")
    return out


def referee_checks_identical(ai_on: dict, ai_off: dict) -> bool:
    """True when both maps have the same check ids and the same pass/partial/fail states."""
    if not isinstance(ai_on, dict) or not isinstance(ai_off, dict):
        return False
    if set(ai_on) != set(ai_off):
        return False
    for key, state in ai_on.items():
        if not isinstance(key, str) or state not in CHECK_STATES or ai_off[key] != state:
            return False
    return True


def _metric(correct: int, total: int) -> dict:
    permille = 0 if total == 0 else (correct * 1000) // total
    return {"correct_weight": correct, "total_weight": total, "accuracy_permille": permille}


def _version_obj(obj: Any, what: str) -> dict:
    raw = _exact(obj, VERSION_PROPERTIES, VERSION_PROPERTIES, what)
    model, calib = _versions(raw, what)
    return {"model_version": model, "calibration_version": calib}


def _rows(rows: Any) -> list[dict]:
    if not isinstance(rows, list):
        raise SchemaError("rows")
    out = []
    for item in rows:
        raw = _exact(item, ROW_PROPERTIES, ROW_PROPERTIES, "row")
        source = raw["source"]
        if source not in SOURCES:
            raise SchemaError("row.source")
        if not isinstance(raw["candidate_correct"], bool) or not isinstance(raw["current_correct"], bool):
            raise SchemaError("row.correct")
        out.append({
            "match_id": _ident(raw["match_id"], _ID, "row.match_id"),
            "source": source,
            "weight": HUMAN_REVIEW_WEIGHT if source == "human_review" else REFEREE_WEIGHT,
            "game": _ident(raw["game"], _LABEL, "row.game"),
            "controller_family": _ident(raw["controller_family"], _LABEL, "row.controller_family"),
            "console": _ident(raw["console"], _LABEL, "row.console"),
            "candidate_correct": raw["candidate_correct"],
            "current_correct": raw["current_correct"],
        })
    return out


def _sum_side(rows: list[dict], side: str) -> tuple[int, int]:
    correct = 0
    total = 0
    for row in rows:
        total += row["weight"]
        if row[side]:
            correct += row["weight"]
    return correct, total


def _by_context(rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (row["game"], row["controller_family"], row["console"])
        groups.setdefault(key, []).append(row)
    return groups


def promotion_gate(*, candidate: dict, current: dict, rows: list[dict], idle: bool,
                   during_capture: bool, referee_identical: bool,
                   lock_age_s: int | None = None) -> dict:
    """Pure promotion decision. Strict overall gain, and no per-context regression.

    ``accuracy_permille`` is display only. The comparison is exact cross-multiplication.
    """
    if not isinstance(idle, bool) or not isinstance(during_capture, bool):
        raise SchemaError("idle")
    if not isinstance(referee_identical, bool):
        raise SchemaError("referee_identical")
    live = during_capture
    if lock_age_s is not None:
        if not _is_int(lock_age_s) or lock_age_s < 0 or lock_age_s > _SAFE_INT:
            raise SchemaError("lock_age_s")
        if lock_age_s < MARKER_FRESH_S:
            live = True
    clean = _rows(rows)
    cand = _version_obj(candidate, "candidate")
    cur = _version_obj(current, "current")
    cand_ok, cand_tot = _sum_side(clean, "candidate_correct")
    cur_ok, cur_tot = _sum_side(clean, "current_correct")
    groups = _by_context(clean)
    per_context = []
    regressed = False
    for key in sorted(groups):
        group = groups[key]
        g_cand = _sum_side(group, "candidate_correct")
        g_cur = _sum_side(group, "current_correct")
        worse = g_cand[0] * g_cur[1] < g_cur[0] * g_cand[1]
        regressed = regressed or worse
        per_context.append({
            "game": key[0],
            "controller_family": key[1],
            "console": key[2],
            "candidate": _metric(*g_cand),
            "current": _metric(*g_cur),
            "regression": worse,
        })
    if live:
        reason = "during_capture"
    elif not idle:
        reason = "not_idle"
    elif not referee_identical:
        reason = "referee_checks_differ"
    elif not clean:
        reason = "no_labelled_matches"
    elif regressed:
        reason = "context_regression"
    elif cand_ok * cur_tot <= cur_ok * cand_tot:
        reason = "not_strict_improvement"
    else:
        reason = "strict_improvement"
    decision = "promoted" if reason == "strict_improvement" else "discarded"
    record = {
        "schema": CONSOLIDATION_SCHEMA,
        "candidate": cand,
        "current": cur,
        "labelled_matches": [
            {
                "match_id": row["match_id"],
                "source": row["source"],
                "weight": row["weight"],
                "game": row["game"],
                "controller_family": row["controller_family"],
                "console": row["console"],
            }
            for row in clean
        ],
        "metrics": {"candidate": _metric(cand_ok, cand_tot), "current": _metric(cur_ok, cur_tot)},
        "per_context": per_context,
        "decision": decision,
        "reason": reason,
        "idle": idle,
        "during_capture": live,
        "referee_identical": referee_identical,
    }
    _validate_consolidation(record)
    return record


def _validate_consolidation(obj: dict) -> None:
    raw = _exact(obj, CONSOLIDATION_PROPERTIES, CONSOLIDATION_PROPERTIES, "consolidation")
    if raw["schema"] != CONSOLIDATION_SCHEMA or raw["decision"] not in DECISIONS:
        raise SchemaError("consolidation")
    if raw["reason"] not in GATE_REASONS:
        raise SchemaError("consolidation.reason")
    if (raw["decision"] == "promoted") != (raw["reason"] == "strict_improvement"):
        raise SchemaError("consolidation.decision")
    for bucket in (raw["metrics"]["candidate"], raw["metrics"]["current"]):
        _exact(bucket, METRIC_PROPERTIES, METRIC_PROPERTIES, "metrics")


def write_consolidation(path: Path | str, **kwargs: Any) -> dict:
    record = promotion_gate(**kwargs)
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(record) + "\n", encoding="utf-8")
    return record


def align_for_gate(candidate_outcomes: list[dict], current_outcomes: list[dict]) -> list[dict]:
    """Build gate rows from two joins of the same closed proposal set."""
    def index(rows: list[dict]) -> dict[str, dict]:
        if not isinstance(rows, list):
            raise SchemaError("outcomes")
        out = {}
        for row in rows:
            _validate_outcome(row)
            pid = row["proposal_id"]
            if pid in out:
                raise SchemaError("proposal_id")
            out[pid] = row
        return out

    cand = index(candidate_outcomes)
    cur = index(current_outcomes)
    if set(cand) != set(cur) or not cand:
        raise SchemaError("labelled series")
    rows = []
    for pid in sorted(cand):
        a, b = cand[pid], cur[pid]
        if a["match_id"] != b["match_id"] or a["source"] != b["source"]:
            raise SchemaError("labelled series")
        for key in ("game", "controller_family", "console"):
            if a["context"][key] != b["context"][key]:
                raise SchemaError("context")
        rows.append({
            "match_id": a["match_id"],
            "source": a["source"],
            "game": a["context"]["game"],
            "controller_family": a["context"]["controller_family"],
            "console": a["context"]["console"],
            "candidate_correct": a["trainable"] and a["outcome"] == "confirmed",
            "current_correct": b["trainable"] and b["outcome"] == "confirmed",
        })
    return rows
