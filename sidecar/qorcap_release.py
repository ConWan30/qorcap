"""Files the sidecar opens from the directory that contains ``sidecar/``.

That directory is the repo when developing, and the unzipped Windows release when
a player runs Start Match. ``release.yml`` copies ``sidecar/`` and ``contracts/``
into that folder. This module is the check that the copy actually contains every
file those modules read, and that the signing path can load ``receipt.v0.json``
from it.

``qorcap.exe``, ``ffmpeg.exe``, and the vendored ``qortime/`` tree are located at
runtime and installed by their own release steps. They are not in this list.
"""
from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
import uuid
from pathlib import Path

# Vendored registry. The sidecar does not open it today (submit constants and the
# challenge bounds are copied into code, and tests compare them). It still ships:
# the release that dropped ``contracts/`` also dropped the schema signing reads.
ALWAYS_INSTALL_FILES = ("contracts/compat.json",)

# Existence probes and programs the release installs separately.
_OPTIONAL_INSTALL = frozenset({"qorcap", "qorcap.exe", "ffmpeg", "ffmpeg.exe", "qortime"})
_READ_TEXT = frozenset({"read_text", "read_bytes"})
_FILE_SUFFIXES = (".json", ".md", ".npz", ".txt")


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _subscript_index(node: ast.AST):
    if isinstance(node, ast.Constant):
        return node.value
    value = getattr(node, "value", None)
    if isinstance(value, ast.Constant):
        return value.value
    return None


class _Origin:
    """A path relative to the module file or to the install root (its parent)."""

    def __init__(self, kind: str, parts: tuple[str, ...]) -> None:
        self.kind = kind  # "file" | "sidecar" | "install" | "above"
        self.parts = parts

    def join(self, name: str) -> _Origin:
        return _Origin(self.kind, self.parts + (name,))

    def parent(self) -> _Origin | None:
        if self.kind == "file":
            return _Origin("sidecar", ())
        if self.parts:
            return _Origin(self.kind, self.parts[:-1])
        if self.kind == "sidecar":
            return _Origin("install", ())
        if self.kind == "install":
            return _Origin("above", ())
        return None


def _eval(node: ast.AST, lookup) -> _Origin | None:
    if isinstance(node, ast.Name):
        return lookup(node.id)
    if isinstance(node, ast.Call):
        if (
            _call_name(node.func) in {"Path", "pathlib.Path"}
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "__file__"
        ):
            return _Origin("file", ())
        if isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
            return _eval(node.func.value, lookup)
        # A function whose body returns a fixed path, called with no arguments.
        if isinstance(node.func, ast.Name) and not node.args and not node.keywords:
            bound = lookup(node.func.id)
            if isinstance(bound, _Origin):
                return bound
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _eval(node.value, lookup)
        return base.parent() if base else None
    if isinstance(node, ast.Attribute) and node.attr == "parents":
        return None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        base = _eval(node.value.value, lookup)
        index = _subscript_index(node.slice)
        if base is None or not isinstance(index, int) or index < 0:
            return None
        cur: _Origin | None = base
        for _ in range(index + 1):
            if cur is None:
                return None
            cur = cur.parent()
        return cur
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval(node.left, lookup)
        if left is None:
            return None
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return left.join(node.right.value)
        return left.join("*")
    return None


def _rel(origin: _Origin) -> str | None:
    if origin.kind not in {"sidecar", "install"} or not origin.parts or "*" in origin.parts:
        return None
    if origin.kind == "sidecar":
        rel = "sidecar/" + "/".join(origin.parts)
    else:
        rel = "/".join(origin.parts)
    if rel in _OPTIONAL_INSTALL or rel.startswith("target/"):
        return None
    return rel


def _looks_like_file(name: str) -> bool:
    return name.endswith(_FILE_SUFFIXES)


