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
