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
count below the floor. Zero testcases fail even when the floor is zero. A floor
below the number of listed entries fails on its own.

Every ``sidecar/test_*.py`` and ``tools/test_*.py``, including a file in a
subfolder, must be named by that suite's own list (a node id covers its file)
or excluded from that suite with a reason. A Linux-only file stays on the
Linux list and carries a separate Windows reason. The skip message is compared
as recorded, including surrounding whitespace. ``xfail(strict=False)`` in those
files fails the suite: it would override ``-o xfail_strict=true``.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

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
    "sidecar/test_post_final_tail.py",
    "sidecar/test_start_match_glimpse.py",
    "sidecar/test_qorcap_ai_loop.py",
    "sidecar/test_qorcap_console.py",
    "sidecar/test_qorcap_release.py",
    "tools/test_frame_clock_ab.py",
    "tools/test_assert_pytest_skips.py",
    "sidecar/test_qorcap_reader.py",
    "sidecar/test_c11_snapshot_inputs_timeout.py",
    "sidecar/test_qorcap_match.py::test_refresh_off_the_frame_thread_keeps_four_hertz",
    "sidecar/test_qorcap_match.py::test_incremental_tail_matches_full_parse",
    "sidecar/test_qorcap_match.py::test_tail_reparse_when_the_window_before_the_offset_changes",
    "sidecar/test_qorcap_match.py::test_options_press_is_known_before_end_menu_can_lock",
    "sidecar/test_qorcap_match.py::test_reader_crash_leaves_enter_and_the_heartbeat_running",
    "sidecar/test_qorcap_match.py::test_refresh_thread_is_joined_before_the_reader_returns",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_stops",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_crashes",
    "sidecar/test_qorcap_match.py::test_large_inputs_file_keeps_frames_bounded_and_reaches_final",
    "sidecar/test_qorcap_match.py::test_reader_stalled_notice_when_frames_arrive_without_rows",
    "sidecar/test_qorcap_match.py::test_hung_inputs_read_does_not_confirm_final",
    "sidecar/test_qorcap_match.py::test_reader_joined_and_health_sealed_before_sign",
    "sidecar/test_qorcap_match.py::test_stall_shapes_do_not_skew_the_verified_chain",
    "sidecar/test_qorcap_match.py::test_hung_haptics_read_does_not_confirm_final",
    "sidecar/test_qorcap_match.py::test_hung_manifest_read_does_not_confirm_final",
    "sidecar/test_qorcap_match.py::test_hung_on_frame_is_noticed_from_the_status_loop",
    "sidecar/test_qorcap_match.py::test_hung_refresh_shows_refresh_age_ms",
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
    "sidecar/test_post_final_tail.py",
    "sidecar/test_start_match_glimpse.py",
    "sidecar/test_qorcap_ai_loop.py",
    "sidecar/test_qorcap_console.py",
    "sidecar/test_qorcap_release.py",
    "tools/test_frame_clock_ab.py",
    "tools/test_assert_pytest_skips.py",
    "sidecar/test_qorcap_reader.py",
    "sidecar/test_c11_snapshot_inputs_timeout.py",
    "sidecar/test_qorcap_match.py::test_refresh_off_the_frame_thread_keeps_four_hertz",
    "sidecar/test_qorcap_match.py::test_incremental_tail_matches_full_parse",
    "sidecar/test_qorcap_match.py::test_tail_reparse_when_the_window_before_the_offset_changes",
    "sidecar/test_qorcap_match.py::test_options_press_is_known_before_end_menu_can_lock",
    "sidecar/test_qorcap_match.py::test_reader_crash_leaves_enter_and_the_heartbeat_running",
    "sidecar/test_qorcap_match.py::test_refresh_thread_is_joined_before_the_reader_returns",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_stops",
    "sidecar/test_qorcap_match.py::test_closed_health_span_is_written_when_the_reader_crashes",
    "sidecar/test_qorcap_match.py::test_large_inputs_file_keeps_frames_bounded_and_reaches_final",
    "sidecar/test_qorcap_match.py::test_reader_stalled_notice_when_frames_arrive_without_rows",
    "sidecar/test_qorcap_match.py::test_hung_inputs_read_does_not_confirm_final",
    "sidecar/test_qorcap_match.py::test_reader_joined_and_health_sealed_before_sign",
    "sidecar/test_qorcap_match.py::test_stall_shapes_do_not_skew_the_verified_chain",
    "sidecar/test_qorcap_match.py::test_hung_haptics_read_does_not_confirm_final",
    "sidecar/test_qorcap_match.py::test_hung_manifest_read_does_not_confirm_final",
    "sidecar/test_qorcap_match.py::test_hung_on_frame_is_noticed_from_the_status_loop",
    "sidecar/test_qorcap_match.py::test_hung_refresh_shows_refresh_age_ms",
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
# pytest --collect-only -q of each --list with QORTIME importable. Pin the same
# numbers in tools/test_assert_pytest_skips.py. See README "Sidecar CI suite list".
# The floor is a minimum. A PR that adds tests, and any merge resolution, must
# re-collect and set both floors to those exact counts. Leaving the old floor
# keeps a dropped test green.
# Collect-only of these lists after the tail tests in sidecar/test_post_final_tail.py.
# Main e00d374, including the guard tests, collected 596 linux / 578 windows.
# Re-collect after any list or test change.
LINUX_MIN_COLLECTED = 612
WINDOWS_MIN_COLLECTED = 594

