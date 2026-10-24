"""Reader manifest: hashes of loaded code and packs, never key files."""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_readers as R  # noqa: E402


def test_manifest_hashes_loaded_files_and_skips_keys(tmp_path):
    code = tmp_path / "reader.py"
    code.write_bytes(b"print(1)\n")
    pack = tmp_path / "pack.npz"
    pack.write_bytes(b"NPZ-bytes")
    key = tmp_path / "referee.key"
    key.write_bytes(b"SECRET-KEY-MATERIAL")
    nested = tmp_path / "keys"
    nested.mkdir()
    hidden = nested / "root.pem"
    hidden.write_bytes(b"SECRET-KEY-MATERIAL")
    files = [
        R.LoadedFile("scorebug", "0.3.32", "code", code, "sidecar/reader.py"),
        R.LoadedFile("scorebug", "0.3.32", "pack", pack, "sidecar/packs/pack.npz"),
        R.LoadedFile("scorebug", "0.3.32", "code", key, "sidecar/referee.key"),
        R.LoadedFile("scorebug", "0.3.32", "code", hidden, "keys/root.pem"),
        R.LoadedFile("scorebug", "0.3.32", "code", code, "../reader.py"),
    ]
    man = R.build_manifest(files, qorcap_version="0.3.32")
    assert man["schema"] == "qorcap-readers/v0"
    assert man["readers"][0]["id"] == "scorebug"
    paths = [f["path"] for f in man["readers"][0]["files"]]
    assert paths == ["sidecar/reader.py", "sidecar/packs/pack.npz"]
    by_path = {f["path"]: f["sha256"] for f in man["readers"][0]["files"]}
    assert by_path["sidecar/reader.py"] == hashlib.sha256(b"print(1)\n").hexdigest()
    assert by_path["sidecar/packs/pack.npz"] == hashlib.sha256(b"NPZ-bytes").hexdigest()
    blob = json.dumps(man)
    assert "SECRET-KEY-MATERIAL" not in blob
    assert "referee.key" not in blob
    assert "root.pem" not in blob


def test_unsafe_paths_and_flag_words():
    assert not R.frame_bind_enabled("")
    assert not R.frame_bind_enabled("0")
    assert R.frame_bind_enabled("1")
    assert R.frame_bind_enabled(" true ")
    for bad in ("../x.py", "/etc/passwd", "C:video.py", "keys/a.py", "a/../../b.py", "a.key", ""):
        assert not R.safe_rel_path(bad), bad
    assert R.safe_rel_path("sidecar/qorcap_standard.py")
    assert R.safe_rel_path("qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py")


def test_empty_load_is_not_a_reader_claim(tmp_path):
    try:
        R.build_manifest([], qorcap_version="0.3.32")
    except ValueError as e:
        assert "no reader" in str(e)
    else:
        raise AssertionError("an empty manifest must not look like a bound reader")
