"""The Windows zip must contain every file the sidecar reads, and signing must say so when it does not."""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import qorcap_match as qm  # noqa: E402
import qorcap_release as rel  # noqa: E402

BANNED = ("attested", "attestation", "hardware-verified", "hardware verified")


def test_release_without_a_banner_pack_installs_and_runs(tmp_path):
    files, _dynamic, bad = rel.discover_runtime_files(HERE)
    assert bad == []
    assert "sidecar/packs/madden27_banner_fragments.npz" not in files
    root = rel.stage_runtime_layout(ROOT, tmp_path / "qorcap-v0-windows-x64")
    assert not (root / "sidecar" / "packs" / "madden27_banner_fragments.npz").is_file()
    assert rel.check_install(root) == []
    script = root / "sidecar" / "qorcap_banner.py"
    spec = importlib.util.spec_from_file_location("qorcap_banner_staged", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._load() == (None, None)
    assert module.detect_banner(object(), None) is None


def test_audit_finds_the_schema_and_skips_optional_programs():
    files, _dynamic, bad = rel.discover_runtime_files(HERE)
    assert bad == []
    assert "contracts/receipt.v0.json" in files
    assert "contracts/compat.json" in files
    assert "sidecar/qorcap_console.py" not in files  # code, copied with the sidecar tree
    assert "sidecar/packs/madden27_standard_score_110028.npz" not in files
    assert "sidecar/packs/madden27_standard_score_084523.npz" not in files
    assert "sidecar/packs/madden27_end_menu_v2.npz" not in files
    assert "sidecar/glimpse/crop_v1.json" in files
    for name in ("ffmpeg.exe", "ffmpeg", "qorcap.exe", "qorcap", "qortime"):
        assert name not in files


def test_console_and_ai_loop_read_no_install_root_files(tmp_path):
    side = tmp_path / "sidecar"
    side.mkdir()
    for name in ("qorcap_console.py", "qorcap_ai_loop.py"):
        shutil.copy(HERE / name, side / name)
    files, dynamic, bad = rel.discover_runtime_files(side)
    assert files == {"contracts/compat.json"}
    assert dynamic == set() and bad == []


def test_release_workflow_copies_contracts_and_fails_when_one_is_missing():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "Copy-Item contracts" in text
    assert "Copy-Item templates" in text
    assert "qorcap_release.py" in text
    for rel_path in (
        "contracts\\receipt.v0.json",
        "contracts\\compat.json",
        "templates\\provenance.json",
        "templates\\template_sources.json",
        "templates\\heldout.json",
        "sidecar\\qorcap_console.py",
        "sidecar\\qorcap_ai_loop.py",
        "sidecar\\qorcap_challenge.py",
    ):
        assert rel_path in text
    assert 'throw "package missing $need"' in text
    assert 'throw "release is missing a file the sidecar reads"' in text


def test_staged_release_loads_the_signing_schema(tmp_path):
    root = rel.stage_runtime_layout(ROOT, tmp_path / "qorcap-v0-windows-x64")
    assert rel.check_install(root) == []
    props = rel.load_signing_props(root)
    assert {"home_score", "away_score"} <= props
    assert (root / "contracts" / "receipt.v0.json").read_bytes() == (ROOT / "contracts" / "receipt.v0.json").read_bytes()


def test_sidecar_only_layout_is_the_v031_crash(tmp_path):
    """v0.3.31 copied sidecar/ and not contracts/, then signing raised FileNotFoundError."""
    root = tmp_path / "qorcap-v0.3.31-windows-x64"
    shutil.copytree(HERE, root / "sidecar", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    problems = rel.check_install(root)
    assert "contracts/receipt.v0.json" in problems
    assert "contracts/compat.json" in problems


def _staged_match(root: Path):
    script = root / "sidecar" / "qorcap_match.py"
    spec = importlib.util.spec_from_file_location("qorcap_match_staged_schema", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_schema_exits_with_a_clear_message(tmp_path):
    root = rel.stage_runtime_layout(ROOT, tmp_path / "rel")
    (root / "contracts" / "receipt.v0.json").unlink()
    module = _staged_match(root)
    with pytest.raises(SystemExit, match="receipt schema is missing") as caught:
        module._closed_props("result")
    message = str(caught.value)
    assert "Traceback" not in message
    assert "FileNotFoundError" not in message
    assert "contracts" in message and "receipt.v0.json" in message


def test_unreadable_schema_json_exits_with_a_clear_message(tmp_path):
    root = rel.stage_runtime_layout(ROOT, tmp_path / "rel")
    (root / "contracts" / "receipt.v0.json").write_text("{", encoding="utf-8")
    module = _staged_match(root)
    with pytest.raises(SystemExit, match="not valid JSON"):
        module._closed_props("result")


def test_local_summary_explains_the_600_ms_ceiling():
    lines = qm.explain_local_partials([
        "referee_local_self_attested",
        "away:kickoff:challenge_latency_out_of_range",
        "away:halftime:challenge_latency_out_of_range",
        "away:end:challenge_latency_out_of_range",
    ])
    text = "\n".join(lines)
    assert "Not checked:  referee_local_self_attested" in text
    assert "challenge_latency_out_of_range" not in text
    assert "Local challenge check: away kickoff, away halftime, away end." in text
    assert "600 ms" in text
    assert "does not stamp a rule" in text
    assert "official referee decides with its own rule" in text
    fresh = "\n".join(line for line in lines if not line.startswith("  Not checked:"))
    for word in BANNED:
        assert word not in fresh.lower()
    only = "\n".join(qm.explain_local_partials(["home:end:challenge_latency_out_of_range"]))
    assert only.startswith("  Local challenge check: home end.")
    assert "Not checked:" not in only
    assert qm.explain_local_partials([]) == []
    assert qm.explain_local_partials(None) == []
    assert qm.explain_local_partials(["away:motion_signal_missing"]) == [
        "  Not checked:  away:motion_signal_missing",
    ]


def test_sign_and_report_uses_the_plain_challenge_lines():
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert "for line in explain_local_partials" in src
    assert "Not checked:  {" not in src


def test_recorder_result_does_not_stamp_a_rule():
    """A rule number on the recorder result would let this PC apply the wider ceiling itself."""
    body = (HERE / "qorcap_challenge.py").read_text(encoding="utf-8").split("def challenge_results", 1)[1]
    assert 'res["rule"]' not in body and "res['rule']" not in body
    assert 'res["reverified_by"] = "recorder"' in body
