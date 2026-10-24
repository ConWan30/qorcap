"""Fail the job if pytest skipped a test that is not on the allowlist.

A missing import, a missing fixture, or a blanket skip must not turn a required
reader test into a green check. The allowlist is only for cases whose inputs
are deliberately absent from this repo.

Windows-only submit skips are not in that set. They are accepted only when this
process is told it is the Windows suite and the junit skip message equals the
reason recorded below. A longer message that merely contains that reason fails.
The match is the full node id (``file.py::test_name``), so the same test name
in another module, or a parameter suffix, does not pass.

``--platform`` and ``--suite`` have to name this machine. ``sys.platform ==
"linux"`` accepts only ``linux``; ``sys.platform == "win32"`` accepts only
``windows``. Omitting the flag means linux, so a Windows job that forgets it
fails closed.

``--suite`` also requires every file in that suite's list to have produced a
testcase, and at least ``MIN_COLLECTED`` testcases. Dropping a file drops the
count below the floor.
"""
import sys
import xml.etree.ElementTree as ET

# Full node id. The skip reason is not checked. Keep this list short.
ALLOWED = {
    # Match 021003 frames show the opponent and are not committed.
    # Set QORCAP_PSN_FRAMES locally to run this test.
    "sidecar/test_qorcap_psn.py::test_local_021003_both_players",
}

# The Windows sidecar job has no ffmpeg, and select() cannot wait on a pipe there.
# Linux installs ffmpeg and runs these. A skip on Linux is a failure.
_FFMPEG = "needs ffmpeg; the Windows sidecar job does not install it"
_PIPE = "select() cannot wait on a pipe on Windows; readline would block the sidecar job"
_SUBMIT = "sidecar/test_qorcap_submit.py"
WINDOWS_SKIP_REASONS = {
    f"{_SUBMIT}::test_submit_spot_check_end_to_end": _FFMPEG,
    f"{_SUBMIT}::test_submit_reports_refusal": _FFMPEG,
    f"{_SUBMIT}::test_submit_rejected_bundle": _FFMPEG,
    f"{_SUBMIT}::test_official_live_anchors_and_challenges": _FFMPEG,
    f"{_SUBMIT}::test_submit_is_install_key_signed_and_unsigned_is_refused": _FFMPEG,
    f"{_SUBMIT}::test_submit_from_another_pc_key_is_rejected": _FFMPEG,
    f"{_SUBMIT}::test_fill_challenge_results_on_real_fmp4": _FFMPEG,
    f"{_SUBMIT}::test_side_corrected_bundle_refused_before_network": _FFMPEG,
    f"{_SUBMIT}::test_tampered_timestamp_resigned_offline_is_refused": _FFMPEG,
    f"{_SUBMIT}::test_offline_signed_receipt_refused_when_live_countersigned_log_exists": _FFMPEG,
    f"{_SUBMIT}::test_live_signed_bundle_still_passes": _FFMPEG,
    f"{_SUBMIT}::test_submit_prompt_times_out_to_no": _PIPE,
}

