#!/usr/bin/env python3
"""Write FROZEN-fusion-params.json from fusion_core.DEFAULT_PARAMS plus the hashes of the code and inputs it was
tuned with. Run once, after pilot tuning and before any heldout-2 run; print its sha256 for the report.

  python freeze_fusion.py --labels labels-con-20261113.json --v02-sha256 <hex> --tuning-note "..." --out FROZEN-fusion-params.json
"""
from __future__ import annotations

import argparse, datetime, hashlib, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as F  # noqa: E402

HERE = Path(__file__).resolve().parent
CODE = ("fusion_core.py", "fuse.py", "detect_person.py", "bodycue_core.py", "pins.py", "models.manifest.json")


def sha256(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True); ap.add_argument("--v02-sha256", required=True)
    ap.add_argument("--tuning-note", required=True); ap.add_argument("--frozen-at", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    fz = {"schema": "s2s-bodycue-fusion-frozen/v0",
          "frozen_at": a.frozen_at or datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
          "rule": "tuned on the 60-frame pilot only (labels-con-20261113.json); held-out-1 not used for tuning; "
                  "frozen before any heldout-2 frame exists; heldout-2 is scored once with these params",
          "params": F.DEFAULT_PARAMS,
          "tuned_on": {"labels_sha256": sha256(a.labels), "v02_script_sha256": a.v02_sha256},
          "code_sha256": {n: sha256(HERE / n) for n in CODE},
          "tuning_note": a.tuning_note}
    Path(a.out).write_text(json.dumps(fz, indent=1) + "\n", encoding="utf-8")
    print(sha256(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
