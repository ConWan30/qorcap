"""Challenge alerts are loud, optional, and invisible to the recording.

The three-beep, toast, and flash run after the challenge is posted. They do not
move the window, the response log, the receipt, or the checkpoints. Off Windows
the real backends do nothing and do not raise.
"""
import io
import json
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_challenge_alert as alert  # noqa: E402
import qorcap_submit as qs  # noqa: E402

WALL = 1_700_000_000_000
TOKEN = {
    "nonce": "ab" * 16,
    "pattern": ["OPTIONS", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE"],
    "issued_ms": 1_000,
    "deadline_ms": 91_000,
    "phase": "kickoff",
}
SEEDS = {
    "checkpoints.jsonl": b'{"seq":0,"trigger":"start"}\n',
    "receipt.json": b'{"challenges":[]}\n',
    "inputs.jsonl": b'{"t_ms":1000,"button":"OPTIONS","down":true}\n',
    "challenges.jsonl": b"",
}
_ON = {
    "QORCAP_CHALLENGE_BEEP": "1",
    "QORCAP_CHALLENGE_TOAST": "1",
    "QORCAP_CHALLENGE_FLASH": "1",
    "QORCAP_CHALLENGE_BEEP_HZ": "2400",
    "QORCAP_CHALLENGE_BEEP_VOLUME": "1",
}
_OFF_ALL = {
    "QORCAP_CHALLENGE_BEEP": "0",
    "QORCAP_CHALLENGE_TOAST": "0",
    "QORCAP_CHALLENGE_FLASH": "0",
}


def _pcm(data: bytes) -> list[int]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        assert wf.getnchannels() == 1 and wf.getsampwidth() == 2 and wf.getframerate() == 44100
    count = len(raw) // 2
    import array
    samples = array.array("h")
    samples.frombytes(raw[: count * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return list(samples)


def _bursts(samples: list[int], gap: int = 100) -> int:
    """Count tones. A sine crosses zero, so only a long silence starts a new tone."""
    runs = 0
    silent = gap
    for sample in samples:
        if sample:
            if silent >= gap:
                runs += 1
            silent = 0
        else:
            silent += 1
    return runs


class _Api:
    def __init__(self):
        self.posted = []

    def get(self, path, timeout=3):
        return {"chain_head": "c" * 64, "segments": 7, "checkpoint_head": "d" * 64}

    def post(self, path, body=None, timeout=5):
        self.posted.append((path, body))
        return {"ok": True}


class _Client:
    def post_json(self, path, body):
        if path == "/v1/anchors":
            return {"type": "anchor"}
        return dict(TOKEN)


class _Gate:
    def __init__(self):
        self.events = []

    def latest(self, after_ms=0):
        event = self.events[-1] if self.events else None
        return event if event is not None and event.t_ms > after_ms else None


class _Ev:
    t_ms = 10
    phase = "kickoff"
    quarter = 1

    def to_dict(self):
        return {"basis": "play_call", "phase": self.phase, "t_ms": self.t_ms, "quarter": self.quarter}


def _expected_files() -> dict[str, bytes]:
    gate = {
        "phase": "kickoff",
        "nonce_id": TOKEN["nonce"][:8],
        "issued_ms": TOKEN["issued_ms"],
        "deadline_ms": TOKEN["deadline_ms"],
        "prompt_wall_ms": WALL,
        "gate": {"basis": "play_call", "phase": "kickoff", "t_ms": 10, "quarter": 1},
    }
    files = dict(SEEDS)
    files["anchors.jsonl"] = json.dumps({"type": "anchor"}).encode() + b"\n"
    files["official_challenges.jsonl"] = json.dumps(TOKEN).encode() + b"\n"
    files["challenge_gates.jsonl"] = json.dumps(gate).encode() + b"\n"
    return files


def _snapshot(bundle: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(bundle.iterdir()) if p.is_file()}


def _expected_post() -> dict:
    return {
        "nonce": TOKEN["nonce"],
        "phase": "kickoff",
        "role": "home",
        "id": TOKEN["nonce"][:8],
        "issued_ms": TOKEN["issued_ms"],
        "deadline_ms": TOKEN["deadline_ms"],
    }


def _backends(calls, *, beep=None, toast=None, flash=None):
    def beep_fn(prompt, cfg):
        calls.append(("beep", prompt, cfg))
        if beep:
            beep(prompt, cfg)

    def toast_fn(prompt, cfg):
        calls.append(("toast", prompt, cfg))
        if toast:
            toast(prompt, cfg)

    def flash_fn(prompt, cfg):
        calls.append(("flash", prompt, cfg))
        if flash:
            flash(prompt, cfg)

    return alert.AlertBackends(beep=beep_fn, toast=toast_fn, flash=flash_fn)


def _fire(bundle: Path, *, legacy: list):
    for name, data in SEEDS.items():
        (bundle / name).write_bytes(data)
    api = _Api()
    gate = _Gate()
    gate.events.append(_Ev())
    marks = {}

    def wall():
        marks.setdefault("wall_first", time.perf_counter())
        marks["wall_last"] = time.perf_counter()
        return WALL

    def post(path, body=None, timeout=5):
        if path == "/challenge":
            marks["post"] = time.perf_counter()
        api.posted.append((path, body))
        return {"ok": True}

    api.post = post
    live = qs.OfficialLive(
        "http://referee.test", bundle, "m1", "home", api, challenges=True, client=_Client(),
        clock=lambda: 0.0, wall=wall, gate=lambda: gate, beep=lambda: legacy.append("beep"),
    )
    started = time.perf_counter()
    live.step()
    marks["done"] = time.perf_counter()
    marks["started"] = started
    return live, api, marks


def _wait(calls, n, timeout=1.0):
    deadline = time.monotonic() + timeout
    while len(calls) < n and time.monotonic() < deadline:
        time.sleep(0.01)


def _names(calls):
    return [name for name, _prompt, _cfg in calls]


def test_three_beep_wav_is_full_scale_and_three_pulses():
    cfg = alert.config_from_env({})
    assert cfg.beep and cfg.toast and cfg.flash
    assert cfg.beep_hz == alert.DEFAULT_HZ == 2400
    assert cfg.beep_volume == 1.0
    assert alert.config_from_env({"QORCAP_CHALLENGE_BEEP_HZ": "880"}).beep_hz == 880
    assert alert.config_from_env({"QORCAP_CHALLENGE_BEEP_HZ": "nope"}).beep_hz == 2400
    assert alert.config_from_env({"QORCAP_CHALLENGE_BEEP_HZ": "50"}).beep_hz == 2400
    assert alert.config_from_env({"QORCAP_CHALLENGE_BEEP_VOLUME": "0.5"}).beep_volume == 0.5
    assert alert.config_from_env({"QORCAP_CHALLENGE_BEEP_VOLUME": "50"}).beep_volume == 0.5
    loud = _pcm(alert.render_three_beep_wav(2400, 1.0))
    half = _pcm(alert.render_three_beep_wav(2400, 0.5))
    assert max(abs(sample) for sample in loud) >= 32000
    assert 16000 <= max(abs(sample) for sample in half) <= 17000
    assert _bursts(loud) == 3
    played = {}

    def capture(data, hz):
        played["data"] = data
        played["hz"] = hz

    original = alert._play_wav_async
    alert._play_wav_async = capture
    try:
        alert.play_three_beep("QORTIME CHALLENGE", alert.config_from_env({
            "QORCAP_CHALLENGE_BEEP_HZ": "1760",
            "QORCAP_CHALLENGE_BEEP_VOLUME": "1",
        }))
    finally:
        alert._play_wav_async = original
    assert played["hz"] == 1760
    assert max(abs(sample) for sample in _pcm(played["data"])) >= 32000
    assert _bursts(_pcm(played["data"])) == 3


def test_platform_backends_do_not_raise():
    cfg = alert.config_from_env({})
    t0 = time.perf_counter()
    alert.play_three_beep("QORTIME CHALLENGE", cfg)
    alert.show_toast("QORTIME CHALLENGE press OPTIONS", cfg)
    alert.flash_console("QORTIME CHALLENGE", cfg)
    alert.notify_challenge("QORTIME CHALLENGE", env=_OFF_ALL)
    assert time.perf_counter() - t0 < 5.0


def test_challenge_fires_beep_toast_and_flash_once(tmp_path, monkeypatch):
    for key, value in _ON.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr(alert, "default_backends", lambda: _backends(calls))
    legacy = []
    live, api, _marks = _fire(tmp_path, legacy=legacy)
    _wait(calls, 3)
    assert _names(calls) == ["beep", "toast", "flash"]
    prompt = calls[1][1]
    assert "QORTIME CHALLENGE" in prompt and "CROSS" in prompt and "90 seconds" in prompt
    assert calls[0][2].beep_hz == 2400 and calls[0][2].beep_volume == 1.0
    assert live.issued == {"kickoff"}
    assert live.open_until_ms == TOKEN["deadline_ms"]
    assert legacy == ["beep"]
    assert [path for path, _body in api.posted] == ["/challenge"]


@pytest.mark.parametrize("flag,skipped", [
    ("QORCAP_CHALLENGE_BEEP", "beep"),
    ("QORCAP_CHALLENGE_TOAST", "toast"),
    ("QORCAP_CHALLENGE_FLASH", "flash"),
])
def test_env_flag_disables_each_alert(tmp_path, monkeypatch, flag, skipped):
    for key, value in _ON.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(flag, "0")
    calls = []
    monkeypatch.setattr(alert, "default_backends", lambda: _backends(calls))
    _fire(tmp_path, legacy=[])
    _wait(calls, 2)
    assert skipped not in _names(calls)
    assert _names(calls) == [name for name in ("beep", "toast", "flash") if name != skipped]


def test_disabling_alerts_matches_today(tmp_path, monkeypatch):
    for key, value in _OFF_ALL.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr(alert, "default_backends", lambda: _backends(calls))
    legacy = []
    _live, api, _marks = _fire(tmp_path, legacy=legacy)
    time.sleep(0.2)
    assert calls == []
    assert legacy == ["beep"]
    assert _snapshot(tmp_path) == _expected_files()
    assert api.posted == [("/challenge", _expected_post())]


def test_slow_alert_does_not_block_or_shift_the_window(tmp_path, monkeypatch):
    for key, value in _ON.items():
        monkeypatch.setenv(key, value)
    started = threading.Event()
    finished = threading.Event()

    def slow(prompt, cfg):
        del prompt, cfg
        started.set()
        time.sleep(2)
        finished.set()

    calls = []
    monkeypatch.setattr(alert, "default_backends", lambda: _backends(calls, beep=slow))
    off = tmp_path / "off"
    off.mkdir()
    on = tmp_path / "on"
    on.mkdir()
    monkeypatch.setenv("QORCAP_CHALLENGE_BEEP", "0")
    monkeypatch.setenv("QORCAP_CHALLENGE_TOAST", "0")
    monkeypatch.setenv("QORCAP_CHALLENGE_FLASH", "0")
    _fire(off, legacy=[])
    for key, value in _ON.items():
        monkeypatch.setenv(key, value)
    _live, api, marks = _fire(on, legacy=[])
    elapsed = marks["done"] - marks["started"]
    assert elapsed < 0.5
    assert marks["wall_last"] - marks["started"] < 0.5
    assert marks["post"] - marks["started"] < 0.5
    assert started.wait(1.0)
    assert not finished.is_set()
    assert _snapshot(on) == _snapshot(off) == _expected_files()
    assert api.posted == [("/challenge", _expected_post())]
    assert api.posted[0][1]["issued_ms"] == 1_000
    assert api.posted[0][1]["deadline_ms"] == 91_000
    assert b'"prompt_wall_ms": ' + str(WALL).encode() in (on / "challenge_gates.jsonl").read_bytes()


def test_alert_exception_does_not_change_the_recording(tmp_path, monkeypatch, capsys):
    for key, value in _ON.items():
        monkeypatch.setenv(key, value)
    errors = []

    def hook(args):
        errors.append(args.exc_type)

    monkeypatch.setattr(threading, "excepthook", hook)

    def boom(prompt, cfg):
        del prompt, cfg
        raise RuntimeError("alert down")

    calls = []
    monkeypatch.setattr(alert, "default_backends", lambda: _backends(calls, beep=boom, toast=boom, flash=boom))
    live, api, _marks = _fire(tmp_path, legacy=[])
    _wait(calls, 3)
    time.sleep(0.05)
    err = capsys.readouterr()
    assert live.issued == {"kickoff"} and live.failures == 0
    assert _names(calls) == ["beep", "toast", "flash"]
    assert errors == []
    assert "Exception in thread" not in err.err
    assert "alert down" not in err.err
    assert _snapshot(tmp_path) == _expected_files()
    assert api.posted == [("/challenge", _expected_post())]
    assert (tmp_path / "inputs.jsonl").read_bytes() == SEEDS["inputs.jsonl"]
    assert (tmp_path / "checkpoints.jsonl").read_bytes() == SEEDS["checkpoints.jsonl"]
    assert (tmp_path / "receipt.json").read_bytes() == SEEDS["receipt.json"]
    assert b"result" not in (tmp_path / "official_challenges.jsonl").read_bytes()


def test_recorded_bytes_match_with_alerts_on_and_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(alert, "default_backends", lambda: _backends(calls))
    on = tmp_path / "on"
    off = tmp_path / "off"
    on.mkdir()
    off.mkdir()
    for key, value in _ON.items():
        monkeypatch.setenv(key, value)
    _live_on, api_on, _marks = _fire(on, legacy=[])
    _wait(calls, 3)
    for key, value in _OFF_ALL.items():
        monkeypatch.setenv(key, value)
    _live_off, api_off, _marks = _fire(off, legacy=[])
    time.sleep(0.05)
    assert _snapshot(on) == _snapshot(off) == _expected_files()
    assert api_on.posted == api_off.posted == [("/challenge", _expected_post())]
    posted = json.dumps(api_on.posted[0][1], sort_keys=True).encode()
    assert posted == json.dumps(api_off.posted[0][1], sort_keys=True).encode()
    assert (on / "official_challenges.jsonl").read_bytes() == (off / "official_challenges.jsonl").read_bytes()
    assert (on / "challenge_gates.jsonl").read_bytes() == (off / "challenge_gates.jsonl").read_bytes()
    assert (on / "inputs.jsonl").read_bytes() == (off / "inputs.jsonl").read_bytes() == SEEDS["inputs.jsonl"]
    assert (on / "receipt.json").read_bytes() == (off / "receipt.json").read_bytes() == SEEDS["receipt.json"]
    assert (on / "checkpoints.jsonl").read_bytes() == (off / "checkpoints.jsonl").read_bytes() == SEEDS["checkpoints.jsonl"]
    assert (on / "challenges.jsonl").read_bytes() == (off / "challenges.jsonl").read_bytes() == b""