# Absent from both sidecar suites. A reason is required; whitespace does not count.
EXCLUDED_TEST_FILES = {
    "sidecar/test_contracts.py": (
        "the contracts job runs this against unpinned qortime main, not the sidecar suite"
    ),
}

# On the Linux list, and not on the Windows list. The reason is for Windows only.
WINDOWS_ONLY_EXCLUSIONS = {
    "sidecar/test_qorcap_highlights.py": (
        "needs ffmpeg; the Windows sidecar job does not install it"
    ),
    "sidecar/test_qorcap_challenge.py": (
        "needs ffmpeg; the Windows sidecar job does not install it"
    ),
    "sidecar/test_qorcap_hl170123.py": (
        "cuts clips with ffmpeg; the Windows sidecar job does not install it"
    ),
    "sidecar/test_qorcap_seq125430.py": (
        "cuts clips with ffmpeg; the Windows sidecar job does not install it"
    ),
}

_LOOSE_XFAIL = re.compile(r"xfail\s*\([^)]*strict\s*=\s*False", re.DOTALL)


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _covered_files(suite: str) -> set[str]:
    covered = set()
    for entry in suite_files(suite):
        covered.add(entry.split("::", 1)[0].replace("\\", "/"))
    return covered


def _exclusion_reason(suite: str, rel: str) -> str | None:
    if suite == "windows" and rel in WINDOWS_ONLY_EXCLUSIONS:
        return WINDOWS_ONLY_EXCLUSIONS[rel]
    if rel in EXCLUDED_TEST_FILES:
        return EXCLUDED_TEST_FILES[rel]
    return None


def _test_modules(root: Path) -> list[str]:
    """``test_*.py`` under sidecar/ and tools/, including subfolders."""
    found = set()
    for base in ("sidecar", "tools"):
        directory = root / base
        if not directory.is_dir():
            continue
        for path in directory.rglob("test_*.py"):
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def unlisted_test_files(root: Path | None = None) -> list[str]:
    """Test modules missing from one suite list and not excluded from that suite.

    Reads the suite tuples and the exclusion maps at call time. A node id covers
    its file for that list only. A Linux-only file needs a Windows reason of its
    own; sharing one reason across both lists does not excuse it.
    """
    root = root or _repo_root()
    problems = []
    for rel in _test_modules(root):
        for suite in ("linux", "windows"):
            if rel in _covered_files(suite):
                continue
            reason = _exclusion_reason(suite, rel)
            if isinstance(reason, str) and reason.strip():
                continue
            if reason is not None:
                problems.append(f"{rel}: {suite} exclusion has no reason")
            else:
                problems.append(
                    f"{rel}: not in the {suite} suite list or excluded with a reason"
                )
    return problems


def loose_xfails(root: Path | None = None) -> list[str]:
    """``xfail(strict=False)`` overrides the CI ini and must not appear."""
    root = root or _repo_root()
    problems = []
    for rel in _test_modules(root):
        text = (root / rel).read_text(encoding="utf-8")
        if _LOOSE_XFAIL.search(text):
            problems.append(f"{rel}: xfail(strict=False) overrides xfail_strict")
    return problems


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
        # Compare before stripping. A trailing space is not the recorded reason.
        msg = skipped[0].get("message") or ""
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
    if len(cases) == 0:
        problems.append(f"collected 0 testcases; sidecar {suite} suite ran nothing")
    if floor < len(files):
        problems.append(
            f"sidecar {suite} floor {floor} is below {len(files)} listed entries"
        )
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
            bad.extend(unlisted_test_files())
            bad.extend(loose_xfails())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if bad:
        print("sidecar suite check failed:", file=sys.stderr)
        for line in bad:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
