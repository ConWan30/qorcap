"""Fail the job if pytest skipped a test that is not on the allowlist.

A missing import, a missing fixture, or a blanket skip must not turn a required
reader test into a green check. The allowlist is only for cases whose inputs
are deliberately absent from this repo.
"""
import sys
import xml.etree.ElementTree as ET

# node name, without a parametrize suffix. Keep this list short and justified.
ALLOWED = {
    # Match 021003 frames show the opponent and are not committed.
    # Set QORCAP_PSN_FRAMES locally to run sidecar/test_qorcap_psn.py::test_local_021003_both_players.
    "test_local_021003_both_players",
    # sidecar/test_qorcap_submit.py builds an in-process referee bundle with ffmpeg.
    # Linux installs ffmpeg and runs these. The Windows sidecar job does not install
    # ffmpeg, so requires_ffmpeg skips them there. A skip on Linux is also allowed by
    # this list; the Linux job is still expected to execute them.
    "test_submit_spot_check_end_to_end",
    "test_submit_reports_refusal",
    "test_submit_rejected_bundle",
    "test_official_live_anchors_and_challenges",
    "test_submit_is_install_key_signed_and_unsigned_is_refused",
    "test_submit_from_another_pc_key_is_rejected",
    "test_fill_challenge_results_on_real_fmp4",
    "test_side_corrected_bundle_refused_before_network",
    "test_tampered_timestamp_resigned_offline_is_refused",
    "test_offline_signed_receipt_refused_when_live_countersigned_log_exists",
    "test_live_signed_bundle_still_passes",
}


def unexpected_skips(path: str) -> list[str]:
    root = ET.parse(path).getroot()
    bad = []
    for case in root.iter("testcase"):
        skipped = case.findall("skipped")
        if not skipped:
            continue
        name = case.get("name") or ""
        base = name.split("[", 1)[0]
        if base in ALLOWED:
            continue
        msg = (skipped[0].get("message") or "").strip()
        bad.append(f"{case.get('classname')}::{name}: {msg}")
    return bad


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: assert_pytest_skips.py JUNIT.xml", file=sys.stderr)
        return 2
    bad = unexpected_skips(argv[1])
    if bad:
        print("unexpected pytest skips (a required test did not run):", file=sys.stderr)
        for line in bad:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
