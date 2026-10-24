"""Fail the job if pytest skipped a test that is not on the allowlist.

A missing import, a missing fixture, or a blanket skip must not turn a required
reader test into a green check. The allowlist is only for cases whose inputs
are deliberately absent from this repo.

Windows-only submit skips are not in that set. They are accepted only when this
process is told ``--platform windows`` and the junit skip message contains the
reason recorded below. A Linux skip of the same test fails the job, even when
the message text matches.

``--platform`` has to name this machine. ``sys.platform == "linux"`` accepts
only ``linux``; ``sys.platform == "win32"`` accepts only ``windows``. Omitting
the flag means linux, so a Windows job that forgets it fails closed.
"""
import sys
import xml.etree.ElementTree as ET

# node name, without a parametrize suffix. Keep this list short and justified.
# These names are allowed on every OS. The skip reason is not checked.
ALLOWED = {
    # Match 021003 frames show the opponent and are not committed.
    # Set QORCAP_PSN_FRAMES locally to run sidecar/test_qorcap_psn.py::test_local_021003_both_players.
    "test_local_021003_both_players",
}

# The Windows sidecar job has no ffmpeg, and select() cannot wait on a pipe there.
# Linux installs ffmpeg and runs these. A skip on Linux is a failure.
_FFMPEG = "needs ffmpeg; the Windows sidecar job does not install it"
_PIPE = "select() cannot wait on a pipe on Windows; readline would block the sidecar job"
WINDOWS_SKIP_REASONS = {
    "test_submit_spot_check_end_to_end": _FFMPEG,
    "test_submit_reports_refusal": _FFMPEG,
    "test_submit_rejected_bundle": _FFMPEG,
    "test_official_live_anchors_and_challenges": _FFMPEG,
    "test_submit_is_install_key_signed_and_unsigned_is_refused": _FFMPEG,
    "test_submit_from_another_pc_key_is_rejected": _FFMPEG,
    "test_fill_challenge_results_on_real_fmp4": _FFMPEG,
    "test_side_corrected_bundle_refused_before_network": _FFMPEG,
    "test_tampered_timestamp_resigned_offline_is_refused": _FFMPEG,
    "test_offline_signed_receipt_refused_when_live_countersigned_log_exists": _FFMPEG,
    "test_live_signed_bundle_still_passes": _FFMPEG,
    "test_submit_prompt_times_out_to_no": _PIPE,
}


def host_platform() -> str:
    """The ``--platform`` name for this process, or ``sys.platform`` when it is neither."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        return "linux"
    return sys.platform


def unexpected_skips(path: str, *, platform: str = "linux") -> list[str]:
    plat = platform.strip().lower()
    if plat not in {"linux", "windows"}:
        raise ValueError(f"platform must be linux or windows, not {platform!r}")
    root = ET.parse(path).getroot()
    bad = []
    for case in root.iter("testcase"):
        skipped = case.findall("skipped")
        if not skipped:
            continue
        name = case.get("name") or ""
        base = name.split("[", 1)[0]
        msg = (skipped[0].get("message") or "").strip()
        if base in ALLOWED:
            continue
        expected = WINDOWS_SKIP_REASONS.get(base) if plat == "windows" else None
        if expected and expected in msg:
            continue
        bad.append(f"{case.get('classname')}::{name}: {msg}")
    return bad


def main(argv: list[str]) -> int:
    platform = "linux"
    args = argv[1:]
    if len(args) == 3 and args[0] == "--platform":
        platform = args[1]
        path = args[2]
    elif len(args) == 1:
        path = args[0]
    else:
        print("usage: assert_pytest_skips.py [--platform linux|windows] JUNIT.xml", file=sys.stderr)
        return 2
    plat = platform.strip().lower()
    if plat not in {"linux", "windows"}:
        print(f"platform must be linux or windows, not {platform!r}", file=sys.stderr)
        return 2
    host = host_platform()
    if host != plat:
        print(
            f"--platform {plat} does not match this OS (sys.platform={sys.platform})",
            file=sys.stderr,
        )
        return 2
    try:
        bad = unexpected_skips(path, platform=plat)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if bad:
        print("unexpected pytest skips (a required test did not run):", file=sys.stderr)
        for line in bad:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
