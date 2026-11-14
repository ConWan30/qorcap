"""P173-L1 / P174-L1 / P174-L2 / P173-L3: role-bound fail-closed pins, pinned manifest and frozen file, OpenVINO
telemetry opt-out. stdlib + numpy only; temp files only."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pins  # noqa: E402
from frozen_pin import FROZEN_PARAMS_SHA256  # noqa: E402


def _h(b):
    return hashlib.sha256(b).hexdigest()


MAN = {"yolox_tiny_onnx": {"sha256": _h(b"yolox")}, "rfdetr_nano_pth": {"sha256": _h(b"pth")},
       "rfdetr_nano_onnx": {"sha256": _h(b"rf-onnx")},
       "rfdetr_nano_ir": {"xml_sha256": _h(b"<xml/>"), "bin_sha256": _h(b"bin")}, "versions": {"x": "1"}}


def _w(tmp_path, name, data):
    p = tmp_path / name; p.write_bytes(data); return p


def test_pinned_role_passes_and_returns_the_same_bytes(tmp_path):
    assert pins.read_pinned(_w(tmp_path, "y.onnx", b"yolox"), "yolox_onnx", MAN) == b"yolox"


def test_unpinned_or_missing_file_fails_closed(tmp_path):
    with pytest.raises(pins.PinError):
        pins.read_pinned(_w(tmp_path, "y.onnx", b"swapped"), "yolox_onnx", MAN)
    with pytest.raises(pins.PinError):
        pins.read_pinned(tmp_path / "absent.onnx", "yolox_onnx", MAN)
    with pytest.raises(pins.PinError):
        pins.read_pinned(_w(tmp_path, "y.onnx", b"yolox"), "no_such_role", MAN)


@pytest.mark.parametrize("data,role", [(b"rf-onnx", "rfdetr_pth"), (b"yolox", "rfdetr_pth"),
                                       (b"pth", "rfdetr_onnx"), (b"rf-onnx", "yolox_onnx")])
def test_pinned_file_in_the_wrong_role_is_rejected(tmp_path, data, role):
    with pytest.raises(pins.PinError):
        pins.read_pinned(_w(tmp_path, "m.bin", data), role, MAN)


def test_ir_needs_the_pinned_xml_and_bin(tmp_path):
    x = _w(tmp_path, "m.xml", b"<xml/>")
    with pytest.raises(pins.PinError):
        pins.read_pinned_ir(x, MAN)  # .bin missing
    _w(tmp_path, "m.bin", b"other")
    with pytest.raises(pins.PinError):
        pins.read_pinned_ir(x, MAN)
    _w(tmp_path, "m.bin", b"bin")
    assert pins.read_pinned_ir(x, MAN) == (b"<xml/>", b"bin")


def test_manifest_bytes_must_match_the_expected_hash(tmp_path):
    b = json.dumps(MAN).encode()
    p = _w(tmp_path, "m.json", b)
    assert pins.load_manifest(p, _h(b)) == MAN
    with pytest.raises(pins.PinError):
        pins.load_manifest(p, _h(b + b" "))


def test_frozen_file_must_hash_to_the_published_value(tmp_path):
    b = b'{"code_sha256": {"models.manifest.json": "x"}}'
    p = _w(tmp_path, "f.json", b)
    assert pins.read_frozen(p, _h(b))["code_sha256"]["models.manifest.json"] == "x"
    with pytest.raises(pins.PinError):
        pins.read_frozen(p, _h(b"other"))


def test_duplicate_json_keys_are_refused():
    with pytest.raises(pins.PinError):
        pins.parse_json_bytes(b'{"a": 1, "a": 2}')


def test_pinned_copy_holds_the_checked_bytes_and_is_removed(tmp_path):
    src = _w(tmp_path, "w.pth", b"pth")
    with pins.pinned_copy(src, "rfdetr_pth", MAN, name="rf-detr-nano.pth") as q:
        assert q.read_bytes() == b"pth" and q.name == "rf-detr-nano.pth" and q.parent != src.parent
    assert not q.exists() and not q.parent.exists()


def test_committed_frozen_manifest_and_roles_are_consistent():
    assert pins.sha256_file(pins.FROZEN) == FROZEN_PARAMS_SHA256
    m = pins.load_manifest()
    for role in pins.ROLES:
        assert len(pins.role_hash(m, role)) == 64
    assert "P173-L2" in m["rfdetr_nano_pth"]["pin_note"]


@pytest.mark.parametrize("content,ok", [("0", True), ("0\n", True), ("1", False), ("", False)])
def test_openvino_telemetry_opt_out_is_enforced(tmp_path, content, ok):
    f = tmp_path / "openvino_telemetry"; f.write_text(content)
    if ok:
        pins.require_openvino_telemetry_opt_out(f)
    else:
        with pytest.raises(pins.PinError):
            pins.require_openvino_telemetry_opt_out(f)


def test_missing_consent_file_fails_closed(tmp_path):
    with pytest.raises(pins.PinError):
        pins.require_openvino_telemetry_opt_out(tmp_path / "nope")


def test_detect_person_refuses_unpinned_model_before_any_runtime_import(tmp_path):
    import detect_person as dp
    f = _w(tmp_path, "evil.onnx", b"not a pinned model")
    with pytest.raises(pins.PinError):
        dp.Runner("yolox", "onnxruntime", str(f))
    with pytest.raises(pins.PinError):
        dp.Runner("yolox", "openvino", str(f))  # no pinned role for YOLOX on OpenVINO