# The sidecar CI jobs run exactly these files, in this order. --list prints them.
# MIN_COLLECTED is the collect-only count of that list. A dropped file lands under it.
LINUX_FILES = (
    "sidecar/test_pairing.py",
    "sidecar/test_qorcap_highlights.py",
    "sidecar/test_auto_highlights.py",
    "sidecar/test_final_screen_pause.py",
    "sidecar/test_start_match_glimpse.py",
    "sidecar/test_qorcap_ai_loop.py",
    "sidecar/test_qorcap_console.py",
    "sidecar/test_qorcap_release.py",
    "tools/test_frame_clock_ab.py",
    "tools/test_assert_pytest_skips.py",
    "sidecar/test_qorcap_reader.py",
    "sidecar/test_qorcap_match.py::test_refresh_off_the_frame_thread_keeps_four_hertz",
    "sidecar/test_qorcap_match.py::test_incremental_tail_matches_full_parse",
    "sidecar/test_qorcap_match.py::test_tail_reparse_when_the_window_before_the_offset_changes",
    "sidecar/test_qorcap_match.py::test_options_press_is_known_before_end_menu_can_lock",
    "sidecar/test_qorcap_match.py::test_reader_crash_leaves_enter_and_the_heartbeat_running",
    "sidecar/test_qorcap_match.py::test_refresh_thread_is_joined_before_the_reader_returns",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_stops",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_crashes",
    "sidecar/test_qorcap_match.py::test_reader_timing_log_names_the_three_clocks",
    "sidecar/test_qorcap_standard.py",
    "sidecar/test_qorcap_score_grammar.py",
    "sidecar/test_qorcap_hl170123.py",
    "sidecar/test_qorcap_seq125430.py",
    "sidecar/test_qorcap_tnp.py",
    "sidecar/test_qorcap_snp.py",
    "sidecar/test_qorcap_mnp.py",
    "sidecar/test_qorcap_endmenu.py",
    "sidecar/test_qorcap_touchdown.py",
    "sidecar/test_qorcap_td_glimpse.py",
    "sidecar/test_qorcap_playcall.py",
    "sidecar/test_qorcap_psn.py",
    "sidecar/test_qorcap_challenge.py",
    "sidecar/test_qorcap_challenge_alert.py",
    "sidecar/test_qorcap_paths.py",
    "sidecar/test_qorcap_sidepick.py",
    "sidecar/test_score_timeline.py",
    "sidecar/test_qorcap_glimpse_watch.py",
    "sidecar/test_qorcap_glimpse_witness.py",
    "sidecar/test_qorcap_submit.py",
)
WINDOWS_FILES = (
    "sidecar/test_pairing.py",
    "sidecar/test_auto_highlights.py",
    "sidecar/test_final_screen_pause.py",
    "sidecar/test_start_match_glimpse.py",
    "sidecar/test_qorcap_ai_loop.py",
    "sidecar/test_qorcap_console.py",
    "sidecar/test_qorcap_release.py",
    "tools/test_frame_clock_ab.py",
    "tools/test_assert_pytest_skips.py",
    "sidecar/test_qorcap_reader.py",
    "sidecar/test_qorcap_match.py::test_refresh_off_the_frame_thread_keeps_four_hertz",
    "sidecar/test_qorcap_match.py::test_incremental_tail_matches_full_parse",
    "sidecar/test_qorcap_match.py::test_tail_reparse_when_the_window_before_the_offset_changes",
    "sidecar/test_qorcap_match.py::test_options_press_is_known_before_end_menu_can_lock",
    "sidecar/test_qorcap_match.py::test_reader_crash_leaves_enter_and_the_heartbeat_running",
    "sidecar/test_qorcap_match.py::test_refresh_thread_is_joined_before_the_reader_returns",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_stops",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_crashes",
    "sidecar/test_qorcap_match.py::test_reader_timing_log_names_the_three_clocks",
    "sidecar/test_qorcap_standard.py",
    "sidecar/test_qorcap_score_grammar.py",
    "sidecar/test_qorcap_tnp.py",
    "sidecar/test_qorcap_snp.py",
    "sidecar/test_qorcap_mnp.py",
    "sidecar/test_qorcap_endmenu.py",
    "sidecar/test_qorcap_touchdown.py",
    "sidecar/test_qorcap_td_glimpse.py",
    "sidecar/test_qorcap_playcall.py",
    "sidecar/test_qorcap_psn.py",
    "sidecar/test_qorcap_paths.py",
    "sidecar/test_qorcap_challenge_alert.py",
    "sidecar/test_qorcap_sidepick.py",
    "sidecar/test_score_timeline.py",
    "sidecar/test_qorcap_glimpse_watch.py",
    "sidecar/test_qorcap_glimpse_witness.py",
    "sidecar/test_qorcap_submit.py",
)
# pytest --collect-only of the lists above (QORTIME importable). A dropped file
# or node id lands under the floor.
LINUX_MIN_COLLECTED = 535
WINDOWS_MIN_COLLECTED = 517


def suite_files(suite: str) -> tuple[str, ...]:
    if suite == "linux":
        return LINUX_FILES
    if suite == "windows":
        return WINDOWS_FILES
    raise ValueError(f"suite must be linux or windows, not {suite!r}")