class _Scan(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.scopes: list[dict[str, _Origin]] = [{}]
        self.files: set[str] = set()
        self.dynamic_dirs: set[str] = set()
        self.unclassified: list[str] = []
        self._returns: list[list[_Origin | None]] = []

    def _lookup(self, name: str) -> _Origin | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _add(self, origin: _Origin | None, *, read: bool) -> None:
        if origin is None:
            return
        if "*" in origin.parts:
            if not read:
                return
            if origin.kind == "install":
                shown = "/".join(origin.parts)
                self.unclassified.append(f"{self.filename}: install-root read {shown}")
                return
            if origin.kind == "sidecar":
                prefix = [p for p in origin.parts if p != "*"]
                self.dynamic_dirs.add("sidecar/" + "/".join(prefix) if prefix else "sidecar")
            return
        rel = _rel(origin)
        if rel is None:
            return
        if read or _looks_like_file(origin.parts[-1]):
            self.files.add(rel)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append({})
        self._returns.append([])
        self.generic_visit(node)
        returns = self._returns.pop()
        self.scopes.pop()
        # receipt_schema_path() is a call, not a path expression. Remember a function
        # that only returns one fixed path so the later read_text is still visible.
        if returns and all(item is not None and item.kind == returns[0].kind and item.parts == returns[0].parts for item in returns):
            self.scopes[-1][node.name] = returns[0]

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Return(self, node: ast.Return) -> None:
        if self._returns:
            self._returns[-1].append(_eval(node.value, self._lookup) if node.value is not None else None)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        origin = _eval(node.value, self._lookup)
        module_level = len(self.scopes) == 1
        for target in node.targets:
            if isinstance(target, ast.Name) and origin is not None:
                self.scopes[-1][target.id] = origin
            if module_level and isinstance(target, ast.Name) and target.id == "PROMPT_SHA256":
                self._pinned_prompts(node.value)
        if module_level:
            self._add(origin, read=False)
        self.generic_visit(node)

    def _pinned_prompts(self, node: ast.AST) -> None:
        if not isinstance(node, ast.Dict):
            return
        base = self._lookup("PROMPTS")
        if base is None:
            return
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self._add(base.join(key.value), read=True)

    def visit_Call(self, node: ast.Call) -> None:
        origin = None
        if isinstance(node.func, ast.Attribute) and node.func.attr in _READ_TEXT:
            origin = _eval(node.func.value, self._lookup)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "load" and node.args:
            arg = node.args[0]
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id == "str"
                and arg.args
            ):
                arg = arg.args[0]
            origin = _eval(arg, self._lookup)
        elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            origin = _eval(node.args[0], self._lookup)
        if origin is not None:
            self._add(origin, read=True)
        self.generic_visit(node)


def discover_runtime_files(sidecar_dir: Path) -> tuple[set[str], set[str], list[str]]:
    """Return (files, dynamic directories, unclassified install-root reads).

    ``files`` are paths relative to the install root. ``dynamic directories`` are
    folders a module reads through a variable (the Glimpse prompt directory).
    """
    files: set[str] = set(ALWAYS_INSTALL_FILES)
    dynamic: set[str] = set()
    unclassified: list[str] = []
    for path in sorted(Path(sidecar_dir).glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scan = _Scan(path.name)
        scan.visit(tree)
        files |= scan.files
        dynamic |= scan.dynamic_dirs
        unclassified.extend(scan.unclassified)
    return files, dynamic, unclassified


def required_runtime_files(sidecar_dir: Path) -> list[str]:
    files, _dynamic, unclassified = discover_runtime_files(sidecar_dir)
    if unclassified:
        raise RuntimeError("unclassified install-root read: " + "; ".join(unclassified))
    return sorted(files)


def stage_runtime_layout(repo: Path, dest: Path) -> Path:
    """The slice of the Windows zip the sidecar reads.

    Same two copies as ``.github/workflows/release.yml``: the ``sidecar/`` tree
    and ``contracts/``. The exe, ffmpeg, and qortime checkout are separate steps.
    """
    repo, dest = Path(repo), Path(dest)
    shutil.copytree(
        repo / "sidecar", dest / "sidecar",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(repo / "contracts", dest / "contracts")
    return dest


def load_signing_props(install_root: Path, section: str = "result") -> frozenset:
    """Import the staged ``qorcap_match.py`` and load the receipt schema it ships with."""
    script = Path(install_root) / "sidecar" / "qorcap_match.py"
    name = "qorcap_match_install_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"QorCap cannot sign this match: {script} could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._closed_props(section)


def check_install(install_root: Path) -> list[str]:
    """Problems that should fail the release. Empty means the signing schema loads."""
    root = Path(install_root)
    sidecar = root / "sidecar"
    if not (sidecar / "qorcap_match.py").is_file():
        return ["sidecar/qorcap_match.py"]
    try:
        files, dynamic, unclassified = discover_runtime_files(sidecar)
    except (OSError, SyntaxError) as e:
        return [f"could not audit sidecar ({e})"]
    problems = list(unclassified)
    for rel in sorted(files):
        if not (root / rel).is_file():
            problems.append(rel)
    for rel in sorted(dynamic):
        folder = root / rel
        if not folder.is_dir() or not any(folder.iterdir()):
            problems.append(rel + "/")
    if problems:
        return problems
    try:
        props = load_signing_props(root)
    except SystemExit as e:
        return [str(e)]
    if "home_score" not in props or "away_score" not in props:
        return ["receipt schema did not expose result.home_score and result.away_score"]
    return []


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Fail if a release folder is missing a file the sidecar reads")
    ap.add_argument("--install-root", required=True, help="unzipped release folder (the directory that contains sidecar/)")
    args = ap.parse_args(argv)
    problems = check_install(Path(args.install_root))
    if problems:
        for item in problems:
            if item.startswith("QorCap cannot") or item.startswith("could not") or item.startswith("unclassified") or "install-root read" in item:
                print(item, file=sys.stderr)
            else:
                print(f"release missing runtime file: {item}", file=sys.stderr)
        return 1
    print("release runtime files ok; receipt schema loaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
