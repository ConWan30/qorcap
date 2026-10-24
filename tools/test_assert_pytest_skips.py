"""A Windows-only submit skip must fail the job when it is recorded on Linux."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assert_pytest_skips as skips  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_SPOT = "sidecar/test_qorcap_submit.py::test_submit_spot_check_end_to_end"
_PSN = "sidecar/test_qorcap_psn.py::test_local_021003_both_players"


def _xml(tmp_path: Path, nodeid: str, message: str, *, count: int | None = None) -> str:
    file, _, rest = nodeid.partition("::")
    parts = rest.split("::") if rest else [""]
    name = parts[-1]
    classes = parts[:-1]
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file.replace("/", ".")
    classname = ".".join([module, *classes])
    cases = (
        f'  <testcase classname="{classname}" name="{name}" file="{file}">\n'
        f'    <skipped type="pytest.skip" message="{message}"/>\n'
        "  </testcase>\n"
    )
    total = 1 if count is None else count
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite tests="{total}">\n'
        f"{cases}"
        "</testsuite>\n",
        encoding="utf-8",
    )
    return str(path)


def _suite_xml(tmp_path: Path, files: tuple[str, ...], count: int) -> str:
    chosen = list(files)
    cases = []
    for i in range(count):
        entry = chosen[i % len(chosen)]
        if "::" in entry:
            file, name = entry.split("::", 1)
        else:
            file, name = entry, f"t{i}"
        module = file[:-3].replace("/", ".") if file.endswith(".py") else file.replace("/", ".")
        cases.append(f'  <testcase classname="{module}" name="{name}" file="{file}"/>\n')
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite tests="{count}">\n'
        + "".join(cases)
        + "</testsuite>\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize("nodeid,reason", sorted(skips.WINDOWS_SKIP_REASONS.items()))
def test_linux_skip_of_a_windows_only_submit_test_fails_the_job(tmp_path, nodeid, reason):
    path = _xml(tmp_path, nodeid, reason)
    bad = skips.unexpected_skips(path, platform="linux")
    assert bad and nodeid in bad[0]
    assert skips.unexpected_skips(path, platform="windows") == []


def test_windows_skip_with_the_wrong_reason_fails(tmp_path):
    path = _xml(tmp_path, _SPOT, "no qortime checkout with the referee service")
    assert skips.unexpected_skips(path, platform="windows")
    assert skips.unexpected_skips(path, platform="linux")


def test_skip_reason_is_compared_before_stripping_whitespace(tmp_path):
    reason = skips.WINDOWS_SKIP_REASONS[_SPOT]
    padded = _xml(tmp_path, _SPOT, reason + " ")
    assert skips.unexpected_skips(padded, platform="windows")
    exact = _xml(tmp_path, _SPOT, reason)
    assert skips.unexpected_skips(exact, platform="windows") == []


def test_prefixed_skip_reason_is_not_an_exact_match(tmp_path, monkeypatch, capsys):
    reason = skips.WINDOWS_SKIP_REASONS[_SPOT]
    path = _xml(tmp_path, _SPOT, f"x; {reason}")
    bad = skips.unexpected_skips(path, platform="windows")
    assert bad and _SPOT in bad[0]
    monkeypatch.setattr(skips.sys, "platform", "win32")
    assert skips.main(["assert_pytest_skips.py", "--platform", "windows", path]) == 1
    exact = _xml(tmp_path, _SPOT, reason)
    assert skips.unexpected_skips(exact, platform="windows") == []
    assert skips.main(["assert_pytest_skips.py", "--platform", "windows", exact]) == 0


def test_skip_must_match_the_full_nodeid(tmp_path):
    reason = skips.WINDOWS_SKIP_REASONS[_SPOT]
    other = _xml(tmp_path, "sidecar/test_other.py::test_submit_spot_check_end_to_end", reason)
    assert skips.unexpected_skips(other, platform="windows")
    evil = _xml(tmp_path, _SPOT + "[evil]", reason)
    assert skips.unexpected_skips(evil, platform="windows")
    assert skips.unexpected_skips(_xml(tmp_path, _SPOT, reason), platform="windows") == []
    foreign = _xml(tmp_path, "sidecar/test_other.py::test_local_021003_both_players", "frames absent")
    assert skips.unexpected_skips(foreign, platform="linux")
    assert skips.unexpected_skips(_xml(tmp_path, _PSN, "frames absent"), platform="linux") == []
    bracket = _xml(tmp_path, _PSN + "[evil]", "frames absent")
    assert skips.unexpected_skips(bracket, platform="linux")


def test_psn_skip_stays_allowed_on_linux(tmp_path):
    path = _xml(tmp_path, _PSN, "opponent frames are not committed")
    assert skips.unexpected_skips(path, platform="linux") == []
    assert skips.unexpected_skips(path, platform="windows") == []


def test_main_reports_a_linux_skip_as_a_failed_job(tmp_path, capsys, monkeypatch):
    nodeid = f"{skips._SUBMIT}::test_live_signed_bundle_still_passes"
    path = _xml(tmp_path, nodeid, skips.WINDOWS_SKIP_REASONS[nodeid])
    monkeypatch.setattr(skips.sys, "platform", "linux")
    assert skips.main(["assert_pytest_skips.py", "--platform", "linux", path]) == 1
    assert nodeid in capsys.readouterr().err
    monkeypatch.setattr(skips.sys, "platform", "win32")
    assert skips.main(["assert_pytest_skips.py", "--platform", "windows", path]) == 0


def test_missing_platform_flag_means_linux(tmp_path, capsys, monkeypatch):
    """A default of windows would accept this skip and the job would stay green."""
    nodeid = f"{skips._SUBMIT}::test_submit_prompt_times_out_to_no"
    path = _xml(tmp_path, nodeid, skips.WINDOWS_SKIP_REASONS[nodeid])
    monkeypatch.setattr(skips.sys, "platform", "linux")
    assert skips.main(["assert_pytest_skips.py", path]) == 1
    assert nodeid in capsys.readouterr().err


def test_platform_flag_must_match_sys_platform(tmp_path, capsys, monkeypatch):
    path = _xml(tmp_path, _PSN, "opponent frames are not committed")
    monkeypatch.setattr(skips.sys, "platform", "linux")
    assert skips.main(["assert_pytest_skips.py", "--platform", "windows", path]) == 2
    err = capsys.readouterr().err
    assert "sys.platform=linux" in err and "windows" in err

    monkeypatch.setattr(skips.sys, "platform", "win32")
    assert skips.main(["assert_pytest_skips.py", "--platform", "linux", path]) == 2
    err = capsys.readouterr().err
    assert "sys.platform=win32" in err

    assert skips.main(["assert_pytest_skips.py", "--platform", "windows", path]) == 0
    monkeypatch.setattr(skips.sys, "platform", "linux")
    assert skips.main(["assert_pytest_skips.py", path]) == 0


def test_collected_below_the_floor_fails(tmp_path):
    path = _suite_xml(tmp_path, skips.LINUX_FILES, skips.LINUX_MIN_COLLECTED - 1)
    bad = skips.collection_problems(path, suite="linux")
    assert any("floor" in line for line in bad)
    ok = _suite_xml(tmp_path, skips.LINUX_FILES, skips.LINUX_MIN_COLLECTED)
    assert skips.collection_problems(ok, suite="linux") == []


def test_zero_collected_tests_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(skips, "LINUX_MIN_COLLECTED", 0)
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuite tests="0"></testsuite>\n',
        encoding="utf-8",
    )
    bad = skips.collection_problems(str(path), suite="linux")
    assert any("collected 0 testcases" in line for line in bad)


def test_floor_below_listed_entries_fails(tmp_path, monkeypatch):
    listed = len(skips.LINUX_FILES)
    monkeypatch.setattr(skips, "LINUX_MIN_COLLECTED", listed - 1)
    path = _suite_xml(tmp_path, skips.LINUX_FILES, listed)
    bad = skips.collection_problems(path, suite="linux")
    assert any(f"below {listed} listed entries" in line for line in bad)


def test_unlisted_test_module_fails_the_suite(tmp_path, monkeypatch, capsys):
    assert skips.unlisted_test_files() == []
    dropped = "sidecar/test_pairing.py"
    linux = skips.LINUX_FILES
    windows = skips.WINDOWS_FILES
    monkeypatch.setattr(skips, "LINUX_FILES", tuple(f for f in linux if f != dropped))
    linux_bad = skips.unlisted_test_files()
    assert any(dropped in line and "linux" in line for line in linux_bad)
    assert not any(dropped in line and "windows" in line for line in linux_bad)
    monkeypatch.setattr(skips, "LINUX_FILES", linux)
    monkeypatch.setattr(skips, "WINDOWS_FILES", tuple(f for f in windows if f != dropped))
    windows_bad = skips.unlisted_test_files()
    assert any(dropped in line and "windows" in line for line in windows_bad)
    assert not any(dropped in line and "linux" in line for line in windows_bad)
    highlight = "sidecar/test_qorcap_highlights.py"
    kept = skips.WINDOWS_ONLY_EXCLUSIONS[highlight]
    monkeypatch.setitem(skips.WINDOWS_ONLY_EXCLUSIONS, highlight, "   ")
    blank = skips.unlisted_test_files()
    assert any(highlight in line and "windows" in line and "no reason" in line for line in blank)
    assert not any(highlight in line and "linux" in line for line in blank)
    monkeypatch.setitem(skips.WINDOWS_ONLY_EXCLUSIONS, highlight, kept)
    reason = skips.EXCLUDED_TEST_FILES["sidecar/test_contracts.py"]
    monkeypatch.setitem(skips.EXCLUDED_TEST_FILES, "sidecar/test_contracts.py", "   ")
    assert any("test_contracts.py" in line and "no reason" in line for line in skips.unlisted_test_files())
    monkeypatch.setitem(skips.EXCLUDED_TEST_FILES, "sidecar/test_contracts.py", reason)
    monkeypatch.setattr(skips, "LINUX_FILES", tuple(f for f in linux if f != dropped))
    files = tuple(f for f in skips.LINUX_FILES if f != dropped)
    path = _suite_xml(tmp_path, files, skips.LINUX_MIN_COLLECTED)
    monkeypatch.setattr(skips.sys, "platform", "linux")
    assert skips.main(["assert_pytest_skips.py", "--suite", "linux", path]) == 1
    assert dropped in capsys.readouterr().err


def test_nested_test_module_is_unlisted(tmp_path):
    nested = tmp_path / "sidecar" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    (nested / "test_hidden.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    bad = skips.unlisted_test_files(tmp_path)
    assert any("sidecar/nested/test_hidden.py" in line and "linux" in line for line in bad)
    assert any("sidecar/nested/test_hidden.py" in line and "windows" in line for line in bad)


def _loose_mark(body: str) -> str:
    return "@pytest.mark.xfail(" + body + ")\ndef test_x():\n    assert False\n"


def test_explicit_xfail_strict_false_fails(tmp_path, monkeypatch, capsys):
    suite = tmp_path / "sidecar"
    suite.mkdir()
    (tmp_path / "tools").mkdir()
    (suite / "test_loose.py").write_text(_loose_mark("strict=False"), encoding="utf-8")
    (suite / "test_multiline.py").write_text(
        _loose_mark("\n    strict=False,\n"),
        encoding="utf-8",
    )
    (suite / "test_strict.py").write_text(_loose_mark("strict=True"), encoding="utf-8")
    bad = skips.loose_xfails(tmp_path)
    assert any("test_loose.py" in line for line in bad)
    assert any("test_multiline.py" in line for line in bad)
    assert not any("test_strict.py" in line for line in bad)
    assert skips.loose_xfails() == []
    monkeypatch.setattr(skips.sys, "platform", "linux")
    needle = "xfail" + "(strict=False)"
    monkeypatch.setattr(
        skips,
        "loose_xfails",
        lambda root=None, needle=needle: [f"sidecar/test_loose.py: {needle} overrides xfail_strict"],
    )
    path = _suite_xml(tmp_path, skips.LINUX_FILES, skips.LINUX_MIN_COLLECTED)
    assert skips.main(["assert_pytest_skips.py", "--suite", "linux", path]) == 1
    assert needle in capsys.readouterr().err


def _git_bash() -> str:
    """Bash CI actually runs. On Windows, ``bash`` on PATH is the WSL stub.

    That stub writes a UTF-16 "Windows Subsystem for Linux ... to install"
    line and exits 1, so a test that calls it never reaches the suite guard.
    GitHub's shell is ``C:\\Program Files\\Git\\bin\\bash.EXE -e``.
    """
    if os.name != "nt":
        return "bash"
    candidates = []
    exepath = os.environ.get("EXEPATH", "")
    if exepath:
        candidates.append(os.path.join(exepath, "bin", "bash.exe"))
        candidates.append(os.path.join(exepath, "usr", "bin", "bash.exe"))
    candidates.extend(
        [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    found = shutil.which("bash") or ""
    if found and "system32" not in found.replace("/", "\\").lower():
        return found
    raise RuntimeError(f"Git bash not found; refusing the WSL stub {found!r}")


def _list_guard_script(suite: str, command: str) -> str:
    """The workflow's list capture, with ``command`` in place of ``python --list``.

    The whole capture line must match, prefix included. ``declare -a`` or
    ``readonly`` in front of the assignment hides the command-substitution
    status, and slicing the line at ``SIDECAR_TESTS`` would drop that prefix.
    ``:`` is an empty list. A path plus ``exit 3`` is a crashing ``--list``
    that still wrote an id, so the empty-array check would not stop it.
    """
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    py = f"python tools/assert_pytest_skips.py --list {suite}"
    expected = f"_suite_list=$({py})"
    lines = text.splitlines()
    matches = [line for line in lines if py in line]
    if len(matches) != 1:
        raise AssertionError(f"{suite} capture lines: {matches}")
    actual = matches[0].strip()
    if actual != expected:
        raise AssertionError(f"{suite} capture line is {actual!r}, expected {expected!r}")
    start = text.index(matches[0])
    stop = text.index("python -m pytest", start)
    return text[start:stop].replace(f"$({py})", f"$({command})", 1) + "echo ran\n"


def test_list_failure_and_an_empty_list_do_not_run_pytest():
    """Run both ci.yml blocks under ``bash -e``, the same flag GitHub passes.

    Each block must reject a failing ``--list`` and an empty list. A missing
    ``exit 1`` on only one block, or ``|| true`` on only the other, stays red.
    """
    bash = _git_bash()
    crash_cmd = "echo sidecar/test_pairing.py; exit 3"
    for suite in ("linux", "windows"):
        crash = subprocess.run(
            [bash, "-e", "-o", "pipefail", "-c", _list_guard_script(suite, crash_cmd)],
            capture_output=True,
            text=True,
        )
        assert crash.returncode != 0, (suite, crash.returncode, crash.stdout, crash.stderr)
        assert "ran" not in crash.stdout, (suite, crash.returncode, crash.stdout, crash.stderr)
        empty = subprocess.run(
            [bash, "-e", "-o", "pipefail", "-c", _list_guard_script(suite, ":")],
            capture_output=True,
            text=True,
        )
        assert empty.returncode == 1, (suite, empty.returncode, empty.stdout, empty.stderr)
        assert "empty" in empty.stderr, (suite, empty.returncode, empty.stdout, empty.stderr)
        assert "ran" not in empty.stdout, (suite, empty.returncode, empty.stdout, empty.stderr)


def test_missing_required_file_fails_even_when_the_count_is_high(tmp_path):
    files = skips.LINUX_FILES[1:]
    path = _suite_xml(tmp_path, files, skips.LINUX_MIN_COLLECTED + len(skips.LINUX_FILES))
    bad = skips.collection_problems(path, suite="linux")
    assert any(skips.LINUX_FILES[0] in line for line in bad)
    assert any("no collected test" in line for line in bad)


def test_suite_list_uses_lf_only():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "assert_pytest_skips.py"), "--list", "linux"],
        capture_output=True,
        check=True,
    )
    assert b"\r" not in proc.stdout
    assert proc.stdout.splitlines()[0] == b"sidecar/test_pairing.py"
    source = (ROOT / "tools" / "assert_pytest_skips.py").read_text(encoding="utf-8")
    assert "sys.stdout.buffer" in source


def test_sidecar_ci_sets_xfail_strict_and_the_suite_gate():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert text.count("-o xfail_strict=true") == 2
    assert text.count("-o junit_family=xunit1") == 2
    assert "--list linux" in text and "--suite linux" in text
    assert "--list windows" in text and "--suite windows" in text
    assert "_suite_list=$(python tools/assert_pytest_skips.py --list linux)" in text
    assert "_suite_list=$(python tools/assert_pytest_skips.py --list windows)" in text
    assert 'SIDECAR_TESTS+=("${_suite_line}")' in text
    assert text.count("suite list is empty") == 2
    assert text.count("must re-collect") == 2
    assert "< <(" not in text
    assert "mapfile" not in text
    stall_ids = (
        "sidecar/test_qorcap_match.py::test_large_inputs_file_keeps_frames_bounded_and_reaches_final",
        "sidecar/test_qorcap_match.py::test_reader_stalled_notice_when_frames_arrive_without_rows",
        "sidecar/test_qorcap_match.py::test_hung_inputs_read_does_not_confirm_final",
        "sidecar/test_qorcap_match.py::test_reader_joined_and_health_sealed_before_sign",
        "sidecar/test_qorcap_match.py::test_stall_shapes_do_not_skew_the_verified_chain",
        "sidecar/test_qorcap_match.py::test_hung_haptics_read_does_not_confirm_final",
        "sidecar/test_qorcap_match.py::test_hung_manifest_read_does_not_confirm_final",
        "sidecar/test_qorcap_match.py::test_hung_on_frame_is_noticed_from_the_status_loop",
        "sidecar/test_qorcap_match.py::test_hung_refresh_shows_refresh_age_ms",
    )
    for files in (skips.LINUX_FILES, skips.WINDOWS_FILES):
        pause = files.index("sidecar/test_final_screen_pause.py")
        assert files[pause + 1] == "sidecar/test_post_final_tail.py"
        reader = files.index("sidecar/test_qorcap_reader.py")
        assert files[reader + 1] == "sidecar/test_c11_snapshot_inputs_timeout.py"
        timing = files.index(
            "sidecar/test_qorcap_match.py::test_reader_timing_log_names_the_three_clocks"
        )
        assert files[timing - len(stall_ids):timing] == stall_ids
    assert skips.LINUX_MIN_COLLECTED == 675
    assert skips.WINDOWS_MIN_COLLECTED == 657


def test_xfail_strict_rejects_an_unexpected_pass(tmp_path):
    path = tmp_path / "test_xpass.py"
    path.write_text("import pytest\n\n@pytest.mark.xfail\ndef test_passes():\n    assert True\n", encoding="utf-8")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)]
    loose = subprocess.run([*cmd, "-o", "xfail_strict=false"], capture_output=True, text=True)
    strict = subprocess.run([*cmd, "-o", "xfail_strict=true"], capture_output=True, text=True)
    assert loose.returncode == 0
    assert strict.returncode != 0