def suite_floor(suite: str) -> int:
    if suite == "linux":
        return LINUX_MIN_COLLECTED
    if suite == "windows":
        return WINDOWS_MIN_COLLECTED
    raise ValueError(f"suite must be linux or windows, not {suite!r}")


def host_platform() -> str:
    """The ``--platform`` name for this process, or ``sys.platform`` when it is neither."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        return "linux"
    return sys.platform


def case_nodeid(case: ET.Element) -> str:
    """Pytest node id for one junit testcase.

    xunit1 records ``file``. The name keeps any parameter suffix, so
    ``test_name[evil]`` is not ``test_name``. Without ``file`` (xunit2) the id
    is ``classname::name`` and does not match a path node id.
    """
    name = case.get("name") or ""
    file = (case.get("file") or "").replace("\\", "/")
    classname = case.get("classname") or ""
    if not file:
        return f"{classname}::{name}" if classname else name
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file.replace("/", ".")
    if not classname or classname == module:
        return f"{file}::{name}"
    prefix = module + "."
    if classname.startswith(prefix):
        classes = classname[len(prefix):].replace(".", "::")
        return f"{file}::{classes}::{name}"
    return f"{file}::{classname}::{name}"


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
        nodeid = case_nodeid(case)
        msg = (skipped[0].get("message") or "").strip()
        if nodeid in ALLOWED:
            continue
        expected = WINDOWS_SKIP_REASONS.get(nodeid) if plat == "windows" else None
        if expected is not None and msg == expected:
            continue
        bad.append(f"{nodeid}: {msg}")
    return bad


def collection_problems(path: str, *, suite: str) -> list[str]:
    """A dropped test file, or a count under the floor, fails the suite."""
    files = suite_files(suite)
    floor = suite_floor(suite)
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    problems = []
    if len(cases) < floor:
        problems.append(f"collected {len(cases)} tests; sidecar {suite} floor is {floor}")
    seen_files = set()
    seen_ids = set()
    for case in cases:
        seen_files.add((case.get("file") or "").replace("\\", "/"))
        seen_ids.add(case_nodeid(case))
    for required in files:
        if "::" in required:
            if required not in seen_ids:
                problems.append(f"no collected test {required}")
        elif required not in seen_files:
            problems.append(f"no collected test from {required}")
    return problems


def main(argv: list[str]) -> int:
    args = argv[1:]
    if len(args) == 2 and args[0] == "--list":
        try:
            files = suite_files(args[1])
        except ValueError:
            print("usage: assert_pytest_skips.py --list linux|windows", file=sys.stderr)
            return 2
        # Windows Python translates print() newlines to CRLF. Pytest then looks
        # for "file.py\r" and reports that the file is missing. Write LF only.
        out = sys.stdout.buffer
        for name in files:
            out.write(name.encode("ascii") + b"\n")
        return 0

    platform = "linux"
    suite = None
    path = None
    i = 0
    while i < len(args):
        if args[i] == "--platform" and i + 1 < len(args):
            platform = args[i + 1]
            i += 2
        elif args[i] == "--suite" and i + 1 < len(args):
            suite = args[i + 1]
            i += 2
        elif path is None and not args[i].startswith("--"):
            path = args[i]
            i += 1
        else:
            print(
                "usage: assert_pytest_skips.py [--platform linux|windows] [--suite linux|windows] JUNIT.xml",
                file=sys.stderr,
            )
            return 2
    if path is None:
        print(
            "usage: assert_pytest_skips.py [--platform linux|windows] [--suite linux|windows] JUNIT.xml",
            file=sys.stderr,
        )
        return 2
    if suite is not None:
        if suite not in {"linux", "windows"}:
            print(f"suite must be linux or windows, not {suite!r}", file=sys.stderr)
            return 2
        if "--platform" in args and platform.strip().lower() != suite:
            print(f"--platform {platform} does not match --suite {suite}", file=sys.stderr)
            return 2
        platform = suite
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
        if suite is not None:
            bad.extend(collection_problems(path, suite=suite))
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
