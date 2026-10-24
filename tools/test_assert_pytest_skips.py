"""A Windows-only submit skip must fail the job when it is recorded on Linux."""
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


def test_missing_required_file_fails_even_when_the_count_is_high(tmp_path):
    files = skips.LINUX_FILES[1:]
    path = _suite_xml(tmp_path, files, skips.LINUX_MIN_COLLECTED + len(skips.LINUX_FILES))
    bad = skips.collection_problems(path, suite="linux")
    assert any(skips.LINUX_FILES[0] in line for line in bad)
    assert any("no collected test" in line for line in bad)


def test_sidecar_ci_sets_xfail_strict_and_the_suite_gate():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert text.count("-o xfail_strict=true") == 2
    assert text.count("-o junit_family=xunit1") == 2
    assert "--list linux" in text and "--suite linux" in text
    assert "--list windows" in text and "--suite windows" in text
    assert skips.LINUX_MIN_COLLECTED == 534
    assert skips.WINDOWS_MIN_COLLECTED == 516


def test_xfail_strict_rejects_an_unexpected_pass(tmp_path):
    path = tmp_path / "test_xpass.py"
    path.write_text("import pytest\n\n@pytest.mark.xfail\ndef test_passes():\n    assert True\n", encoding="utf-8")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)]
    loose = subprocess.run([*cmd, "-o", "xfail_strict=false"], capture_output=True, text=True)
    strict = subprocess.run([*cmd, "-o", "xfail_strict=true"], capture_output=True, text=True)
    assert loose.returncode == 0
    assert strict.returncode != 0
