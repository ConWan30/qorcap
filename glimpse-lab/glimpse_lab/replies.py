"""Cached Glimpse replies already on the box (replay executor, $0). A reply is DATA to be graded,
never a label (see labels.assert_independent)."""
import csv, json, re
from pathlib import Path

EVAL = Path("/workspace/glimpse-eval")
SHADOW = Path("/workspace/glimpse-shadow/eval")
RUNS = Path("/workspace/glimpse-harness2/runs")

def _n(region, a, h, valid=True):
    return {"region": region, "away": a, "home": h, "valid": valid}

def load_pool():
    pool = {}
    def v0(path):
        d = {}
        for l in open(path):
            r = json.loads(l); g = r.get("glimpse") or {}
            d[r["item_id"]] = _n("SCOREBUG" if g.get("away_score") is not None else None,
                                 g.get("away_score"), g.get("home_score"), r.get("error") is None)
        return d
    pool["full_v0"] = v0(EVAL / "full.jsonl")
    pool["crop_v0"] = v0(EVAL / "crop_run.jsonl")
    d = {}
    for r in csv.DictReader(open(SHADOW / "offline_results.csv")):
        m = re.match(r"\((\w+), (\w+)\)", r["model_raw"] or "")
        a, h = (None, None) if not m else [None if x == "None" else int(x) for x in m.groups()]
        d[r["item_id"]] = _n(r["region"] or None, a, h, r["valid"] == "True")
    pool["crop_v1"] = d
    d = {}
    for fn in ("tune_base.jsonl", "heldout_base_run1.jsonl"):
        for l in open(RUNS / fn):
            r = json.loads(l); o = r.get("obj") or {}
            d[r["id"]] = _n(o.get("region"), o.get("away_score"), o.get("home_score"),
                            r.get("error") is None and not r.get("reject"))
    pool["watch_012"] = d
    return pool
