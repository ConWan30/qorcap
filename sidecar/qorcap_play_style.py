"""Offline Practice Mode play-style sequence tagger & controller cross-check (T-0056, info_only).

Reads sealed Practice Mode session artifacts strictly read-only, filters/validates
sessions by ``frame_clock.jsonl`` and HID input report counts without silently shifting
later sessions on bad captures, assigns Con's ordered sequence tags
(``TAP``, ``HIPFIRE``, ``SPRAY_MIX``, ``OPTIC`` / ``FIRING_RANGE``), checks Firing Range
vs. multiplayer strictly from session bundle evidence (or tags ``UNKNOWN``), writes
no-float integer/string ``play_style.v0.json`` sidecars **outside** sealed match
folders, and performs a read-only controller cross-check (``AGREES`` / ``DISAGREES`` /
``UNMEASURED``) that never alters the assigned ``play_style``.

Boundaries:
* ``play_style`` is a session tag only — not a hit/no-hit label, and never assigns
  ``dataset_split`` (``train_pool`` vs ``fresh_holdout`` stays Con's own selection).
* Never writes into sealed match folders or touches ``receipt.v0`` / ``contracts/``.
* Rejects any float inside emitted JSON artifacts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

INFO_ONLY = True
COUNTED = False

SCHEMA = "qor.play_style.v0"
SUMMARY_SCHEMA = "qor.play_style_summary.v0"
DEFAULT_PRACTICE_DIR = Path(r"C:\Users\Contr\Documents\QorCap Practice")
DEFAULT_HAND_DIR = Path(r"D:\QorCap\practice-hand")
DEFAULT_OUT_ROOT = Path(r"D:\antigravity-work\t0056-out")
SIDECAR_NAME = "play_style.v0.json"

SESSION_DIR_RE = re.compile(r"^match-(\d{8})-(\d{6})$")

TRIGGER_THRESHOLD = 60
SINGLE_TAP_MAX_US = 160_000

DEFAULT_SEQUENCE = (
    ("TAP", 1),
    ("HIPFIRE", 1),
    ("SPRAY_MIX", 1),
    ("OPTIC_OR_FIRING_RANGE", 1),
)

VALID_SLOT_KEYS = ("TAP", "HIPFIRE", "SPRAY_MIX", "OPTIC_OR_FIRING_RANGE", "OPTIC", "FIRING_RANGE")
VALID_CROSS_CHECKS = ("AGREES", "DISAGREES", "UNMEASURED")


class PlayStyleError(Exception):
    """Base error for T-0056 play-style tagging."""


class PlayStyleContractError(PlayStyleError):
    """Raised on no-float or output isolation boundary violations."""


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _under(child: Path, parent: Path) -> bool:
    c = os.path.realpath(child)
    p = os.path.realpath(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def assert_no_floats(value, path: str = "$") -> None:
    if isinstance(value, float):
        raise PlayStyleContractError(f"float forbidden in play_style artifact at {path}: {value!r}")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > (1 << 53) - 1:
            raise PlayStyleContractError(f"integer exceeds 2^53-1 at {path}: {value!r}")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise PlayStyleContractError(f"object key must be str at {path}: {k!r}")
            assert_no_floats(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            assert_no_floats(item, f"{path}[{i}]")
        return
    raise PlayStyleContractError(f"unsupported value type at {path}: {type(value).__name__}")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: dict) -> None:
    assert_no_floats(payload)
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def parse_counts_spec(spec: str | None) -> list[tuple[str, int]]:
    """Parse ``TAP=2,HIPFIRE=1,...`` into ordered ``(slot_key, count)`` pairs."""
    if not spec or not spec.strip():
        return list(DEFAULT_SEQUENCE)
    slots: list[tuple[str, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"invalid counts entry {part!r}; expected STYLE=N")
        k, v = part.split("=", 1)
        key = k.strip().upper()
        if key not in VALID_SLOT_KEYS:
            raise ValueError(f"unknown style slot {key!r}; allowed: {VALID_SLOT_KEYS}")
        cnt = int(v.strip())
        if cnt < 0:
            raise ValueError(f"count must be >= 0 for {key!r}")
        if cnt > 0:
            slots.append((key, cnt))
    return slots


def _expand_slots(sequence_counts: list[tuple[str, int]]) -> list[str]:
    out: list[str] = []
    for key, cnt in sequence_counts:
        out.extend([key] * int(cnt))
    return out


# ----------------------------------------------------------------- Session discovery & telemetry
def discover_practice_sessions(
    practice_dir: Path,
    *,
    hand_dir: Path | None = None,
    date_filter: str | None = None,
) -> list[dict]:
    """List ``match-YYYYMMDD-HHMMSS`` folders sorted strictly by start time."""
    practice_dir = Path(practice_dir)
    if not practice_dir.is_dir():
        return []
    found = []
    for entry in practice_dir.iterdir():
        if not entry.is_dir():
            continue
        m = SESSION_DIR_RE.match(entry.name)
        if not m:
            continue
        d_str, t_str = m.group(1), m.group(2)
        if date_filter and d_str != date_filter:
            continue
        iso_start = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]}T{t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"
        paired_hand = Path(hand_dir) / entry.name if hand_dir is not None else None
        found.append({
            "session_id": entry.name,
            "date": d_str,
            "time": t_str,
            "start_time_local": iso_start,
            "match_dir": entry,
            "hand_dir": paired_hand if (paired_hand is not None and paired_hand.is_dir()) else None,
        })
    found.sort(key=lambda item: (item["date"], item["time"], item["session_id"]))
    return found


def _count_frame_clock_frames(match_dir: Path) -> int:
    fc = match_dir / "frame_clock.jsonl"
    if not fc.is_file():
        return 0
    count = 0
    with open(fc, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("type") == "frame" and "frame" in r:
                count += 1
    return count


def _resolve_inputs_file(match_dir: Path, hand_dir: Path | None) -> Path | None:
    ip = match_dir / "inputs.jsonl"
    if ip.is_file() and ip.stat().st_size > 0:
        return ip
    if hand_dir is not None:
        hp = hand_dir / "hand_timeline.v0.jsonl"
        if hp.is_file() and hp.stat().st_size > 0:
            return hp
    hp_local = match_dir / "hand_timeline.v0.jsonl"
    if hp_local.is_file() and hp_local.stat().st_size > 0:
        return hp_local
    return None


def _hold_spans(rows: list[tuple[int, int]], last_t_us: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    down = None
    for t_us, val in rows:
        held = val > TRIGGER_THRESHOLD
        if held and down is None:
            down = t_us
        elif not held and down is not None:
            spans.append((down, t_us))
            down = None
    if down is not None:
        spans.append((down, max(last_t_us, down)))
    return spans


def extract_controller_metrics(match_dir: Path, hand_dir: Path | None = None) -> dict:
    """Compute integer-only HID report count & trigger metrics (read-only)."""
    ip = _resolve_inputs_file(match_dir, hand_dir)
    if ip is None:
        return {
            "hid_in_rows": 0,
            "r2_span_count": 0,
            "r2_total_ms": 0,
            "r2_median_ms": 0,
            "single_tap_share_bps": 0,
            "l2_overlap_share_bps": 0,
        }

    r2_rows: list[tuple[int, int]] = []
    l2_rows: list[tuple[int, int]] = []
    hid_in_rows = 0
    last_t_us = 0
    with open(ip, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = r.get("t_us")
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                continue
            t_us = int(t)
            if t_us > last_t_us:
                last_t_us = t_us
            if r.get("type") not in ("in", "press", "buttons"):
                continue
            hid_in_rows += 1
            r2 = r.get("r2")
            l2 = r.get("l2")
            if r2 is not None and not isinstance(r2, bool):
                r2_rows.append((t_us, int(r2)))
            if l2 is not None and not isinstance(l2, bool):
                l2_rows.append((t_us, int(l2)))

    r2_spans = _hold_spans(r2_rows, last_t_us)
    l2_spans = _hold_spans(l2_rows, last_t_us)
    if not r2_spans:
        return {
            "hid_in_rows": hid_in_rows,
            "r2_span_count": 0,
            "r2_total_ms": 0,
            "r2_median_ms": 0,
            "single_tap_share_bps": 0,
            "l2_overlap_share_bps": 0,
        }

    durations_us = sorted(max(0, b - a) for a, b in r2_spans)
    r2_total_us = sum(durations_us)
    tap_count = sum(1 for d in durations_us if d <= SINGLE_TAP_MAX_US)
    overlap_us = 0
    for a1, b1 in r2_spans:
        for a2, b2 in l2_spans:
            if b2 > a1 and b1 > a2:
                overlap_us += min(b1, b2) - max(a1, a2)

    mid = len(durations_us) // 2
    median_us = durations_us[mid]
    tap_bps = int(round((tap_count * 10000) / len(durations_us)))
    l2_bps = int(round((overlap_us * 10000) / max(1, r2_total_us)))
    return {
        "hid_in_rows": hid_in_rows,
        "r2_span_count": len(r2_spans),
        "r2_total_ms": r2_total_us // 1000,
        "r2_median_ms": median_us // 1000,
        "single_tap_share_bps": max(0, min(10000, tap_bps)),
        "l2_overlap_share_bps": max(0, min(10000, l2_bps)),
    }


def _read_json_safe(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def detect_environment_from_bundle(match_dir: Path) -> tuple[str, str]:
    """Infer ``FIRING_RANGE`` vs ``OPTIC`` (multiplayer) strictly from bundle data, or ``UNKNOWN``."""
    # 1. Explicit metadata keys if present
    for name in ("cod_session.json", "game_set.json", "receipt.json", "bundle.json", "session_meta.json"):
        data = _read_json_safe(match_dir / name)
        if not data:
            continue
        blob = " ".join(
            str(data.get(k) or "")
            for k in ("mode", "map", "playlist", "environment", "venue", "match_type")
        ).lower()
        if "firing_range" in blob or "firing range" in blob:
            return "FIRING_RANGE", f"{name}:explicit_firing_range"
        if any(tok in blob for tok in ("multiplayer", "team_deathmatch", "domination", "hardpoint", "core_6v6", "search_and_destroy", "kill_confirmed")):
            return "OPTIC", f"{name}:explicit_multiplayer"

    # 2. Multiplayer telemetry inside bundle (live scoreboard rows, end screen, killfeed)
    stats = _read_json_safe(match_dir / "cod_reader_stats.json")
    if stats and (stats.get("owner_row_ever_read") or stats.get("owner_row_accepted")):
        return "OPTIC", "cod_reader_stats:owner_row_read"

    stl = _read_json_safe(match_dir / "score_timeline.json")
    if stl:
        events = ((stl.get("signals") or {}).get("score_timeline") or {}).get("events") or []
        if events:
            return "OPTIC", "score_timeline:events_present"

    end_stop = _read_json_safe(match_dir / "cod_end_stop.json")
    if end_stop and (end_stop.get("confirmed") is not None or int(end_stop.get("end_word_frames") or 0) > 0):
        return "OPTIC", "cod_end_stop:end_screen_seen"

    sig_path = match_dir / "cod_signals.jsonl"
    if sig_path.is_file():
        with open(sig_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 2000:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for reg in rec.get("regions") or []:
                    if not isinstance(reg, dict):
                        continue
                    rname = reg.get("region")
                    rstat = reg.get("status")
                    if rname in ("scoreboard", "killfeed") and rstat == "read":
                        return "OPTIC", f"cod_signals:{rname}_read"

    return "UNKNOWN", "no_unambiguous_environment_evidence"


def cross_check_play_style(play_style: str, metrics: dict) -> str:
    """Advisory controller cross-check (``AGREES``/``DISAGREES``/``UNMEASURED``). Never edits tag."""
    r2_count = int(metrics.get("r2_span_count", 0))
    if r2_count == 0 or play_style in ("UNKNOWN", "SKIPPED", "EXTRA_UNASSIGNED"):
        return "UNMEASURED"

    tap_bps = int(metrics.get("single_tap_share_bps", 0))
    l2_bps = int(metrics.get("l2_overlap_share_bps", 0))
    med_ms = int(metrics.get("r2_median_ms", 0))

    if play_style == "TAP":
        return "AGREES" if (tap_bps >= 5000 or med_ms <= 200) else "DISAGREES"
    if play_style == "HIPFIRE":
        return "AGREES" if l2_bps <= 3000 else "DISAGREES"
    if play_style == "SPRAY_MIX":
        return "AGREES" if (tap_bps <= 7500 and med_ms >= 150) else "DISAGREES"
    if play_style in ("OPTIC", "FIRING_RANGE"):
        if r2_count < 2:
            return "UNMEASURED"
        return "AGREES" if l2_bps >= 4000 else "DISAGREES"
    return "UNMEASURED"


# ----------------------------------------------------------------- Main auto-tagger & reader API
def read_play_style_tag(out_root: Path, session_id: str) -> dict | None:
    """Helper for downstream ingest (e.g. T-0055) to read a session's ``play_style.v0.json``."""
    p = Path(out_root) / session_id / SIDECAR_NAME
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def tag_practice_sessions(
    practice_dir: Path,
    out_root: Path,
    *,
    hand_dir: Path | None = None,
    date_filter: str | None = None,
    sequence_counts: list[tuple[str, int]] | None = None,
) -> dict:
    """Validate, sequence-tag, cross-check, and write ``play_style.v0.json`` outside match dirs."""
    practice_dir = Path(practice_dir)
    out_root = Path(out_root)
    if _under(out_root, practice_dir):
        raise PlayStyleContractError(f"refusing to write play_style outputs inside practice dir: {out_root}")

    sessions = discover_practice_sessions(
        practice_dir, hand_dir=hand_dir, date_filter=date_filter
    )
    slots = _expand_slots(sequence_counts if sequence_counts is not None else list(DEFAULT_SEQUENCE))

    records: list[dict] = []
    valid_cursor = 0
    skipped_count = 0
    saw_skipped = False

    for s in sessions:
        match_dir: Path = s["match_dir"]
        s_hand_dir: Path | None = s["hand_dir"]
        session_out = out_root / s["session_id"]
        if _under(session_out, match_dir) or _under(match_dir, session_out):
            raise PlayStyleContractError(f"refusing to write inside match bundle: {session_out}")

        fc_frames = _count_frame_clock_frames(match_dir)
        metrics = extract_controller_metrics(match_dir, s_hand_dir)
        hid_rows = metrics["hid_in_rows"]

        flags: list[str] = []
        if fc_frames == 0:
            flags.append("missing_frame_clock")
        if hid_rows == 0:
            flags.append("no_hid_reports")

        if flags:
            skipped_count += 1
            saw_skipped = True
            flags.append("sequence_mismatch")
            rec = {
                "schema": SCHEMA,
                "info_only": True,
                "counted": False,
                "session_id": s["session_id"],
                "start_time_local": s["start_time_local"],
                "status": "SKIPPED_INVALID",
                "sequence_index": -1,
                "slot_label": "SKIPPED",
                "play_style": "SKIPPED",
                "environment_evidence": "skipped_invalid_session",
                "controller_check": "UNMEASURED",
                "controller_metrics": metrics,
                "frame_clock_frames": fc_frames,
                "flags": flags,
                "note": "Session skipped due to missing frame_clock or 0 HID reports; not shifted silently. Session tag only; never assigns dataset_split.",
            }
            _atomic_write_json(session_out / SIDECAR_NAME, rec)
            records.append(rec)
            continue

        if saw_skipped:
            flags.append("after_skipped_session")

        if valid_cursor < len(slots):
            slot_label = slots[valid_cursor]
            if slot_label == "OPTIC_OR_FIRING_RANGE":
                play_style, env_ev = detect_environment_from_bundle(match_dir)
                if play_style == "UNKNOWN":
                    flags.append("environment_unclear")
            else:
                play_style = slot_label
                env_ev = "sequence_slot"
        else:
            slot_label = "EXTRA_UNASSIGNED"
            play_style = "EXTRA_UNASSIGNED"
            env_ev = "sequence_overflow"
            flags.append("sequence_overflow")

        check = cross_check_play_style(play_style, metrics)
        if check == "DISAGREES":
            flags.append("controller_check_disagrees")

        rec = {
            "schema": SCHEMA,
            "info_only": True,
            "counted": False,
            "session_id": s["session_id"],
            "start_time_local": s["start_time_local"],
            "status": "TAGGED" if play_style not in ("UNKNOWN", "EXTRA_UNASSIGNED") else "FLAGGED",
            "sequence_index": valid_cursor,
            "slot_label": slot_label,
            "play_style": play_style,
            "environment_evidence": env_ev,
            "controller_check": check,
            "controller_metrics": metrics,
            "frame_clock_frames": fc_frames,
            "flags": flags,
            "note": "Session play_style tag only; not a hit/no-hit label and never assigns dataset_split.",
        }
        _atomic_write_json(session_out / SIDECAR_NAME, rec)
        records.append(rec)
        valid_cursor += 1

    mismatch_reasons: list[str] = []
    if skipped_count > 0:
        mismatch_reasons.append(f"skipped_invalid_sessions={skipped_count}")
    if valid_cursor < len(slots):
        mismatch_reasons.append(f"sequence_shortfall={valid_cursor}_of_{len(slots)}")
    if valid_cursor > len(slots):
        mismatch_reasons.append(f"sequence_overflow={valid_cursor}_of_{len(slots)}")

    summary = {
        "schema": SUMMARY_SCHEMA,
        "info_only": True,
        "counted": False,
        "date_filter": date_filter or "ALL",
        "expected_slots": slots,
        "total_discovered": len(sessions),
        "valid_tagged": valid_cursor,
        "skipped_invalid": skipped_count,
        "sequence_mismatch": bool(mismatch_reasons),
        "mismatch_reasons": mismatch_reasons,
        "created_at_utc": _utc_now_iso(),
        "sessions": records,
    }
    _atomic_write_json(out_root / "play_style_summary.v0.json", summary)

    md_lines = [
        f"# Practice Play-Style Tags ({date_filter or 'ALL'})",
        "",
        f"- **Discovered:** `{len(sessions)}` | **Valid Tagged:** `{valid_cursor}` | **Skipped Invalid:** `{skipped_count}` | **Sequence Mismatch:** `{bool(mismatch_reasons)}` ({', '.join(mismatch_reasons) if mismatch_reasons else 'none'})",
        "",
        "| session_id | start_time_local | slot_label | play_style | controller_check | flags |",
        "|---|---|---|---|---|---|",
    ]
    for r in records:
        fl = ", ".join(r["flags"]) if r["flags"] else "-"
        md_lines.append(
            f"| `{r['session_id']}` | `{r['start_time_local']}` | `{r['slot_label']}` | `{r['play_style']}` | `{r['controller_check']}` | {fl} |"
        )
    if not records:
        md_lines.append("| (none) | - | - | - | - | no sessions matched |")
    _atomic_write_text(out_root / "play_style_summary.md", "\n".join(md_lines) + "\n")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Auto-tag Practice Mode CoD sessions by play-style sequence (info_only)")
    ap.add_argument("--practice-dir", default=str(DEFAULT_PRACTICE_DIR))
    ap.add_argument("--hand-dir", default=str(DEFAULT_HAND_DIR))
    ap.add_argument("--out", default=str(DEFAULT_OUT_ROOT))
    ap.add_argument("--date", help="Optional YYYYMMDD filter (e.g. 20261125)")
    ap.add_argument(
        "--counts",
        help="Optional comma-separated STYLE=COUNT list (default: TAP=1,HIPFIRE=1,SPRAY_MIX=1,OPTIC_OR_FIRING_RANGE=1)",
    )
    args = ap.parse_args(argv)
    seq = parse_counts_spec(args.counts)
    res = tag_practice_sessions(
        Path(args.practice_dir),
        Path(args.out),
        hand_dir=Path(args.hand_dir) if args.hand_dir else None,
        date_filter=args.date,
        sequence_counts=seq,
    )
    print(json.dumps(res, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
