"""Offline clock A/B math. No video decode, no receipt."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frame_clock_ab as ab


def _manifest(late_ms=0):
    rows = [{"type": "segment", "kind": "init", "file": "video.mp4", "mono_ms": 0, "frames": 0}]
    for i in range(1, 20):
        fr = i * 60
        mono = fr * 1000 // 30
        if i == 19:
            mono += late_ms
        rows.append({"type": "segment", "kind": "media", "file": "video.mp4", "mono_ms": mono, "frames": fr})
    return rows


def test_no_stamps_leaves_measured_null_and_an_on_line_fit_is_flat():
    out = ab.clock_ab(_manifest(), [])
    assert out["measured"] is None
    assert out["fit"]["resid_abs_max_ms"] <= 1
    assert "clock_source" not in out


def test_a_late_fragment_close_shows_up_in_the_fit_and_stamps_can_disagree():
    late = ab.clock_ab(_manifest(800), [])
    assert late["fit"]["resid_abs_max_ms"] >= 400
    stamps = [{"type": "frame", "file": "video.mp4", "frame": f, "t_mono_us": round(f * 1_000_000 / 30), "pts_90k": f * 3000} for f in range(10)]
    on_line = ab.clock_ab(_manifest(), stamps)
    assert on_line["measured"]["vs_fit_abs_max_ms"] <= 2
    assert on_line["measured"]["interval_abs_dev_max_ms"] == 0
    # Arrivals 800 ms after the fitted frame time. The flag-off line would call these frames early.
    shifted = [dict(s, t_mono_us=s["t_mono_us"] + 800_000) for s in stamps]
    off = ab.clock_ab(_manifest(), shifted)
    assert off["measured"]["vs_fit_abs_p50_ms"] >= 700


def test_report_reads_a_bundle_and_does_not_require_the_sidecar_file(tmp_path):
    man = tmp_path / "manifest.jsonl"
    man.write_text("\n".join(json.dumps(r) for r in _manifest()) + "\n", encoding="utf-8")
    out = ab.report(tmp_path)
    assert out["frame_clock_file"] is False
    assert out["frame_clock_frames"] == 0
    assert out["measured"] is None
    assert out["fit"]["n"] == 19
