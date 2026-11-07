"""QorData adversarial fixtures. Each case is one acceptance test."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import qorcap_qordata as qd

_FIXTURES = json.loads((Path(__file__).resolve().parent / "fixtures" / "qordata" / "fixtures.json").read_text(encoding="utf-8"))
_PARAMS = [
    (group, case["id"], case["input"], case["expected"])
    for group, cases in _FIXTURES.items()
    for case in cases
]


def _match(actual, expected, path=""):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), (path, actual)
        for key, value in expected.items():
            assert key in actual, (path, key, actual)
            _match(actual[key], value, f"{path}.{key}")
        return
    assert actual == expected, (path, actual, expected)


@pytest.mark.parametrize(
    "group,case_id,doc,expected",
    _PARAMS,
    ids=[f"{group}:{case_id}" for group, case_id, _doc, _expected in _PARAMS],
)
def test_qordata_fixture(group, case_id, doc, expected):
    actual = qd.evaluate(group, case_id, doc)
    _match(actual, expected)
