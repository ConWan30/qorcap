"""Synthetic tests for the PWEC-2 Carbon reader and the local side rule.

No frames, no network, no ledger from a real run. Images are drawn in memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_carbon_pwec2 as carbon  # noqa: E402
import side_hsv  # noqa: E402

KEY = "unit-test-key"
BANNED = ("verified", "proven", "skill", "reaction time", "anti-cheat", "anticheat")
# Words the colour-cue prompt must use in its "do not read" sentence.
# "gamertags" is required here; it is not a BANNED bar word, because the
# prompt itself has to name it.
LANGUAGE_GATE = ("letters", "names", "gamertags")


def _bgr_hsv(h, s, v):
    pix = np.uint8([[[int(h), int(s), int(v)]]])
    got = cv2.cvtColor(pix, cv2.COLOR_HSV2BGR)[0, 0]
    return (int(got[0]), int(got[1]), int(got[2]))


def _blank():
    return np.zeros((side_hsv.FRAME_H, side_hsv.FRAME_W, 3), np.uint8)


def _paint(img, box, color):
    x0, y0, x1, y1 = box
    img[y0:y1, x0:x1] = color


def _plate(img, color, box=(600, 330, 680, 350)):
    _paint(img, box, color)


def _chevron(img):
    # Disc centre is full-frame (97, 92.5). An 8x8 block sits on it.
    _paint(img, (93, 88, 101, 96), _bgr_hsv(23, 198, 212))


def _red_dot(img):
    _paint(img, (30, 30, 35, 35), _bgr_hsv(4, 157, 142))


def _read_ok(n=7, side="enemy"):
    window = {
        "n": n,
        "target_present": "yes",
        "side": side,
        "tracked": "unsure",
        "shot_at": "unsure",
        "hitmarker_seen": "no",
        "kill_confirmed": "no",
        "escaped": "unsure",
    }
    return {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps({"windows": [window]})}]}
        }],
        "usageMetadata": {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 100,
            "thoughtsTokenCount": 50,
            "totalTokenCount": 1150,
        },
        "modelVersion": "gemini-4-carbon",
    }


class Fake:
    def __init__(self, responses, count_tokens=1000, count_status=200, count_body=None):
        self.responses = list(responses)
        self.count_tokens = count_tokens
        self.count_status = count_status
        self.count_body = count_body
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if url.endswith(":countTokens"):
            payload = self.count_body if self.count_body is not None else {"totalTokens": self.count_tokens}
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            return self.count_status, raw.encode("utf-8")
        if url.endswith(":generateContent"):
            if not self.responses:
                raise AssertionError("unexpected generateContent")
            status, payload = self.responses.pop(0)
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            return status, raw.encode("utf-8")
        raise AssertionError(url)

    def generated(self):
        return [item for item in self.calls if item[0].endswith(":generateContent")]


def _package(tmp_path, *, order=7, include_cal=False, sha=None, shape=(720, 1280, 3), rel="fullframes/windows/synthetic.jpg"):
    data = tmp_path / "data"
    path = data / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    img = np.zeros(shape, np.uint8)
    if shape == (720, 1280, 3):
        cv2.imwrite(str(path), img)
    else:
        cv2.imwrite(str(path), img)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    frames = [{
        "set": "window",
        "id": "synth1",
        "package_path": rel,
        "sha256": sha or digest,
        "window_id": "synthetic-window",
        "window_order": order,
        "role": "PRE",
        "t_us": 10,
    }]
    if include_cal:
        cal = data / "fullframes" / "calibration" / "cal_synth.jpg"
        cal.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(cal), img)
        frames.append({
            "set": "calibration",
            "id": "cal_synth",
            "package_path": "fullframes/calibration/cal_synth.jpg",
            "sha256": hashlib.sha256(cal.read_bytes()).hexdigest(),
            "window_id": "cal-only",
            "window_order": 0,
            "role": "PRE",
            "t_us": 0,
        })
    (data / "MANIFEST.json").write_text(json.dumps({"schema": "synthetic", "frames": frames}), encoding="utf-8")
    return data


def _run(tmp_path, fake, **kw):
    data = kw.pop("data", None) or _package(tmp_path, **{k: kw.pop(k) for k in list(kw) if k in ("order", "include_cal", "sha", "shape", "rel")})
    out = tmp_path / "out"
    code = carbon.run_reads(data, out, transport=fake, api_key=KEY, expect_sha=kw.get("expect_sha"))
    return code, out


def _ledger(out):
    path = out / "LEDGER.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _calls(out):
    return [row for row in _ledger(out) if row["event_code"] != "PWEC-COUNT"]


# --- local side rule -------------------------------------------------------

def test_check_sheet_fills_keep_their_colours():
    enemy_a = _blank()
    _plate(enemy_a, (29, 43, 114))  # rgb(114, 43, 29)
    enemy_b = _blank()
    _plate(enemy_b, (35, 44, 107))  # rgb(107, 44, 35)
    cyan = _blank()
    _plate(cyan, (134, 149, 74))  # rgb(74, 149, 134)
    blue = _blank()
    _plate(blue, (138, 106, 73))  # rgb(73, 106, 138)
    grey = _blank()
    _plate(grey, (40, 40, 40))
    assert side_hsv.classify_frame(enemy_a)["side"] == "enemy"
    assert side_hsv.classify_frame(enemy_b)["side"] == "enemy"
    assert side_hsv.classify_frame(cyan)["side"] == "teammate"
    assert side_hsv.classify_frame(blue)["side"] == "teammate"
    grey_cue = side_hsv.classify_frame(grey)
    assert grey_cue["side"] == "unsure"
    assert grey_cue["c_red"] is False and grey_cue["c_cyan"] is False
    assert grey_cue["n_grey_plates"] >= 1
    assert grey_cue["out_of_band"] is False


def test_flat_core_extremes_from_calibration_stay_inside():
    low_s = _blank()
    _plate(low_s, _bgr_hsv(8, 78, 167))
    high_h = _blank()
    _plate(high_h, _bgr_hsv(106, 137, 93))
    assert side_hsv.classify_frame(low_s)["c_red"] is True
    assert side_hsv.classify_frame(high_h)["c_cyan"] is True
    assert side_hsv.FROZEN["red_s_min"] == 74
    assert side_hsv.FROZEN["cyan_h"] == (75, 110)
    assert side_hsv.FROZEN["cyan_s_min"] == 60
    assert side_hsv.FROZEN["jpeg_margin"] == 4


def test_below_floor_red_is_not_a_side_and_not_out_of_band():
    img = _blank()
    _plate(img, _bgr_hsv(8, 73, 167))
    cue = side_hsv.classify_frame(img)
    assert cue["c_red"] is False
    assert cue["out_of_band"] is False
    assert cue["side"] == "unsure"


def test_out_of_band_hue_is_unsure():
    img = _blank()
    _plate(img, _bgr_hsv(50, 150, 150))
    cue = side_hsv.classify_frame(img)
    assert cue["out_of_band"] is True
    assert cue["side"] == "unsure"
    just_past = _blank()
    _plate(just_past, _bgr_hsv(111, 150, 150))
    assert side_hsv.classify_frame(just_past)["out_of_band"] is True


def test_gold_is_not_enemy():
    img = _blank()
    _plate(img, _bgr_hsv(19, 102, 199))
    cue = side_hsv.classify_frame(img)
    assert cue["c_red"] is False
    assert cue["side"] == "unsure"
    assert cue["n_gold_plates"] >= 1


def test_red_and_cyan_plates_conflict():
    img = _blank()
    _plate(img, _bgr_hsv(8, 160, 140), (600, 330, 680, 350))
    _plate(img, _bgr_hsv(90, 120, 160), (600, 180, 700, 210))
    cue = side_hsv.classify_frame(img)
    assert cue["c_red"] and cue["c_cyan"]
    assert cue["side"] == "unsure"


def test_plate_outside_the_centre_crop_is_not_a_cue():
    img = _blank()
    _plate(img, _bgr_hsv(4, 180, 150), (1100, 200, 1180, 230))
    cue = side_hsv.classify_frame(img)
    assert cue["c_red"] is False
    assert cue["side"] == "unsure"


def test_plate_inside_the_feed_mask_is_cleared():
    img = _blank()
    _plate(img, _bgr_hsv(4, 180, 150), (340, 400, 420, 430))
    assert side_hsv.classify_frame(img)["c_red"] is False


def test_map_dot_without_a_plate_does_not_call_a_side():
    img = _blank()
    _chevron(img)
    _red_dot(img)
    cue = side_hsv.classify_frame(img)
    assert cue["map_available"] is True
    assert cue["self_yellow"] is True
    assert cue["m_red"] is True
    assert cue["side"] == "unsure"
    window = side_hsv.classify_window([img])
    assert window["side"] == "unsure"
    assert window["map_calls_side"] is False
    assert window["reason"] == "no_plate"


def test_red_plate_stands_when_the_minimap_shows_cyan():
    img = _blank()
    _plate(img, _bgr_hsv(6, 160, 140))
    _chevron(img)
    _paint(img, (40, 50, 48, 58), _bgr_hsv(98, 140, 160))
    cue = side_hsv.classify_frame(img)
    assert cue["side"] == "enemy"
    assert cue["m_cyan"] is True
    window = side_hsv.classify_window([img])
    assert window["side"] == "enemy"
    assert window["map_agrees"] is False


def test_red_plate_with_a_red_dot_records_map_agreement():
    img = _blank()
    _plate(img, _bgr_hsv(6, 160, 140))
    _chevron(img)
    _red_dot(img)
    window = side_hsv.classify_window([img])
    assert window["side"] == "enemy"
    assert window["map_agrees"] is True


def test_no_chevron_makes_map_cues_unavailable():
    img = _blank()
    _red_dot(img)
    cue = side_hsv.classify_frame(img)
    assert cue["map_available"] is False
    assert cue["m_red"] is False


def test_window_aggregation_n_req_is_one_and_conflict_is_unsure():
    red = _blank()
    _plate(red, _bgr_hsv(6, 160, 140))
    cyan = _blank()
    _plate(cyan, _bgr_hsv(90, 120, 160))
    assert side_hsv.classify_window([red])["side"] == "enemy"
    assert side_hsv.classify_window([red, red])["side"] == "enemy"
    assert side_hsv.classify_window([cyan])["side"] == "teammate"
    both = side_hsv.classify_window([red, cyan])
    assert both["side"] == "unsure"
    assert both["reason"] == "conflict"
    assert side_hsv.classify_window([])["side"] == "unsure"


def test_out_of_band_plate_makes_the_window_unsure():
    red = _blank()
    _plate(red, _bgr_hsv(6, 160, 140))
    odd = _blank()
    _plate(odd, _bgr_hsv(50, 150, 150))
    assert side_hsv.classify_window([red, odd])["reason"] == "out_of_band"


def test_wrong_frame_shape_is_refused():
    with pytest.raises(ValueError):
        side_hsv.classify_frame(np.zeros((10, 10, 3), np.uint8))


def test_a_wash_of_red_is_not_a_plate():
    img = _blank()
    _paint(img, (400, 80, 900, 600), _bgr_hsv(4, 180, 150))
    assert side_hsv.classify_frame(img)["c_red"] is False


# --- carbon reader ---------------------------------------------------------

def test_prompts_name_the_frozen_bands_and_the_tiles():
    text = carbon.prompt_text().lower()
    for phrase in (
        "hue 0-15 or 165-179",
        "saturation at least 74",
        "value at least 90",
        "hue is 75-110",
        "saturation at least 60",
        "rgb 40, 40, 40",
        "[12, 10, 184, 176)",
        "[320, 40, 960, 680)",
        "[0, 250, 110, 500)",
        "yellow chevron",
        "jev is false",
    ):
        assert phrase in text
    for word in BANNED:
        assert word not in text
    for word in LANGUAGE_GATE:
        assert word in text


def test_reader_sources_do_not_use_the_bar_words():
    root = Path(__file__).resolve().parent
    blob = "\n".join(path.read_text(encoding="utf-8").lower() for path in (
        root / "side_hsv.py",
        root / "run_carbon_pwec2.py",
        root / "prompts" / "colour_cues.txt",
        root / "prompts" / "read_window.txt",
    ))
    for word in BANNED:
        assert word not in blob


def test_scrub_removes_key_material():
    secret = "unit-test-key"
    text = "see AIzaSyDUMMYDUMMYDUMMY1234 and key=supersecret plus " + secret
    cleaned = carbon.scrub(text, secret)
    assert "AIza" not in cleaned
    assert "key=" not in cleaned
    assert secret not in cleaned
    assert "[redacted]" in cleaned


def test_worst_case_matches_the_cap_rule_and_stops_at_exactly_ten():
    worst = carbon.worst_case_usd(1000)
    assert worst == (Decimal(1000 + 512) * Decimal(2) + Decimal(3024) * Decimal(10)) / Decimal(1_000_000)
    assert carbon.would_exceed(carbon.CAP_USD - worst, 1000) is False
    assert carbon.would_exceed(carbon.CAP_USD - worst + Decimal("0.000001"), 1000) is True
    billed, mark = carbon.billed_usd({
        "promptTokenCount": 1000,
        "candidatesTokenCount": 100,
        "thoughtsTokenCount": 50,
    })
    assert mark == "ESTIMATED"
    assert billed == Decimal("0.003500")
    missing, missing_mark = carbon.billed_usd({})
    assert missing is None and missing_mark == "UNMEASURED"


def test_chicago_clock_needs_no_tzdata():
    winter = datetime(2026, 11, 21, 7, 45, tzinfo=timezone.utc).astimezone(carbon._CST)
    assert winter.utcoffset() == timedelta(hours=-6)
    assert (winter.hour, winter.minute) == (1, 45)
    summer = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc).astimezone(carbon._CST)
    assert summer.utcoffset() == timedelta(hours=-5)
    assert summer.hour == 13
    spring = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc).astimezone(carbon._CST)
    assert (spring.hour, spring.minute) == (3, 0)
    assert spring.utcoffset() == timedelta(hours=-5)
    fall = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc).astimezone(carbon._CST)
    assert (fall.hour, fall.minute) == (1, 0)
    assert fall.utcoffset() == timedelta(hours=-6)
    assert winter.tzname() == "CST"
    assert summer.tzname() == "CDT"
    winter_stamp = carbon.now_cst(datetime(2026, 11, 21, 7, 45, tzinfo=timezone.utc))
    assert winter_stamp == "2026-11-21 01:45:00 CST"
    summer_stamp = carbon.now_cst(datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc))
    assert summer_stamp == "2026-07-15 13:00:00 CDT"
    stamp = carbon.now_cst()
    assert stamp.endswith(" CST") or stamp.endswith(" CDT")
    wall, zone = stamp.rsplit(" ", 1)
    datetime.strptime(wall, "%Y-%m-%d %H:%M:%S")
    assert zone in {"CST", "CDT"}


def test_generation_config_is_carbon_and_keeps_thinking_budget():
    cfg = carbon.generation_config()
    assert cfg["maxOutputTokens"] == 2000
    assert cfg["temperature"] == 1.0
    assert cfg["thinkingConfig"]["thinkingBudget"] == 1024
    assert carbon.MODEL == "gemini-4-carbon"
    assert "neon" not in carbon.endpoint("generateContent")


def test_panel_places_minimap_left_and_centre_right():
    img = _blank()
    img[10, 12] = (10, 20, 30)
    img[360, 640] = (7, 8, 9)
    panel = carbon.compose_panel(img)
    assert panel.shape == (640, 820, 3)
    assert tuple(int(v) for v in panel[0, 0]) == (10, 20, 30)
    assert tuple(int(v) for v in panel[320, 180 + 320]) == (7, 8, 9)
    assert int(panel[:, 172:180].sum()) == 0


def test_outputs_inside_the_repo_are_refused(tmp_path):
    fake = Fake([(200, _read_ok())])
    data = _package(tmp_path)
    out = carbon._HERE / "not-data"
    code = carbon.run_reads(data, out, transport=fake, api_key=KEY)
    assert code == 2
    assert fake.calls == []
    assert not out.exists()


def test_missing_key_makes_no_call(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    code = carbon.main(["--data", str(tmp_path), "--out", str(tmp_path / "out")])
    assert code == 2
    assert "AIza" not in capsys.readouterr().err


def test_sha_pin_mismatch_makes_no_call(tmp_path):
    fake = Fake([ (200, _read_ok()) ])
    code, _out = _run(tmp_path, fake, expect_sha=["colour_cues.txt=" + ("0" * 64)])
    assert code == 2
    assert fake.calls == []


def test_success_sends_the_key_in_the_header_and_ledgers_estimated_usage(tmp_path):
    fake = Fake([(200, _read_ok())])
    sha = carbon.file_sha256(carbon._HERE / "prompts" / "read_window.txt")
    code, out = _run(tmp_path, fake, expect_sha=[f"read_window.txt={sha}"], include_cal=True)
    assert code == 0
    generated = fake.generated()
    assert len(generated) == 1
    url, headers, body, _timeout = generated[0]
    assert url == carbon.endpoint("generateContent")
    assert "key=" not in url and "AIza" not in url and "?" not in url
    assert headers["x-goog-api-key"] == KEY
    assert "neon" not in url.lower() and b"neon" not in body.lower()
    payload = json.loads(body)
    assert payload["generationConfig"]["temperature"] == 1.0
    assert payload["generationConfig"]["maxOutputTokens"] == 2000
    assert payload["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1024
    images = [part for part in payload["contents"][0]["parts"] if "inlineData" in part]
    assert len(images) == 1
    counts = [item for item in fake.calls if item[0].endswith(":countTokens")]
    assert "generationConfig" not in json.loads(counts[0][2])
    call_rows = [row for row in _ledger(out) if row["event_code"] == "PWEC-CALL"]
    assert len(call_rows) == 1
    assert call_rows[0]["cost_mark"] == "ESTIMATED"
    assert call_rows[0]["cost_usd"] == "0.003500"
    assert call_rows[0]["input_tokens"] == 1000
    assert call_rows[0]["thinking_tokens"] == 50
    assert call_rows[0]["jev"] is False
    assert carbon.running_total(out / "LEDGER.jsonl") == Decimal("0.003500")
    reads = list((out / "reads").glob("*.json"))
    assert len(reads) == 1
    saved = json.loads(reads[0].read_text(encoding="utf-8"))
    assert saved["status"] == "PARSED"
    assert saved["read"]["windows"][0]["side"] == "enemy"


def test_huge_count_stops_before_generate(tmp_path):
    fake = Fake([(200, _read_ok())], count_tokens=5_000_000)
    code, out = _run(tmp_path, fake)
    assert code == 3
    assert fake.generated() == []
    caps = [row for row in _ledger(out) if row["event_code"] == "PWEC-CAP"]
    assert caps and caps[0]["disposition"] == "STOP"


def test_seeded_ledger_is_part_of_the_running_total(tmp_path):
    data = _package(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "LEDGER.jsonl").write_text(json.dumps({"event_code": "PWEC-CALL", "cost_usd": "9.980000"}) + "\n", encoding="utf-8")
    fake = Fake([(200, _read_ok())], count_tokens=1000)
    code = carbon.run_reads(data, out, transport=fake, api_key=KEY)
    assert code == 3
    assert fake.generated() == []


def test_count_429_freezes_with_no_generate(tmp_path):
    fake = Fake([(200, _read_ok())], count_status=429, count_body={"error": {"status": "RESOURCE_EXHAUSTED"}})
    code, _out = _run(tmp_path, fake)
    assert code == 4
    assert fake.generated() == []


def test_generate_429_freezes_and_does_not_retry(tmp_path):
    fake = Fake([(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})])
    code, out = _run(tmp_path, fake)
    assert code == 4
    assert len(fake.generated()) == 1
    assert _ledger(out)[-1]["event_code"] == "PWEC-FREEZE"


def test_generate_500_stops_with_no_retry_and_scrubs_the_body(tmp_path, capsys):
    fake = Fake([(500, {"error": "boom AIzaSyDUMMYDUMMYDUMMY1234 key=supersecret"})])
    code, out = _run(tmp_path, fake)
    assert code == 5
    assert len(fake.generated()) == 1
    blob = (out / "LEDGER.jsonl").read_text(encoding="utf-8")
    raw = "\n".join(path.read_text(encoding="utf-8") for path in (out / "raw").glob("*"))
    captured = capsys.readouterr()
    for text in (blob, raw, captured.out, captured.err):
        assert "AIza" not in text
        assert "key=" not in text
    assert _ledger(out)[-1]["event_code"] == "PWEC-STOP"


def test_thinking_config_400_stops_and_keeps_the_field(tmp_path):
    fake = Fake([(400, {"error": {"message": "thinkingConfig thinkingBudget is not accepted"}})])
    before = carbon.generation_config()
    code, out = _run(tmp_path, fake)
    assert code == 5
    assert len(fake.generated()) == 1
    sent = json.loads(fake.generated()[0][2])
    assert sent["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1024
    assert carbon.generation_config() == before
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "UNMEASURED"
    assert saved["reason"] == "thinkingConfig"


def test_max_tokens_retries_once_and_keeps_the_second_read(tmp_path):
    first = _read_ok(side="enemy")
    first["candidates"][0]["finishReason"] = "MAX_TOKENS"
    fake = Fake([(200, first), (200, _read_ok(side="teammate"))])
    code, out = _run(tmp_path, fake)
    assert code == 0
    assert len(fake.generated()) == 2
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "PARSED"
    assert saved["read"]["windows"][0]["side"] == "teammate"
    dispositions = [row["disposition"] for row in _ledger(out) if row["event_code"] == "PWEC-CALL"]
    assert dispositions == ["RETRY", "PARSED"]


def test_max_tokens_twice_is_unmeasured_with_no_third_call(tmp_path):
    first = _read_ok()
    first["candidates"][0]["finishReason"] = "MAX_TOKENS"
    second = _read_ok()
    second["candidates"][0]["finishReason"] = "MAX_TOKENS"
    fake = Fake([(200, first), (200, second)])
    code, out = _run(tmp_path, fake)
    assert code == 0
    assert len(fake.generated()) == 2
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "UNMEASURED"
    assert saved["read"] is None


def test_no_parse_retries_once_then_unmeasured(tmp_path):
    bad = _read_ok()
    bad["candidates"][0]["content"]["parts"][0]["text"] = "not json"
    fake = Fake([(200, bad), (200, bad)])
    code, out = _run(tmp_path, fake)
    assert code == 0
    assert len(fake.generated()) == 2
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "UNMEASURED"


def test_no_parse_then_a_clean_read_is_parsed(tmp_path):
    bad = _read_ok(n=1)
    fake = Fake([(200, bad), (200, _read_ok(n=7, side="teammate"))])
    code, out = _run(tmp_path, fake)
    assert code == 0
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "PARSED"
    assert saved["read"]["windows"][0]["side"] == "teammate"


def test_missing_usage_is_unmeasured_and_charged_at_the_worst_case(tmp_path):
    body = _read_ok()
    del body["usageMetadata"]
    fake = Fake([(200, body)])
    code, out = _run(tmp_path, fake)
    assert code == 0
    row = [item for item in _ledger(out) if item["event_code"] == "PWEC-CALL"][0]
    assert row["cost_mark"] == "UNMEASURED"
    assert Decimal(row["cost_usd"]) == carbon.worst_case_usd(1000)
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "PARSED"


def test_frame_sha_mismatch_sends_nothing(tmp_path):
    fake = Fake([(200, _read_ok())])
    code, _out = _run(tmp_path, fake, sha="0" * 64)
    assert code == 6
    assert fake.calls == []


def test_wrong_frame_size_sends_nothing(tmp_path):
    fake = Fake([(200, _read_ok())])
    code, _out = _run(tmp_path, fake, shape=(40, 40, 3))
    assert code == 6
    assert fake.calls == []


def test_parse_rejects_extra_keys_and_markdown_is_accepted():
    extra = (
        '{"windows":[{"n":7,"target_present":"yes","side":"enemy","tracked":"no",'
        '"shot_at":"no","hitmarker_seen":"no","kill_confirmed":"no","escaped":"no","extra":1}]}'
    )
    assert carbon.parse_read(extra, 7) is None
    inner = {
        "windows": [{
            "n": 7,
            "target_present": "no",
            "side": "none",
            "tracked": "unsure",
            "shot_at": "unsure",
            "hitmarker_seen": "no",
            "kill_confirmed": "no",
            "escaped": "unsure",
        }]
    }
    parsed = carbon.parse_read("```json\n" + json.dumps(inner) + "\n```", 7)
    assert parsed["windows"][0]["side"] == "none"



def test_killfeed_mask_residual_is_the_crop_feed_mask():
    assert side_hsv.CROP_FEED_MASK == (0, 250, 110, 500)
    assert side_hsv.crop_local(side_hsv.KILLFEED_MASK) == side_hsv.CROP_FEED_MASK


def test_compose_panel_zeros_feed_weapon_and_compass_residuals():
    img = _blank()
    red = _bgr_hsv(4, 180, 150)
    # Full-frame kill-feed residual inside the centre crop: [320, 290, 430, 540).
    _paint(img, (340, 400, 420, 430), red)
    # Weapon overlap [920, 600, 960, 680).
    _paint(img, (920, 600, 960, 680), red)
    # Compass leak [320, 40, 960, 44).
    _paint(img, (320, 40, 960, 44), red)
    keep = _bgr_hsv(6, 160, 140)
    _paint(img, (600, 330, 680, 350), keep)
    panel = carbon.compose_panel(img)
    origin = side_hsv.MINIMAP_TILE[2] - side_hsv.MINIMAP_TILE[0] + carbon.GUTTER
    feed = panel[250:500, origin:origin + 110]
    assert int(feed.sum()) == 0
    weapon = panel[560:640, origin + 600:origin + 640]
    assert int(weapon.sum()) == 0
    compass = panel[0:4, origin:origin + 640]
    assert int(compass.sum()) == 0
    # The kept plate is crop-local (280, 290).
    assert tuple(int(v) for v in panel[290, origin + 280]) == keep
    # compose_panel must not mutate the source frame.
    assert tuple(int(v) for v in img[400, 340]) == red


def test_plate_inside_the_weapon_overlap_is_cleared():
    img = _blank()
    _plate(img, _bgr_hsv(4, 180, 150), (920, 600, 960, 680))
    assert side_hsv.classify_frame(img)["c_red"] is False


def test_compass_leak_pixels_are_cleared_from_the_centre_crop():
    img = _blank()
    _paint(img, (320, 40, 960, 44), _bgr_hsv(4, 180, 150))
    cue = side_hsv.classify_frame(img)
    assert cue["c_red"] is False
    origin = side_hsv.MINIMAP_TILE[2] - side_hsv.MINIMAP_TILE[0] + carbon.GUTTER
    panel = carbon.compose_panel(img)
    assert int(panel[0:4, origin:origin + 640].sum()) == 0


def test_gold_overlap_at_disc_centre_stays_the_measured_chevron():
    """Self-yellow and XP gold share H 20-35; measured chevrons sit in both.

    The fixture is H 23, S 198, V 212, which also meets gold (S >= 70, V >= 140).
    Tightening yellow to exclude gold would reject that chevron, so the overlap
    is kept. A gold-only hue (H 16-19) at the disc centre is not the chevron.
    """
    img = _blank()
    _chevron(img)
    pix = img[90, 96]
    hsv = cv2.cvtColor(np.uint8([[pix]]), cv2.COLOR_BGR2HSV)[0, 0]
    h, s, v = int(hsv[0]), int(hsv[1]), int(hsv[2])
    assert side_hsv.YELLOW_H[0] <= h <= side_hsv.YELLOW_H[1]
    assert side_hsv.GOLD_H[0] <= h <= side_hsv.GOLD_H[1]
    assert s >= side_hsv.YELLOW_S_MIN and s >= side_hsv.GOLD_S_MIN
    assert v >= side_hsv.YELLOW_V_MIN and v >= side_hsv.GOLD_V_MIN
    cue = side_hsv.classify_frame(img)
    assert cue["self_yellow"] is True
    assert cue["map_available"] is True
    gold_only = _blank()
    _paint(gold_only, (93, 88, 101, 96), _bgr_hsv(16, 198, 212))
    assert side_hsv.classify_frame(gold_only)["self_yellow"] is False


def _window(present, side, **extra):
    row = {
        "n": 7,
        "target_present": present,
        "side": side,
        "tracked": "unsure",
        "shot_at": "unsure",
        "hitmarker_seen": "no",
        "kill_confirmed": "no",
        "escaped": "unsure",
    }
    row.update(extra)
    return json.dumps({"windows": [row]})


def test_parse_rejects_every_forbidden_side_present_combo():
    # prompt: side is none when target_present is no; otherwise not none.
    forbidden = [
        ("no", "enemy"),
        ("no", "teammate"),
        ("no", "unsure"),
        ("yes", "none"),
        ("unsure", "none"),
    ]
    for present, side in forbidden:
        assert carbon.parse_read(_window(present, side), 7) is None, (present, side)
    allowed = [
        ("no", "none"),
        ("yes", "enemy"),
        ("yes", "teammate"),
        ("yes", "unsure"),
        ("unsure", "enemy"),
        ("unsure", "teammate"),
        ("unsure", "unsure"),
    ]
    for present, side in allowed:
        parsed = carbon.parse_read(_window(present, side), 7)
        assert parsed is not None, (present, side)
        assert parsed["windows"][0]["side"] == side


def test_frame_path_escape_sends_nothing(tmp_path):
    fake = Fake([(200, _read_ok())])
    data = _package(tmp_path)
    outside = tmp_path / "outside.jpg"
    cv2.imwrite(str(outside), np.zeros((720, 1280, 3), np.uint8))
    manifest = json.loads((data / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["frames"][0]["package_path"] = "../outside.jpg"
    manifest["frames"][0]["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    (data / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    code = carbon.run_reads(data, tmp_path / "out", transport=fake, api_key=KEY)
    assert code == 6
    assert fake.calls == []


@pytest.mark.parametrize("reason", ["SAFETY", "RECITATION", "OTHER", None])
def test_non_stop_finish_is_unmeasured_with_no_retry(tmp_path, reason):
    body = _read_ok()
    if reason is None:
        del body["candidates"][0]["finishReason"]
    else:
        body["candidates"][0]["finishReason"] = reason
    fake = Fake([(200, body)])
    code, out = _run(tmp_path, fake)
    assert code == 0
    assert len(fake.generated()) == 1
    saved = json.loads(next((out / "reads").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "UNMEASURED"
    assert saved["read"] is None
    assert saved["reason"] == (reason if reason is not None else "missing finish")
    row = [item for item in _ledger(out) if item["event_code"] == "PWEC-CALL"][0]
    assert row["disposition"] == "UNMEASURED"
    assert row["finish_reason"] == reason


def test_running_total_raises_stoprun_on_truncated_or_invalid_ledger(tmp_path):
    path = tmp_path / "LEDGER.jsonl"
    path.write_text('{"event_code":"PWEC-CALL","cost_usd":"1.00"\n', encoding="utf-8")
    with pytest.raises(carbon.StopRun) as exc:
        carbon.running_total(path)
    assert exc.value.code == 5
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(carbon.StopRun):
        carbon.running_total(path)
    path.write_text('["not", "an", "object"]\n', encoding="utf-8")
    with pytest.raises(carbon.StopRun):
        carbon.running_total(path)
    assert carbon.running_total(tmp_path / "missing.jsonl") == Decimal("0")


def test_truncated_ledger_stops_with_no_call(tmp_path):
    data = _package(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "LEDGER.jsonl").write_text('{"event_code":"PWEC-CALL","cost_usd":"1.00"\n', encoding="utf-8")
    fake = Fake([(200, _read_ok())])
    code = carbon.run_reads(data, out, transport=fake, api_key=KEY)
    assert code == 5
    assert fake.calls == []



def _window_call_id(order=7, window_id="synthetic-window", attempt="a0"):
    digest = hashlib.sha256(window_id.encode("utf-8")).hexdigest()[:8]
    return f"w{order:04d}-{digest}-{attempt}", digest


def test_rerun_on_existing_out_makes_no_network_calls(tmp_path):
    first = Fake([(200, _read_ok())])
    code1, out = _run(tmp_path, first)
    assert code1 == 0
    assert first.calls
    second = Fake([(200, _read_ok())])
    code2 = carbon.run_reads(tmp_path / "data", out, transport=second, api_key=KEY)
    assert code2 == 5
    assert second.calls == []
    assert carbon.running_total(out / "LEDGER.jsonl") == Decimal("0.003500")


@pytest.mark.parametrize("kind", ["read", "raw", "ledger-CALL", "ledger-CAP", "ledger-FREEZE", "ledger-STOP"])
def test_existing_read_raw_or_ledger_makes_no_network_calls(tmp_path, kind):
    data = _package(tmp_path)
    out = tmp_path / "out"
    call, digest = _window_call_id()
    if kind == "read":
        path = out / "reads" / f"w0007-{digest}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}" + "\n", encoding="utf-8")
    elif kind == "raw":
        path = out / "raw" / f"{call}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}" + "\n", encoding="utf-8")
    else:
        out.mkdir()
        code = kind.split("-", 1)[1]
        rec = {"event_code": f"PWEC-{code}", "call": call, "cost_usd": "0.003500"}
        (out / "LEDGER.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    fake = Fake([(200, _read_ok())])
    status = carbon.run_reads(data, out, transport=fake, api_key=KEY)
    assert status == 5
    assert fake.calls == []


def test_parse_rejects_kill_confirmed_yes_with_escaped_yes():
    assert carbon.parse_read(_window("yes", "enemy", kill_confirmed="yes", escaped="yes"), 7) is None
    assert carbon.parse_read(_window("yes", "enemy", kill_confirmed="yes", escaped="no"), 7) is not None
    assert carbon.parse_read(_window("yes", "enemy", kill_confirmed="no", escaped="yes"), 7) is not None
    assert carbon.parse_read(_window("yes", "enemy", kill_confirmed="unsure", escaped="yes"), 7) is not None


@pytest.mark.parametrize("cost", ["-0.000001", -1, "NaN", "Infinity", "-Infinity"])
def test_running_total_rejects_non_finite_or_negative_cost(tmp_path, cost):
    path = tmp_path / "LEDGER.jsonl"
    path.write_text(json.dumps({"event_code": "PWEC-CALL", "cost_usd": cost}) + "\n", encoding="utf-8")
    with pytest.raises(carbon.StopRun) as exc:
        carbon.running_total(path)
    assert exc.value.code == 5


@pytest.mark.parametrize("frames", [{"not": "a list"}, ["not-an-object"]])
def test_malformed_manifest_frames_stop_with_no_call(tmp_path, frames):
    data = _package(tmp_path)
    (data / "MANIFEST.json").write_text(json.dumps({"schema": "synthetic", "frames": frames}), encoding="utf-8")
    fake = Fake([(200, _read_ok())])
    code = carbon.run_reads(data, tmp_path / "out", transport=fake, api_key=KEY)
    assert code == 6
    assert fake.calls == []


def test_response_text_guards_malformed_containers():
    assert carbon.response_text("not-an-object") == ("", None)
    assert carbon.response_text({"candidates": {"finishReason": "STOP"}}) == ("", None)
    text, finish = carbon.response_text({"candidates": [{"finishReason": "STOP", "content": ["bad"]}]})
    assert text == "" and finish == "STOP"
    text, finish = carbon.response_text({"candidates": [{"finishReason": "SAFETY", "content": {"parts": "bad"}}]})
    assert text == "" and finish == "SAFETY"
    text, finish = carbon.response_text({"candidates": [{"finishReason": "OTHER"}]})
    assert text == "" and finish == "OTHER"


def test_inconsistent_window_order_sends_nothing(tmp_path):
    data = _package(tmp_path)
    manifest = json.loads((data / "MANIFEST.json").read_text(encoding="utf-8"))
    extra = dict(manifest["frames"][0])
    extra["id"] = "synth2"
    extra["window_order"] = 8
    extra["t_us"] = 11
    manifest["frames"].append(extra)
    (data / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    fake = Fake([(200, _read_ok())])
    code = carbon.run_reads(data, tmp_path / "out", transport=fake, api_key=KEY)
    assert code == 6
    assert fake.calls == []


def test_frame_sha_is_compared_case_insensitively(tmp_path):
    data = _package(tmp_path)
    manifest = json.loads((data / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["frames"][0]["sha256"] = manifest["frames"][0]["sha256"].upper()
    (data / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    fake = Fake([(200, _read_ok())])
    code = carbon.run_reads(data, tmp_path / "out", transport=fake, api_key=KEY)
    assert code == 0
    assert fake.generated()
