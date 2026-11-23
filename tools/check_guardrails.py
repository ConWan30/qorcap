#!/usr/bin/env python3
"""Zero-dependency guardrails for ConWan30/qorcap (Integration #5, T-0030).

Four fail-closed checks, stdlib only, deterministic (sorted walks, sorted output):

1. Frozen surface: no file under ``contracts/`` and no edit to ``src/seal.rs``
   (or ``src/bundle.rs`` -- frozen per the MM-QOR charter, included here even
   though T-0030 names only ``contracts/`` and ``src/seal.rs``) may differ from
   ``origin/main``, be staged, be modified in the working tree, or be newly
   added under ``contracts/`` -- unless ``QORCAP_ALLOW_CONTRACT_EDIT=1`` is
   set, which must only ever be done with Con's explicit approval recorded
   in the task/log. The override skips ONLY this check; the other three
   still run.
2. LF line endings: no ``\\r`` byte in any text file under ``contracts/``,
   ``schemas/``, or ``tests/fixtures/hand_timeline/``. Binary files
   (``.bin`` and friends, or any file whose first 8 KiB contains NUL) are
   skipped: the hand fixtures are byte-pinned binaries (``.gitattributes``
   marks ``tests/fixtures/hand_timeline/** -text``), so a literal ``\\r``
   scan would fail on current main.
3. Listen-only HID: no ``HidDevice::write``, ``send_feature_report``,
   ``get_feature_report``, or ``.write(...)`` call on a HID handle in
   ``src/hand/**/*.rs`` or ``src/hid.rs``. Comments and string literals are
   stripped before scanning, so the existing negative callout in
   ``src/hid.rs`` ("Do not call write() or send_feature_report.") does not
   flag. ``.write(true)`` / ``.write(false)`` are the ``OpenOptions``
   builder (file-open mode, e.g. ``src/hand/mod.rs`` and
   ``src/hand/timeline.rs`` on current main) and are NOT HID writes, so
   they are excluded by the pattern.
4. Banned wording: none of ``anti-cheat``, ``attested``, ``proof-of-human``,
   ``who-held``, ``verified skill``, ``proven``, ``proves``, ``is Con`` may
   appear in ``src/hand/``, ``schemas/hand_timeline/``, or ``docs/HAND_*.md``
   -- except an explicit negative-rule callout: a line that names the term
   AND one of the negations ``never``, ``banned``, ``forbidden``,
   ``must not``, ``do not``, ``don't``, ``not allowed``, ``prohibited``.

Usage:
    python tools/check_guardrails.py [--root PATH]
    python tools/check_guardrails.py --self-test [--root PATH]

Exit 0 with ``ALL CHECKS OK`` on a clean tree, non-zero with one
``GUARDRAIL FAIL:`` line per violation otherwise. ``--self-test`` builds
synthetic violations of all four rules in a temp dir (plus the clean and
callout cases that must pass) and exits 0 only if every case behaves.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# --- Rule configuration ----------------------------------------------------

FROZEN_PREFIXES = ("contracts/",)
# T-0030 names contracts/ and src/seal.rs; src/bundle.rs is added because
# the MM-QOR charter freezes it too (receipt.v0 lives at contracts/receipt.v0.json).
FROZEN_FILES = ("src/seal.rs", "src/bundle.rs")
FROZEN_OVERRIDE_ENV = "QORCAP_ALLOW_CONTRACT_EDIT"

LF_SCOPES = ("contracts", "schemas", "tests/fixtures/hand_timeline")

HID_FILES = ("src/hid.rs",)
HID_DIRS = ("src/hand",)

BANNED_SCOPES = (
    ("src/hand", "*.rs"),
    ("schemas/hand_timeline", None),
    ("docs", "HAND_*.md"),
)

BANNED = (
    ("anti-cheat", re.compile(r"anti-cheat", re.IGNORECASE)),
    ("attested", re.compile(r"\battested\b", re.IGNORECASE)),
    ("proof-of-human", re.compile(r"proof-of-human", re.IGNORECASE)),
    ("who-held", re.compile(r"who-held", re.IGNORECASE)),
    ("verified skill", re.compile(r"verified skill", re.IGNORECASE)),
    ("proven", re.compile(r"\bproven\b", re.IGNORECASE)),
    ("proves", re.compile(r"\bproves\b", re.IGNORECASE)),
    ("is Con", re.compile(r"\bis con\b", re.IGNORECASE)),
)

# A line counts as an explicit negative-rule callout only if it names the
# banned term AND one of these negations. Anything else containing a banned
# term is a violation, even in a comment.
NEGATIONS = re.compile(
    r"\b(never|banned|forbidden|must not|do not|don't|not allowed|prohibited)\b",
    re.IGNORECASE,
)

# HID write patterns, applied to code with comments and string literals
# stripped. The .write( pattern excludes the OpenOptions builder form
# .write(true) / .write(false), which is file-open mode, not a HID write.
HID_PATTERNS = (
    ("HidDevice::write", re.compile(r"HidDevice::write")),
    ("send_feature_report", re.compile(r"send_feature_report")),
    ("get_feature_report", re.compile(r"get_feature_report")),
    (".write(", re.compile(r"\.write\s*\((?!\s*(?:true|false)\s*\))")),
)

BINARY_EXTENSIONS = {
    ".bin", ".png", ".jpg", ".jpeg", ".gif", ".gz", ".xz", ".zip",
    ".mp4", ".ico", ".dll", ".exe", ".pdb", ".so", ".dylib",
}


# --- Small helpers ---------------------------------------------------------

def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return False


def _iter_files(root: Path, scope: str):
    base = root / scope
    if not base.exists():
        return
    if base.is_file():
        yield base
        return
    for path in sorted(base.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            yield path


def _strip_rust_code(text: str) -> str:
    """Remove // line comments, /* */ block comments, string literals and
    char literals, keeping newlines so line numbers survive. This is a
    small lexical scan, not a parser: it is only used to keep negative
    callouts and prose from flagging the HID guard, and it never hides a
    real call (calls live in code, not in comments or strings)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "//":
            while i < n and text[i] != "\n":
                i += 1
        elif two == "/*":
            depth = 1
            i += 2
            while i < n and depth:
                if text[i:i + 2] == "/*":
                    depth += 1
                    i += 2
                elif text[i:i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    if text[i] == "\n":
                        out.append("\n")
                    i += 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 2
                else:
                    if text[i] == "\n":
                        out.append("\n")
                    i += 1
            i += 1  # closing quote
        elif c == "'":
            # Char literal (e.g. 'x', '\n'). A lifetime ('a) has no closing
            # quote on the same line; treat a lone ' as code, not a literal.
            j = i + 1
            closed = False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    closed = True
                    break
                j += 1
            if closed and j - i <= 4:
                i = j + 1
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --- The four guards (each returns a sorted list of violation strings) ---

def check_lf(root: Path) -> list[str]:
    bad: list[str] = []
    for scope in LF_SCOPES:
        for path in _iter_files(root, scope):
            if _is_binary(path):
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                bad.append(f"lf: cannot read {_rel(root, path)}: {exc}")
                continue
            if b"\r" in data:
                bad.append(f"lf: carriage return in {_rel(root, path)}")
    return sorted(bad)


def check_hid(root: Path) -> list[str]:
    bad: list[str] = []
    targets: list[Path] = []
    for name in HID_FILES:
        p = root / name
        if p.is_file():
            targets.append(p)
    for scope in HID_DIRS:
        targets.extend(
            p for p in _iter_files(root, scope) if p.suffix == ".rs"
        )
    for path in sorted(targets):
        try:
            code = _strip_rust_code(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            bad.append(f"hid: cannot read {_rel(root, path)}: {exc}")
            continue
        for lineno, line in enumerate(code.split("\n"), start=1):
            for label, rx in HID_PATTERNS:
                if rx.search(line):
                    bad.append(
                        f"hid: {label} in {_rel(root, path)}:{lineno}"
                    )
    return sorted(bad)


def check_banned(root: Path) -> list[str]:
    bad: list[str] = []
    for scope, glob in BANNED_SCOPES:
        for path in _iter_files(root, scope):
            if glob is not None and not path.match(glob):
                continue
            if _is_binary(path):
                continue
            try:
                lines = path.read_text(encoding="utf-8").split("\n")
            except (OSError, UnicodeDecodeError) as exc:
                bad.append(f"banned: cannot read {_rel(root, path)}: {exc}")
                continue
            for lineno, line in enumerate(lines, start=1):
                if NEGATIONS.search(line):
                    continue  # explicit negative-rule callout
                for label, rx in BANNED:
                    if rx.search(line):
                        bad.append(
                            f"banned: '{label}' in "
                            f"{_rel(root, path)}:{lineno}"
                        )
    return sorted(bad)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=60,
    )


def _resolve_base(root: Path) -> str | None:
    """Return 'origin/main' if resolvable, fetching it shallowly once if
    needed (CI checks out with fetch-depth 1, so the ref is absent until
    fetched). Return None if it still cannot be resolved."""
    if _git(root, "rev-parse", "--verify", "--quiet", "origin/main").returncode == 0:
        return "origin/main"
    _git(root, "fetch", "--depth", "1", "origin", "main")
    if _git(root, "rev-parse", "--verify", "--quiet", "origin/main").returncode == 0:
        return "origin/main"
    return None


def check_frozen(root: Path) -> list[str]:
    if os.environ.get(FROZEN_OVERRIDE_ENV) == "1":
        print(
            f"WARNING: {FROZEN_OVERRIDE_ENV}=1 set -- frozen-surface check "
            "skipped. This override must only be used with Con's explicit "
            "approval recorded in the task/log.",
            file=sys.stderr,
        )
        return []
    base = _resolve_base(root)
    if base is None:
        return [
            "frozen: cannot resolve origin/main (run 'git fetch origin main' "
            "and retry; refusing to pass the frozen-surface check blind)"
        ]
    changed: set[str] = set()
    # Committed PR diff vs main (three-dot: merge-base to HEAD).
    for args in (
        ("diff", "--name-only", f"{base}...HEAD"),
        ("diff", "--cached", "--name-only"),          # staged, pre-commit
        ("diff", "--name-only"),                     # tracked working-tree mods
        ("ls-files", "--others", "--exclude-standard"),  # untracked new files
    ):
        proc = _git(root, *args)
        if proc.returncode != 0:
            return [f"frozen: git {' '.join(args)} failed: {proc.stderr.strip()}"]
        changed.update(p for p in proc.stdout.splitlines() if p.strip())
    bad = sorted(
        p for p in changed
        if p in FROZEN_FILES or any(p.startswith(pfx) for pfx in FROZEN_PREFIXES)
    )
    return [f"frozen: modified frozen file {p}" for p in bad]


# --- Self-test -------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_ok(root: Path, *args: str) -> None:
    proc = _git(root, *args)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")


def _self_test() -> int:
    """Build synthetic violations of all four rules in a temp dir and
    confirm each is caught, and that the clean / callout cases pass."""
    failures: list[str] = []

    def expect(name: str, condition: bool) -> None:
        print(f"  {'PASS' if condition else 'FAIL'} {name}")
        if not condition:
            failures.append(name)

    print("self-test: frozen surface")
    with tempfile.TemporaryDirectory(prefix="qorcap-guardrails-") as td:
        repo = Path(td)
        _git_ok(repo, "init", "-q")
        _git_ok(repo, "config", "user.email", "self-test@example.invalid")
        _git_ok(repo, "config", "user.name", "guardrails self-test")
        _write(repo / "contracts" / "keep.txt", "pinned\n")
        _write(repo / "src" / "seal.rs", "// seal\n")
        _git_ok(repo, "add", ".")
        _git_ok(repo, "commit", "-qm", "base")
        # Stand in for origin/main (no remote in a temp repo): the frozen
        # check resolves its base via this ref, exactly as it does after
        # `git fetch --depth 1 origin main` in CI.
        _git_ok(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        expect("clean tree passes", check_frozen(repo) == [])
        _write(repo / "contracts" / "keep.txt", "tampered\n")
        expect("modified contracts/ file is caught",
               any("contracts/keep.txt" in v for v in check_frozen(repo)))
        _write(repo / "contracts" / "keep.txt", "pinned\n")
        _write(repo / "contracts" / "new.txt", "new frozen file\n")
        _git_ok(repo, "add", "contracts/new.txt")
        expect("staged new contracts/ file is caught",
               any("contracts/new.txt" in v for v in check_frozen(repo)))
        _git_ok(repo, "reset", "-q")
        (repo / "contracts" / "new.txt").unlink()
        _write(repo / "src" / "seal.rs", "// seal changed\n")
        expect("modified src/seal.rs is caught",
               any("src/seal.rs" in v for v in check_frozen(repo)))

    print("self-test: LF line endings")
    with tempfile.TemporaryDirectory(prefix="qorcap-guardrails-") as td:
        root = Path(td)
        _write(root / "contracts" / "a.txt", "line\r\n")
        expect("CRLF file is caught",
               any("contracts/a.txt" in v for v in check_lf(root)))
        (root / "contracts" / "a.txt").write_bytes(b"line\n")
        expect("LF file passes", check_lf(root) == [])
        (root / "tests" / "fixtures" / "hand_timeline").mkdir(parents=True)
        (root / "tests" / "fixtures" / "hand_timeline" / "hand_raw.v0.bin"
         ).write_bytes(bytes(range(256)) * 4)  # binary, contains 0x0D
        expect("binary fixture with 0x0D bytes is skipped",
               check_lf(root) == [])

    print("self-test: listen-only HID")
    with tempfile.TemporaryDirectory(prefix="qorcap-guardrails-") as td:
        root = Path(td)
        hand = root / "src" / "hand"
        _write(hand / "ok.rs",
               "let f = OpenOptions::new().create_new(true).write(true)"
               ".open(&probe)?;\n")
        _write(root / "src" / "hid.rs",
               "// Input reports only. Do not call write() or "
               "send_feature_report().\n")
        expect("OpenOptions .write(true) and comment callout pass",
               check_hid(root) == [])
        _write(hand / "bad.rs", "let n = dev.write(&buf)?;\n")
        expect("device .write(&buf) is caught",
               any("bad.rs" in v for v in check_hid(root)))
        (hand / "bad.rs").unlink()
        _write(hand / "bad.rs",
               "let r = device.send_feature_report(&[0u8; 64])?;\n")
        expect("send_feature_report in code is caught",
               any("bad.rs" in v for v in check_hid(root)))
        _write(hand / "bad.rs", "impl HidDevice::write for Dev {}\n")
        expect("HidDevice::write is caught",
               any("bad.rs" in v for v in check_hid(root)))

    print("self-test: banned wording")
    with tempfile.TemporaryDirectory(prefix="qorcap-guardrails-") as td:
        root = Path(td)
        _write(root / "docs" / "HAND_TEST.md",
               "Never use anti-cheat wording in this project.\n")
        expect("negative-rule callout passes", check_banned(root) == [])
        _write(root / "docs" / "HAND_TEST.md",
               "This is anti-cheat software.\n")
        expect("real banned term is caught",
               any("HAND_TEST.md" in v for v in check_banned(root)))
        _write(root / "docs" / "HAND_TEST.md", "The result was proven.\n")
        expect("'proven' is caught",
               any("HAND_TEST.md" in v for v in check_banned(root)))
        _write(root / "docs" / "HAND_TEST.md", "The result was measured.\n")
        expect("clean wording passes", check_banned(root) == [])

    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} case(s): "
              + ", ".join(failures), file=sys.stderr)
        return 1
    print("SELF-TEST ALL CHECKS OK")
    return 0


# --- Entry point -----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=None,
                        help="repo root (default: parent of tools/)")
    parser.add_argument("--self-test", action="store_true",
                        help="run synthetic violation checks and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    root = args.root or Path(__file__).resolve().parent.parent
    if not (root / ".git").exists() and not (root / "contracts").is_dir():
        print(f"guardrails: {root} does not look like the qorcap repo root",
              file=sys.stderr)
        return 1
    violations: list[str] = []
    violations.extend(check_frozen(root))
    violations.extend(check_lf(root))
    violations.extend(check_hid(root))
    violations.extend(check_banned(root))
    if violations:
        for violation in violations:
            print(f"GUARDRAIL FAIL: {violation}", file=sys.stderr)
        return 1
    print("ALL CHECKS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
