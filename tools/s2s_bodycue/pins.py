"""S2S body cue: fail-closed model pins and OpenVINO telemetry opt-out (stdlib only).

P173-L1: every model file is checked against the committed models.manifest.json sha256 BEFORE it is loaded.
A file whose sha256 is not pinned there (or an OpenVINO IR whose .xml/.bin pair does not match one entry) raises
PinError and nothing is loaded. There is no override flag.

P173-L3: OpenVINO's model-conversion API sends anonymous telemetry unless the machine has opted out
(`opt_in_out --opt_out`, which writes "0" to the openvino_telemetry consent file). Any bodycue path that imports
openvino calls require_openvino_telemetry_opt_out() first and refuses to run unless the consent file reads "0".
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "models.manifest.json"


class PinError(RuntimeError):
    pass


def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def pinned_hashes(manifest=MANIFEST) -> dict:
    """{kind: set(sha256)} from the manifest: 'file' for single files, 'ir_pair' for (xml, bin) tuples."""
    m = json.load(open(manifest, encoding="utf-8"))
    files, pairs = set(), set()
    for k, v in m.items():
        if not isinstance(v, dict):
            continue
        if "sha256" in v:
            files.add(v["sha256"])
        if "xml_sha256" in v and "bin_sha256" in v:
            pairs.add((v["xml_sha256"], v["bin_sha256"]))
    return {"file": files, "ir_pair": pairs}


def check_model(path, manifest=MANIFEST) -> dict:
    """Fail closed unless `path` (or an .xml with its sibling .bin) is pinned. Returns the hashes on success."""
    p = Path(path)
    if not p.is_file():
        raise PinError(f"model file missing: {p.name}")
    pins = pinned_hashes(manifest)
    if p.suffix.lower() == ".xml":
        b = p.with_suffix(".bin")
        if not b.is_file():
            raise PinError(f"OpenVINO IR weights missing: {b.name}")
        pair = (sha256_file(p), sha256_file(b))
        if pair not in pins["ir_pair"]:
            raise PinError(f"OpenVINO IR {p.name} (+.bin) is not pinned in {Path(manifest).name}; refusing to load")
        return {"xml_sha256": pair[0], "bin_sha256": pair[1]}
    h = sha256_file(p)
    if h not in pins["file"]:
        raise PinError(f"{p.name} sha256 {h[:12]}… is not pinned in {Path(manifest).name}; refusing to load")
    return {"sha256": h}


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
