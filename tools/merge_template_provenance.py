"""Copy per-game harvest folders into ``templates/`` and merge provenance rows.

Stops before writing anything if two rows share a path, or if that path is
already in the destination provenance or on disk.

    python tools/merge_template_provenance.py --dest templates --from out-102332 out-111640
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"refusing: unreadable provenance {path} ({exc}). nothing written.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
        raise SystemExit(f"refusing: {path} has no templates list. nothing written.")
    return data


def merge(dest: Path, outs: list[Path]) -> int:
    dest_prov = dest / "provenance.json"
    if dest_prov.is_file():
        base = _load(dest_prov)
    else:
        base = {
            "schema": "qorcap-template-provenance/v0",
            "harvest_script": "tools/harvest_templates.py",
            "harvest_version": "3",
            "templates": [],
        }
    seen: dict[str, str] = {}
    for row in base["templates"]:
        if not isinstance(row, dict):
            raise SystemExit("refusing: destination provenance has a non-object row. nothing written.")
        rel = str(row.get("path") or "")
        if rel in seen:
            raise SystemExit(f"refusing: duplicate path {rel} already in {dest_prov}. nothing written.")
        seen[rel] = "destination"
    incoming: list[dict] = []
    blobs: list[tuple[Path, Path]] = []
    for out in outs:
        doc = _load(out / "provenance.json")
        for row in doc["templates"]:
            if not isinstance(row, dict):
                raise SystemExit(f"refusing: {out} has a non-object row. nothing written.")
            rel = str(row.get("path") or "").replace("\\", "/")
            if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
                raise SystemExit(f"refusing: bad path {rel!r}. nothing written.")
            if rel in seen:
                raise SystemExit(
                    f"refusing: duplicate path {rel} ({seen[rel]} and {out}). nothing written."
                )
            src = out / rel
            if not src.is_file():
                raise SystemExit(f"refusing: {out} provenance names missing file {rel}. nothing written.")
            target = dest / rel
            if target.exists():
                raise SystemExit(f"refusing: {target} already exists. nothing written.")
            seen[rel] = str(out)
            row = dict(row)
            row["path"] = rel
            incoming.append(row)
            blobs.append((src, target))
    dest.mkdir(parents=True, exist_ok=True)
    for src, target in blobs:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
    base["templates"] = list(base["templates"]) + incoming
    dest_prov.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    return len(incoming)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description="Merge harvest output into templates/")
    ap.add_argument("--dest", type=Path, default=root / "templates")
    ap.add_argument("--from", dest="outs", type=Path, nargs="+", required=True)
    args = ap.parse_args(argv)
    try:
        count = merge(args.dest, args.outs)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(exc.code if isinstance(exc.code, str) else exc, file=sys.stderr)
            return 2
        raise
    print(f"merged {count} template rows into {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
