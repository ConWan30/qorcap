"""The per-game loop: label -> split -> find hard frames -> propose -> probe -> compare -> bank gate ->
(pending approval) -> distil <= 1 lesson -> report. Replay mode uses cached replies only ($0)."""
from . import harness as H, labels as L, chain as C
from .stats import mcnemar_exact
from .budget import MEAN_PER_CALL

MIN_BANK = 30
WRONG_WEIGHT = 5

def lock_in_force(segs_all, t):
    last = None
    for (tt, a, h, _) in segs_all:
        if tt <= t:
            last = (a, h)
    return last

def grade(h, ids, frames, labels, pool):
    out = {"right": set(), "wrong": set(), "abstain": set(), "other": set(), "missing": set(), "cost": 0.0}
    for i in ids:
        f = frames[i]
        r, v = H.run(h, f, pool)
        if r is None:
            out["missing"].add(i); continue
        out["cost"] += MEAN_PER_CALL.get(v, 0.002)
        lab = labels[i]
        if lab.get("label") is None:
            continue
        if lab["label"].get("no_scorebug"):           # reader saw no scorebug: any score is a fake score
            (out["wrong"] if r["away"] is not None else out["right"]).add(i)
            continue
        if r["away"] is None and r.get("region") not in (None, "SCOREBUG"):
            out["other"].add(i)      # said 'no scorebug here': labels say nothing about region, so neutral
        elif r["away"] is None or r["home"] is None or not r["valid"]:
            out["abstain"].add(i)
        elif (r["away"], r["home"]) == (lab["label"]["away"], lab["label"]["home"]):
            out["right"].add(i)
        else:
            out["wrong"].add(i)
    return out

def hard_frames(champ, ids, frames, labels, pool):
    g = grade(champ, ids, frames, labels, pool)
    dis = set()
    for i in ids:
        r, _ = H.run(champ, frames[i], pool)
        lp = frames[i].get("lock_in_force")
        if r and lp and r["away"] is not None and (r["away"], r["home"]) != tuple(lp):
            dis.add(i)
    return {"wrong": g["wrong"], "unsure": g["abstain"], "disagree_reader": dis}

def propose(champ, layouts, versions, lessons):
    cands = []
    for les in lessons:                                 # feed-forward: lessons for these layouts first
        a = les["action"]
        cands.append(("LESSON", H.make(champ["base"], {**champ["routes"], **a.get("routes", {})},
                                       sorted(set(champ["checks"]) | set(a.get("checks", []))))))
    for c in H.CHECKS:                                  # INV: code-only checks ("tools")
        if c not in champ["checks"]:
            cands.append(("INV", H.make(champ["base"], champ["routes"], champ["checks"] + [c])))
    for lay in layouts:                                 # DOM / per-layout skill: route this layout to a variant
        for v in versions:
            if v != H.version_for(champ, {"layout": lay}):
                cands.append(("DOM", H.make(champ["base"], {**champ["routes"], lay: v}, champ["checks"])))
    seen, out = set(), []
    for k, h in cands:
        if h["sha256"] not in seen and h["sha256"] != champ["sha256"]:
            seen.add(h["sha256"]); out.append((k, h))
    return out

def probe(champ, cand, tune_ids, frames, labels, pool):
    b = grade(champ, tune_ids, frames, labels, pool)
    c = grade(cand, tune_ids, frames, labels, pool)
    if c["missing"] - b["missing"]:
        return {"verdict": "NOT_RUN", "calls_needed": len(c["missing"] - b["missing"])}
    new_wrong = len(c["wrong"] - b["wrong"])
    dr = len(c["right"] - b["other"]) - len(b["right"])
    dw = len(c["wrong"]) - len(b["wrong"])
    cheaper = c["cost"] < b["cost"] - 1e-9
    ok = new_wrong == 0 and (dr > 0 or dw < 0 or (dr == 0 and dw == 0 and cheaper))
    return {"verdict": "TUNE_PASS" if ok else "TUNE_FAIL", "d_right": dr, "d_wrong": dw, "new_wrong": new_wrong,
            "cost_delta": round(c["cost"] - b["cost"], 6), "score": dr - WRONG_WEIGHT * dw}

def bank_gate(champ, cand, bank, frames, labels_all, pool, layouts):
    ids = [i for i, r in bank.active().items() if r["layout"] in layouts]
    both = [i for i in ids if H.run(champ, frames[i], pool)[0] is not None
            and H.run(cand, frames[i], pool)[0] is not None]
    if len(both) < MIN_BANK:
        return {"verdict": "INSUFFICIENT_BANK", "n": len(both), "need": MIN_BANK}
    if not bank.look(layouts):
        return {"verdict": "BANK_EXHAUSTED", "n": len(both)}
    b = grade(champ, both, frames, labels_all, pool)
    c = grade(cand, both, frames, labels_all, pool)
    new_wrong = sorted(c["wrong"] - b["wrong"])
    bb = len(c["right"] - b["right"] - b["other"]); cc = len(b["right"] - c["right"])
    p = mcnemar_exact(bb, cc)
    res = {"n": len(both), "champ_right": len(b["right"]), "cand_right": len(c["right"]),
           "champ_wrong": len(b["wrong"]), "cand_wrong": len(c["wrong"]), "new_wrong": new_wrong,
           "b_only_cand": bb, "c_only_champ": cc, "p_mcnemar": round(p, 5),
           "champ_cost": round(b["cost"], 5), "cand_cost": round(c["cost"], 5)}
    if new_wrong:
        res["verdict"] = "FAIL_NEW_WRONG"
    elif p < 0.05:
        res["verdict"] = "PASS_SIGNIFICANT"
    elif bb == cc and len(c["wrong"]) <= len(b["wrong"]) and c["cost"] < b["cost"]:
        res["verdict"] = "PASS_EQUAL_CHEAPER"
    else:
        res["verdict"] = "FAIL_NOT_SIGNIFICANT"
    return res
