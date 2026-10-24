"""A Windows-only submit skip must fail the job when it is recorded on Linux."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assert_pytest_skips as skips  # noqa: E402


def _xml(tmp_path: Path, name: str, message: str) -> str:
    path = tmp_path / "junit.xml"
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<testsuite>\n"
        f"  <testcase classname=\"sidecar.test_qorcap_submit\" name=\"{name}\">\n"
        f"    <skipped message=\"{message}\"/>\n"
        "  </testcase>\n"
        "</testsuite>\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize("name,reason", sorted(skips.WINDOWS_SKIP_REASONS.items()))
def test_linux_skip_of_a_windows_only_submit_test_fails_the_job(tmp_path, name, reason):
    path = _xml(tmp_path, name, reason)
    bad = skips.unexpected_skips(path, platform="linux")
    assert bad and name in bad[0]
    assert skips.unexpected_skips(path, platform="windows") == []


def test_windows_skip_with_the_wrong_reason_fails(tmp_path):
    path = _xml(tmp_path, "test_submit_spot_check_end_to_end", "no qortime checkout with the referee service")
    assert skips.unexpected_skips(path, platform="windows")
    assert skips.unexpected_skips(path, platform="linux")


def test_psn_skip_stays_allowed_on_linux(tmp_path):
    path = _xml(tmp_path, "test_local_021003_both_players", "opponent frames are not committed")
    assert skips.unexpected_skips(path, platform="linux") == []
    assert skips.unexpected_skips(path, platform="windows") == []


def test_main_reports_a_linux_skip_as_a_failed_job(tmp_path, capsys):
    name = "test_live_signed_bundle_still_passes"
    path = _xml(tmp_path, name, skips.WINDOWS_SKIP_REASONS[name])
    assert skips.main(["assert_pytest_skips.py", "--platform", "linux", path]) == 1
    assert name in capsys.readouterr().err
    assert skips.main(["assert_pytest_skips.py", "--platform", "windows", path]) == 0
