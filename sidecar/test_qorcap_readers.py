"""Reader manifest: hashes of loaded code and packs, never key files."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_readers as R  # noqa: E402


def _runtime():
    return [{"id": "ffmpeg", "version": "n7.0.1", "sha256": "11" * 32}]


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
    man = R.build_manifest(files, qorcap_version="0.3.32", runtimes=_runtime())
    assert man["schema"] == "qorcap-readers/v0"
    assert man["readers"][0]["id"] == "scorebug"
    paths = [f["path"] for f in man["readers"][0]["files"]]
    assert paths == ["sidecar/reader.py", "sidecar/packs/pack.npz"]
    by_path = {f["path"]: f["sha256"] for f in man["readers"][0]["files"]}
    assert by_path["sidecar/reader.py"] == hashlib.sha256(b"print(1)\n").hexdigest()
    assert by_path["sidecar/packs/pack.npz"] == hashlib.sha256(b"NPZ-bytes").hexdigest()
    assert set(man) == {"schema", "qorcap_version", "readers", "runtimes"}
    assert man["runtimes"] == _runtime()
    assert "reader_verified_by_referee" not in man
    blob = json.dumps(man)
    assert "SECRET-KEY-MATERIAL" not in blob
    assert "referee.key" not in blob
    assert "root.pem" not in blob


def test_unsafe_paths_and_flag_words():
    assert not R.frame_bind_enabled("")
    assert not R.frame_bind_enabled("0")
    assert R.frame_bind_enabled("1")
    assert R.frame_bind_enabled(" true ")
    for bad in ("../x.py", "/etc/passwd", "C:video.py", "keys/a.py", "a/../../b.py", "a.key", "",
                "sidecar/reader.py.", "sidecar/reader.py ", "café.py", "sidecar/CON.py", "nul.txt",
                "COM1.py", "lpt9.txt", "aux", "PRN.py", "sidecar/id_ed25519", "secret/a.py"):
        assert not R.safe_rel_path(bad), bad
    assert R.safe_rel_path("sidecar/qorcap_standard.py")
    assert R.safe_rel_path("qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py")
    assert R.safe_rel_path("sidecar/console.py")
    assert R.safe_rel_path("sidecar/com10.py")


def test_vendored_pack_is_the_file_load_would_open(tmp_path):
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir()
    (pack_dir / "madden27_standard_v1.npz").write_bytes(b"old")
    chosen = pack_dir / "madden27_standard_v2.npz"
    chosen.write_bytes(b"new")
    (pack_dir / "notes.txt").write_bytes(b"no")
    (tmp_path / "reader.py").write_bytes(b"")
    mod = type("M", (), {"__file__": str(tmp_path / "reader.py")})()
    assert R.vendored_scorebug_packs(mod) == [chosen]


def test_glimpse_binding_is_closed_and_does_not_claim_a_veto_already_ran():
    got = R.glimpse_binding()
    if "state" in got:
        assert got == {"state": "unbound"}
    else:
        assert set(got) == {"model", "version", "prompt_sha256", "veto_ran"}
        assert got["veto_ran"] is False
        assert "reader_verified_by_referee" not in got


def test_empty_load_is_not_a_reader_claim(tmp_path):
    try:
        R.build_manifest([], qorcap_version="0.3.32", runtimes=_runtime())
    except ValueError as e:
        assert "no reader" in str(e)
    else:
        raise AssertionError("an empty manifest must not look like a bound reader")


def test_duplicate_ids_paths_case_and_trailing_dots_are_refused(tmp_path):
    code = tmp_path / "reader.py"
    code.write_bytes(b"print(1)\n")
    other = tmp_path / "other.py"
    other.write_bytes(b"print(2)\n")

    def files(*rows):
        return [R.LoadedFile(*row) for row in rows]

    with pytest.raises(ValueError, match="reader id"):
        R.build_manifest(files(
            ("Scorebug", "1", "code", code, "sidecar/reader.py"),
            ("scorebug", "1", "code", other, "sidecar/other.py"),
        ), qorcap_version="0.3.32", runtimes=_runtime())

    with pytest.raises(ValueError, match="duplicate path"):
        R.build_manifest(files(
            ("scorebug", "1", "code", code, "sidecar/Reader.py"),
            ("vision", "1", "code", other, "Sidecar/reader.py"),
        ), qorcap_version="0.3.32", runtimes=_runtime())

    with pytest.raises(ValueError, match="both code and pack"):
        R.build_manifest(files(
            ("scorebug", "1", "code", code, "sidecar/reader.py"),
            ("scorebug", "1", "pack", code, "sidecar/reader.py"),
        ), qorcap_version="0.3.32", runtimes=_runtime())

    with pytest.raises(ValueError, match="no reader"):
        R.build_manifest(files(
            ("scorebug", "1", "code", code, "sidecar/reader.py."),
        ), qorcap_version="0.3.32", runtimes=_runtime())

    with pytest.raises(ValueError, match="runtime id"):
        R.build_manifest(files(
            ("scorebug", "1", "code", code, "sidecar/reader.py"),
        ), qorcap_version="0.3.32", runtimes=[
            {"id": "ffmpeg", "version": "n7", "sha256": "11" * 32},
            {"id": "FFmpeg.", "version": "n8", "sha256": "22" * 32},
        ])

    with pytest.raises(ValueError, match="no runtime"):
        R.build_manifest(files(
            ("scorebug", "1", "code", code, "sidecar/reader.py"),
        ), qorcap_version="0.3.32", ffmpeg={"state": "unbound"}, opencv=None)
