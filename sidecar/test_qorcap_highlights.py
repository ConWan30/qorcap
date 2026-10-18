"""qorcap highlights: locks from the checkpoint chain, refusal while recording, clip cutting + manifest check."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_highlights as hl

T0 = 1_792_000_000_000


def _cp(seq, t_ms, score, prev_score=None, trigger="score_change", prev="0" * 64):
    ev = {"trigger": trigger, "score": score, "prev_score": prev_score, "segment_seq": seq, "segment_hash": f"{seq:064x}",
          "chain_head": f"{seq + 1:064x}", "match_id": "local-test", "role": "home"}
    return {"seq": seq, "t_ms": t_ms, "frames_hash": hl.jcs_sha(ev), "input_hash": "0" * 64, "prev": prev,
            "hash": f"{seq + 100:064x}", "qorcap": ev}


def _bundle(tmp: Path, scores, video_secs=0, with_receipt=True) -> Path:
    b = tmp / "match"
    b.mkdir()
    man = [{"type": "segment", "seq": 0, "kind": "init", "file": "video.mp4", "wall_ms": T0, "frames": 0, "sha256": "aa" * 32,
            "chain_head": "bb" * 32}]
    for i in range(1, 31):
        man.append({"type": "segment", "seq": i, "kind": "media", "file": "video.mp4", "wall_ms": T0 + i * 2000,
                    "frames": i * 60, "sha256": f"{i:064x}", "chain_head": f"{i:064x}"})
    (b / "manifest.jsonl").write_text("".join(json.dumps(m) + "\n" for m in man))
    cps = [_cp(i, T0 + t, {"away": a, "home": h, "quarter": 1, "clock_s": 100}) for i, (t, a, h) in enumerate(scores)]
    cps.insert(1, dict(_cp(50, T0 + 1000, None, trigger="segment"), qorcap={"trigger": "segment"}))
    (b / "checkpoints.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cps))
    if with_receipt:
        (b / "receipt.json").write_text(json.dumps({"schema": "qor-receipt/v0", "match": {"match_id": "local-test"},
                                                    "result": {"home_score": 7, "away_score": 0},
                                                    "checkpoints": {"home": [{"hash": c["hash"]} for c in cps]},
                                                    "trust_tier": "single-attested", "signatures": {"referee": {"sig": "x"}}}))
    if video_secs and shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=30:duration={video_secs}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(b / "video.mp4")], check=True)
    return b


def test_locks_skip_baseline_and_misread(tmp_path):
    b = _bundle(tmp_path, [(500, 0, 0), (10_000, 0, 8), (12_000, 0, 0), (20_000, 0, 6), (30_000, 0, 7), (31_000, 0, 7)])
    receipt = json.loads((b / "receipt.json").read_text())
    locks, skipped, src = hl.find_locks(b, receipt)
    assert src == "live_checkpoint"
    assert [(lk["score_after"]["home"], lk["play"]) for lk in locks] == [(6, "touchdown"), (7, "extra point")]
    assert all(lk["in_receipt"] and lk["frames_hash_matches_evidence"] for lk in locks)
    reasons = {s["score_after"]["home"]: s["reason"] for s in skipped}
    assert "misread" in reasons[8]


def test_tampered_evidence_is_not_a_lock(tmp_path):
    b = _bundle(tmp_path, [(500, 0, 0), (20_000, 0, 6)])
    rows = [json.loads(x) for x in (b / "checkpoints.jsonl").read_text().splitlines()]
    rows[-1]["qorcap"]["score"]["home"] = 3  # evidence edited after the chain was written
    (b / "checkpoints.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    locks, skipped, _ = hl.find_locks(b, json.loads((b / "receipt.json").read_text()))
    assert not locks and "frames_hash" in skipped[0]["reason"]


def test_refuses_while_recording(tmp_path):
    b = _bundle(tmp_path, [(500, 0, 0), (20_000, 0, 6)])
    (b / hl.MARKER).parent.mkdir(parents=True)
    (b / hl.MARKER).write_text("123\n")
    with pytest.raises(hl.Refuse) as e:
        hl.make_highlights(b)
    assert e.value.code == 3
    old = time.time() - 120
    os.utime(b / hl.MARKER, (old, old))  # stale marker (crashed recorder) does not block
    assert not hl.recorder_running(b)


def test_needs_receipt(tmp_path):
    b = _bundle(tmp_path, [(500, 0, 0), (20_000, 0, 6)], with_receipt=False)
    with pytest.raises(hl.Refuse) as e:
        hl.make_highlights(b)
    assert e.value.code == 4


def test_video_clock_maps_wall_to_seconds(tmp_path):
    b = _bundle(tmp_path, [(500, 0, 0)])
    vc = hl.VideoClock(hl.read_jsonl(b / "manifest.jsonl"))
    f, s = vc.locate(T0 + 20_000)
    assert f == "video.mp4" and abs(s - 20.0) < 0.01
    segs = vc.segments_between("video.mp4", T0 + 12_000, T0 + 24_000)
    assert segs[0]["seq"] == 6 and segs[-1]["seq"] == 12


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("burn", [True, False])
def test_cuts_clips_and_manifest_checks(tmp_path, burn):
    b = _bundle(tmp_path, [(500, 0, 0), (20_000, 0, 6), (40_000, 0, 7)], video_secs=60)
    man = hl.make_highlights(b, burn=burn)
    assert [c["lock"]["score_after"]["home"] for c in man["clips"]] == [6, 7]
    c = man["clips"][0]
    assert c["source"]["start_s"] == 12.0 and 11.5 < c["duration_s"] < 12.5
    assert (b / "highlights" / c["vtt"]).read_text().startswith("WEBVTT")
    assert man["receipt"]["sha256"] == hl.sha256_file(b / "receipt.json")
    assert hl.check_manifest(b / "highlights" / "manifest.json") == []
    with open(b / "highlights" / c["file"], "ab") as f:
        f.write(b"x")
    assert any("clip sha256" in e for e in hl.check_manifest(b / "highlights" / "manifest.json"))
