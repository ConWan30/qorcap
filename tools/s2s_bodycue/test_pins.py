"""P173-L1 / P173-L3: fail-closed model pins and OpenVINO telemetry opt-out. stdlib + numpy only."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pins  # noqa: E402


def _h(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def man(tmp_path):
    m = {"a": {"sha256": _h(b"good")}, "ir": {"xml_sha256": _h(b"<xml/>"), "bin_sha256": _h(b"bin")}, "versions": {"x": "1"}}
    p = tmp_path / "m.json"; p.write_text(json.dumps(m))
    return p


def test_pinned_file_passes(tmp_path, man):
    f = tmp_path / "m.onnx"; f.write_bytes(b"good")
    assert pins.check_model(f, man) == {"sha256": _h(b"good")}


def test_unpinned_or_missing_file_fails_closed(tmp_path, man):
    f = tmp_path / "m.onnx"; f.write_bytes(b"swapped")
    with pytest.raises(pins.PinError):
        pins.check_model(f, man)
    with pytest.raises(pins.PinError):
        pins.check_model(tmp_path / "absent.onnx", man)


def test_ir_needs_the_pinned_xml_bin_pair(tmp_path, man):
    x = tmp_path / "m.xml"; x.write_bytes(b"<xml/>")
    with pytest.raises(pins.PinError):
        pins.check_model(x, man)  # .bin missing
    (tmp_path / "m.bin").write_bytes(b"other")
    with pytest.raises(pins.PinError):
        pins.check_model(x, man)
    (tmp_path / "m.bin").write_bytes(b"bin")
    assert pins.check_model(x, man)["bin_sha256"] == _h(b"bin")


def test_committed_manifest_pins_every_model_and_notes_l2():
    m = json.load(open(pins.MANIFEST, encoding="utf-8"))
    p = pins.pinned_hashes()
    assert m["yolox_tiny_onnx"]["sha256"] in p["file"] and m["rfdetr_nano_pth"]["sha256"] in p["file"]
    assert m["rfdetr_nano_onnx"]["sha256"] in p["file"] and len(p["ir_pair"]) == 1
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
    f = tmp_path / "evil.onnx"; f.write_bytes(b"not a pinned model")
    with pytest.raises(pins.PinError):
        dp.Runner("yolox", "onnxruntime", str(f))
