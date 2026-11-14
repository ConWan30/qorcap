"""S2S body cue: fail-closed, role-bound model pins and OpenVINO telemetry opt-out (stdlib only).

P173-L1 / P174-L1: every model file is read ONCE, its sha256 is compared with the hash bound to its ROLE in
models.manifest.json, and only those same bytes are loaded (from memory, or from a private checked temp copy when
a library needs a path). A file whose hash is not the one for its role raises PinError; there is no override.
Roles: yolox_onnx, rfdetr_pth, rfdetr_onnx, ov_xml, ov_bin.

The manifest is itself pinned: its bytes must hash to code_sha256["models.manifest.json"] recorded in
FROZEN-fusion-params.json, whose own bytes must hash to frozen_pin.FROZEN_PARAMS_SHA256.

P173-L3: OpenVINO's model-conversion API sends anonymous telemetry unless the machine has opted out
(`opt_in_out --opt_out`, which writes "0" to the openvino_telemetry consent file). Any bodycue path that imports
openvino calls require_openvino_telemetry_opt_out() first and refuses to run unless the consent file reads "0".
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "models.manifest.json"
FROZEN = HERE / "FROZEN-fusion-params.json"
ROLES = {
    "yolox_onnx": ("yolox_tiny_onnx", "sha256"),
    "rfdetr_pth": ("rfdetr_nano_pth", "sha256"),
    "rfdetr_onnx": ("rfdetr_nano_onnx", "sha256"),
    "ov_xml": ("rfdetr_nano_ir", "xml_sha256"),
    "ov_bin": ("rfdetr_nano_ir", "bin_sha256"),
}


class PinError(RuntimeError):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise PinError(f"duplicate JSON key {k!r}")
        d[k] = v
    return d


def parse_json_bytes(b: bytes):
    return json.loads(b.decode("utf-8"), object_pairs_hook=_no_dupes)


def read_frozen(path=None, expected_sha=None) -> dict:
    """Read FROZEN-fusion-params.json once, require its published sha256, parse those same bytes."""
    if expected_sha is None:
        from frozen_pin import FROZEN_PARAMS_SHA256 as expected_sha  # noqa: N811
    b = Path(path or FROZEN).read_bytes()
    got = sha256_bytes(b)
    if got != expected_sha:
        raise PinError(f"frozen params sha256 {got[:12]}… != published {str(expected_sha)[:12]}…; refusing")
    return parse_json_bytes(b)


def load_manifest(manifest=None, expected_sha=None) -> dict:
    """Manifest bytes must hash to the value recorded in the frozen file (or `expected_sha`), read once."""
    if expected_sha is None:
        expected_sha = read_frozen()["code_sha256"]["models.manifest.json"]
    b = Path(manifest or MANIFEST).read_bytes()
    got = sha256_bytes(b)
    if got != expected_sha:
        raise PinError(f"models.manifest.json sha256 {got[:12]}… is not the frozen one; refusing")
    return parse_json_bytes(b)


def role_hash(manifest: dict, role: str) -> str:
    if role not in ROLES:
        raise PinError(f"unknown model role {role!r}")
    key, field = ROLES[role]
    try:
        h = manifest[key][field]
    except (KeyError, TypeError):
        raise PinError(f"manifest has no {key}.{field} for role {role}") from None
    if not isinstance(h, str) or len(h) != 64:
        raise PinError(f"manifest hash for role {role} is malformed")
    return h


def read_pinned(path, role: str, manifest: dict | None = None) -> bytes:
    """Read the file once; return its bytes only if they hash to the pin for `role`. Fail closed otherwise."""
    m = load_manifest() if manifest is None else manifest
    want = role_hash(m, role)
    p = Path(path)
    if not p.is_file():
        raise PinError(f"model file missing: {p.name}")
    b = p.read_bytes()
    if sha256_bytes(b) != want:
        raise PinError(f"{p.name} is not the pinned {role} (sha256 {sha256_bytes(b)[:12]}…); refusing to load")
    return b


def read_pinned_ir(xml_path, manifest: dict | None = None) -> tuple[bytes, bytes]:
    m = load_manifest() if manifest is None else manifest
    x = Path(xml_path)
    return read_pinned(x, "ov_xml", m), read_pinned(x.with_suffix(".bin"), "ov_bin", m)


@contextlib.contextmanager
def pinned_copy(path, role: str, manifest: dict | None = None, name: str | None = None):
    """For libraries that need a path: write the checked bytes to a private temp dir, yield that path, delete it."""
    b = read_pinned(path, role, manifest)
    d = tempfile.mkdtemp(prefix="bodycue-pin-")
    try:
        os.chmod(d, 0o700)
        q = Path(d) / (name or Path(path).name)
        q.write_bytes(b)
        del b
        yield q
    finally:
        shutil.rmtree(d, ignore_errors=True)


def openvino_consent_file() -> Path | None:
    """Path of the OpenVINO telemetry consent file, from openvino_telemetry itself when installed."""
    try:
        from openvino_telemetry.utils.opt_in_checker import OptInChecker
    except Exception:
        return None
    c = OptInChecker()
    base, sub = c.consent_file_base_dir(), c.consent_file_subdirectory()
    if base is None or sub is None:
        return None
    return Path(base) / sub / "openvino_telemetry"


def require_openvino_telemetry_opt_out(consent_file=None) -> None:
    """Raise PinError unless the consent file exists and reads exactly '0' (opted out)."""
    f = Path(consent_file) if consent_file is not None else openvino_consent_file()
    if f is None or not f.is_file():
        raise PinError("OpenVINO telemetry opt-out not recorded; run `opt_in_out --opt_out` in this venv first")
    if f.read_text(encoding="utf-8", errors="replace").strip() != "0":
        raise PinError("OpenVINO telemetry is not opted out; run `opt_in_out --opt_out` in this venv first")
