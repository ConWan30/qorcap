"""Path containment for manifests, highlight names, and the QorTime zip."""
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qorcap_glimpse_attest as A  # noqa: E402
import qorcap_highlights as hl  # noqa: E402
import qorcap_match as qm  # noqa: E402
import qorcap_paths as P  # noqa: E402
import qorcap_submit as qs  # noqa: E402


def test_bundle_child_rejects_escapes(tmp_path):
    bundle = tmp_path / "match"
    bundle.mkdir()
    (bundle / "video.mp4").write_bytes(b"inside")
    secret = tmp_path / "device.key"
    secret.write_bytes(b"SECRET-KEY-MATERIAL")
    assert P.bundle_child(bundle, "video.mp4") == bundle / "video.mp4"
    for name in ("../device.key", "..\\device.key", "/etc/passwd", "C:video.mp4", "a/b.mp4", "", ".", "..", "vid\x00eo.mp4"):
        assert P.bundle_child(bundle, name) is None, name
    link = bundle / "link.mp4"
    link.symlink_to(secret)
    assert P.bundle_child(bundle, "link.mp4") is None
    assert secret.read_bytes() == b"SECRET-KEY-MATERIAL"


def test_video_files_and_segment_read_stay_inside_the_bundle(tmp_path):
    bundle = tmp_path / "match"
    bundle.mkdir()
    (bundle / "video.mp4").write_bytes(b"picture")
    secret = tmp_path / "device.key"
    secret.write_bytes(b"SECRET-KEY-MATERIAL")
    (bundle / "manifest.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "segment", "seq": 0, "file": "video.mp4", "offset": 0, "bytes": 7}),
            json.dumps({"type": "segment", "seq": 1, "file": "../device.key", "offset": 0, "bytes": 19}),
            json.dumps({"type": "segment", "seq": 2, "file": str(secret), "offset": 0, "bytes": 19}),
        ]) + "\n",
        encoding="utf-8",
    )
    files = qm.video_files(bundle)
    assert files == [bundle / "video.mp4"]
    assert qs.segment_bytes(bundle, {"file": "video.mp4", "offset": 0, "bytes": 7}) == b"picture"
    for name in ("../device.key", str(secret), "C:video.mp4"):
        try:
            qs.segment_bytes(bundle, {"file": name, "offset": 0, "bytes": 4})
        except ValueError:
            pass
        else:
            raise AssertionError(name)
    assert secret.read_bytes() == b"SECRET-KEY-MATERIAL"


def test_highlight_check_does_not_open_a_path_outside_the_folder(tmp_path):
    hl_dir = tmp_path / "highlights"
    hl_dir.mkdir()
    secret = tmp_path / "device.key"
    secret.write_bytes(b"SECRET-KEY-MATERIAL")
    man = {"schema": hl.SCHEMA, "receipt": {"sha256": "0" * 64}, "clips": [
        {"id": "hl00", "file": "../device.key", "sha256": "ab", "lock": {"frames_hash": "no", "evidence": {}}}]}
    (hl_dir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    errs = hl.check_manifest(hl_dir / "manifest.json", tmp_path)
    assert any("not inside the highlights folder" in e for e in errs)
    assert secret.read_bytes() == b"SECRET-KEY-MATERIAL"


def test_zip_extract_refuses_parent_and_symlink(tmp_path):
    dest = tmp_path / "out"
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "pwn")
    try:
        P.safe_extract_zip(bad.getvalue(), dest)
    except ValueError:
        pass
    else:
        raise AssertionError("parent path extracted")
    assert not (tmp_path / "evil.txt").exists()

    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777) << 16
        z.writestr(info, "../device.key")
    try:
        P.safe_extract_zip(link.getvalue(), dest)
    except ValueError:
        pass
    else:
        raise AssertionError("symlink extracted")
    assert not (dest / "link").exists()

    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("qortime-pin/qor_receipt/receipt.py", "print('ok')\n")
    P.safe_extract_zip(good.getvalue(), dest)
    assert (dest / "qortime-pin" / "qor_receipt" / "receipt.py").read_text() == "print('ok')\n"


def test_submit_refuses_a_segment_index_or_name_outside_the_bundle(tmp_path):
    """A referee response is untrusted. A bad index or a ``../`` name must not be uploaded."""
    bundle = tmp_path / "match"
    bundle.mkdir()
    (bundle / "video.mp4").write_bytes(b"picture")
    secret = tmp_path / "device.key"
    secret.write_bytes(b"SECRET-KEY-MATERIAL")
    (bundle / "manifest.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "segment", "seq": 0, "file": "video.mp4", "offset": 0, "bytes": 7}),
            json.dumps({"type": "segment", "seq": 1, "file": "../device.key", "offset": 0, "bytes": 19}),
        ]) + "\n",
        encoding="utf-8",
    )
    for name in ("checkpoints.jsonl", "receipt.json", "bundle.json"):
        (bundle / name).write_text("{}\n", encoding="utf-8")

    class Fake:
        def __init__(self, required):
            self.required, self.puts, self.token = required, [], None

        def call(self, method, path, data=None, ctype="application/json", timeout=None, headers=None):
            if method == "GET" and path.startswith("/v1/referee"):
                return {"pubkey": "not-the-official-key"}
            if method == "GET" and "/v1/submit/challenge" in path:
                raise qs.RefereeError(404, "no challenge")
            if method == "POST":
                return {"id": "sub", "state": "accepted", "commit": "abc", "upload_token": "tok",
                        "required": self.required}
            if method == "PUT":
                self.puts.append((path, data))
                return {}
            raise AssertionError(method + " " + path)

    device = SimpleNamespace(pubkey_hex="ab" * 32)
    bad = Fake([99])
    code = qs.submit(bundle, base="http://referee.test", client=bad, device=device)
    assert code == 2 and bad.puts == []
    saved = json.loads((bundle / "submission.json").read_text())
    assert saved["error"] == "segment index outside the manifest"

    escape = Fake([1])
    code = qs.submit(bundle, base="http://referee.test", client=escape, device=device)
    assert code == 2 and escape.puts == []
    assert secret.read_bytes() == b"SECRET-KEY-MATERIAL"


def test_observer_key_uses_the_same_private_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("QORCAP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    path = A.observer_key_path()
    assert path == tmp_path / "home" / "keys" / "glimpse-observer.key"
    assert A.init_key(path) == 0
    assert path.is_file() and P.mode_owner_only(path)
    text = path.read_text(encoding="utf-8").strip()
    assert len(text) == 64 and text not in "wrote observer key"
    seed = path.read_bytes()
    (path.parent / "device.key").write_bytes(seed)
    assert A._collides(path, path.parent)
