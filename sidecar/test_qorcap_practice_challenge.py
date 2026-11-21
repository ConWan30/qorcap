"""Tests for local CoD practice-lane response-bound prompt and official CoD closure."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qorcap_console as qc  # noqa: E402
import qorcap_match as qm  # noqa: E402
import qorcap_practice_challenge as qpc  # noqa: E402
from qorcap_submit import OfficialLive  # noqa: E402

BANNED_WORDS = (
    "veri" + "fied",
    "pro" + "ven",
    "sk" + "ill",
    "reaction " + "time",
    "anti-" + "cheat",
    "hu" + "man",
    "proof-of-" + "human",
)


class _StubApi:
    def __init__(self, status=None):
        self.posts: list[tuple[str, dict]] = []
        self._status = status or {"state": "recording"}

    def get(self, path: str, timeout: float = 1.0):
        return dict(self._status)

    def post(self, path: str, body: dict, timeout: float = 2.0):
        self.posts.append((path, dict(body)))
        return {"ok": True, "challenge": dict(body), "checkpoint": {"seq": 1}}


class _StubConsole:
    def __init__(self, branded: bool = True):
        self.branded = branded
        self.calls: list[dict] = []

    def show_challenge(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.branded


def _assert_no_banned_words(text: str) -> None:
    low = text.lower()
    for w in BANNED_WORDS:
        assert w not in low, f"banned word {w!r} in {text!r}"


def test_enabled_only_in_practice_lane_for_cod():
    assert qpc.enabled("cod", {"QORCAP_LANE": "practice"}) is True
    assert qpc.enabled("cod", {"QORCAP_LANE": "practice", "QORCAP_PRACTICE_CHALLENGE": "1"}) is True
    assert qpc.enabled("cod", {"QORCAP_LANE": "practice", "QORCAP_PRACTICE_CHALLENGE": "0"}) is False
    assert qpc.enabled("cod", {"QORCAP_LANE": "practice", "QORCAP_PRACTICE_CHALLENGE": "off"}) is False
    assert qpc.enabled("cod", {}) is False
    assert qpc.enabled("cod", {"QORCAP_LANE": "official"}) is False
    assert qpc.enabled("madden", {"QORCAP_LANE": "practice"}) is False
    assert qpc.enabled(None, {"QORCAP_LANE": "practice"}) is False


def test_pattern_for_matches_rust_and_rejects_bad_nonces():
    pat = qpc.pattern_for("0123456789abcdef0123456789abcdef")
    assert len(pat) == 5
    assert pat[0] == "OPTIONS"
    for bad in ("short", "0123456789ABCDEF0123456789ABCDEF", "g" * 32):
        try:
            qpc.pattern_for(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_practice_prompt_posts_locally_and_renders_non_referee_card():
    api = _StubApi()
    con = _StubConsole(branded=True)
    said: list[str] = []
    prompter = qpc.PracticeChallengePrompter(api, role="home", console=con, say=said.append, delay_s=0.0)
    resp = prompter.trigger(nonce="0123456789abcdef0123456789abcdef", issued_ms=1_700_000_000_000)
    assert resp is not None and resp["ok"] is True
    assert len(api.posts) == 1
    path, body = api.posts[0]
    assert path == "/challenge"
    assert body["practice"] is True
    assert body["phase"] == "practice"
    assert body["game"] == "cod"
    assert body["issued_ms"] == 1_700_000_000_000
    assert body["deadline_ms"] == 1_700_000_090_000
    assert len(con.calls) == 1
    assert con.calls[0]["practice"] is True
    assert said == []

    card = qc.ChallengeCard(
        phase="practice",
        label="cod practice",
        pattern=body["pattern"],
        issued_ms=body["issued_ms"],
        deadline_ms=body["deadline_ms"],
        practice=True,
    )
    rendered = qc.grid_text(qc.layout_challenge(card, now_ms=body["issued_ms"] + 5000))
    assert "PRACTICE RESPONSE BOUND" in rendered
    assert "REFEREE CHALLENGE" not in rendered
    _assert_no_banned_words(rendered)

    card.result = "pass"
    card.referee_verified = True  # Even if set, practice layout must never emit referee wording.
    card.note = "Got it. Buttons matched on this pad. Not a referee result."
    settled = qc.grid_text(qc.layout_challenge(card, now_ms=body["issued_ms"] + 6000))
    assert "Referee" not in settled
    _assert_no_banned_words(settled)


def test_practice_prompt_plain_fallback_and_thread_start():
    api = _StubApi()
    said: list[str] = []
    prompter = qpc.PracticeChallengePrompter(api, role="home", console=None, say=said.append, delay_s=0.0)
    stop = threading.Event()
    t = prompter.start(stop)
    t.join(timeout=2.0)
    assert len(api.posts) == 1
    assert len(said) == 2
    for line in said:
        _assert_no_banned_words(line)
    assert "never changes receipt status" in said[1]


def test_official_cod_challenges_stay_closed(tmp_path):
    api = _StubApi()
    live = OfficialLive("https://127.0.0.1:9", tmp_path, "m-cod", "home", api, challenges=False)
    assert live.challenges is False
    # Verify qorcap_match.py keeps official_challenges closed for game == "cod".
    match_src = Path(qm.__file__).read_text(encoding="utf-8")
    assert 'challenges=a.official_challenges and game != "cod"' in match_src


def test_read_hand_challenge_summary_wording(tmp_path):
    rec = {
        "schema": "qorcap-hand-response-bound/v0-info",
        "advisory_only": True,
        "receipt_status_changed": False,
        "phase": "practice",
        "id": "01234567",
        "status": "MEASURED",
        "outcome": "answered_in_bound",
        "bound_head_status": "matched",
        "stream_source": "hand_timeline",
        "statement": "MEASURED: the sealed pad stream answered the nonce within the bound. Advisory only; never changes receipt status. Does not establish who held the controller.",
    }
    (tmp_path / "hand_challenge.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    lines = qpc.read_hand_challenge_summary(tmp_path)
    assert len(lines) == 1
    assert "MEASURED answered_in_bound" in lines[0]
    _assert_no_banned_words(lines[0])
