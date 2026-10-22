"""One post-game session in the lab sandbox."""
import json, os
from collections import Counter
from . import harness as H, loop, LAB_VERSION, TAG
from .ledger import HashLedger
from .bank import Bank
from .budget import Budget
from .safety import check_disk, check_not_recording, check_write
from . import report

class Lab:
    def __init__(self, root, initial, mode="replay", assume_approved=False):
        os.makedirs(root, exist_ok=True)
        self.root = root
        self.mode = mode
        self.assume_approved = assume_approved           # SIMULATION ONLY: stands in for Con's approval
        self.lessons = HashLedger(check_write(os.path.join(root, "lesson_ledger.jsonl"), root))
        self.spend = HashLedger(check_write(os.path.join(root, "spend_ledger.jsonl"), root))
        self.bank = Bank(check_write(os.path.join(root, "heldout_bank.json"), root))
        self.reg = H.Registry(check_write(os.path.join(root, "harness_registry.json"), root), initial)
        self.frames, self.labels = {}, {}

    def library(self, layout, game="madden27"):
        """Active lessons keyed by (game, layout)."""
        out = []
        for e in self.lessons.rows:
            b = e["body"]
            if e["kind"] == "lesson" and b["key"] == {"game": game, "layout": layout} and b["status"] != "rejected":
                out.append(b)
        return out

    def replay_digest(self, champ, cand, ids, pool):
        """SHA-256 over the exact (frame, label, champion reply, candidate reply) tuples the decision used, so an
        auditor holding the same cached replies can recompute the lesson's evidence."""
        from .jcs import sha
        rows = [[i, self.labels[i].get("label"), H.run(champ, self.frames[i], pool)[0],
                 H.run(cand, self.frames[i], pool)[0]] for i in sorted(ids)]
        return sha(rows)

    def run_game(self, g, pool, versions):
        check_disk(self.root)
        check_not_recording(g.get("match_dir"))
        budget = Budget(self.spend, mode=self.mode)
        champ = self.reg.champion
        self.frames.update(g["frames"]); self.labels.update(g["labels"])
        fl = [{"id": i, "layout": f["layout"]} for i, f in g["frames"].items()]
        bank_ids, tune_ids = self.bank.add_game(g["match"], fl, g["labels"])
        layouts = sorted({f["layout"] for f in g["frames"].values()}) or [g["layout"]]
        rec = {"match": g["match"], "layout": g["layout"], "chain": g["chain"], "champion": H.name(champ),
               "frames_cached": len(g["frames"]), "labelled": sum(1 for x in g["labels"].values() if x.get("label")),
               "grades": dict(Counter(x.get("grade") for x in g["labels"].values() if x.get("label"))),
               "bank_added": len(bank_ids), "tune": len(tune_ids), "bank_stats": None, "candidates": [],
               "lesson": None, "pending": None, "decision": None}
        # champion's own reads on this game (live watcher replies, already paid for during the game)
        gr = loop.grade(champ, list(g["frames"]), self.frames, self.labels, pool)
        rec["champion_on_game"] = {k: len(v) for k, v in gr.items() if k != "cost"}
        if not g["frames"]:
            rec["note"] = "no cached Glimpse reads for this game: labels only (they wait for reads)"
        hard = loop.hard_frames(champ, tune_ids, self.frames, self.labels, pool)
        rec["hard"] = {k: sorted(v) for k, v in hard.items()}
        review = []
        for i, f in g["frames"].items():                 # ungradable disagreements -> optional human review
            r, _ = H.run(champ, f, pool)
            if (not g["labels"][i].get("label") and r and r["away"] is not None and f.get("lock_in_force")
                    and (r["away"], r["home"]) != tuple(f["lock_in_force"])):
                review.append(i)
        rec["review_queue"] = sorted(review)[:20]
        n_hard = len(set().union(*hard.values())) if hard else 0
        best = None
        if n_hard:
            for kind, cand in loop.propose(champ, layouts, versions, sum((self.library(l) for l in layouts), [])):
                pr = loop.probe(champ, cand, tune_ids, self.frames, self.labels, pool)
                budget.authorise(len(tune_ids), H.version_for(cand, {"layout": layouts[0]}))
                rec["candidates"].append({"kind": kind, "harness": H.name(cand), **pr})
                if pr["verdict"] == "TUNE_PASS" and (best is None or pr["score"] > best[2]["score"]):
                    best = (kind, cand, pr)
        if best:
            kind, cand, pr = best
            gate = loop.bank_gate(champ, cand, self.bank, self.frames, self.labels, pool, layouts)
            rec["bank_gate"] = {"harness": H.name(cand), **gate}
            failure = max(("wrong", "unsure", "disagree_reader"), key=lambda k: len(hard[k]))
            refs = sorted({self.labels[i]["basis"] for i in set().union(*hard.values())
                           if self.labels.get(i, {}).get("label")})[:10]
            lesson = {"key": {"game": "madden27", "layout": g["layout"]},
                      "cue": f"{g['layout']}: champion {failure} on {len(hard[failure])} tune frame(s)",
                      "action": {"routes": {k: v for k, v in cand["routes"].items() if champ["routes"].get(k) != v},
                                 "checks": sorted(set(cand["checks"]) - set(champ["checks"]))},
                      "evidence": {"tune": pr, "bank": gate["verdict"], "match": g["match"]},
                      "receipt_refs": {"chain_head": g["chain"]["head"], "label_basis": refs,
                                       "receipt_sha256": g.get("receipt_sha256")},
                      "replay_digest": self.replay_digest(champ, cand, tune_ids, pool),
                      "harness_sha256": cand["sha256"], "status": "candidate",
                      "confidence": "low" if pr["d_right"] + abs(pr["d_wrong"]) < 3 else "medium",
                      "tag": TAG}
            veto_on = os.environ.get("QORCAP_GLIMPSE_TD_VETO") == "1"
            if gate["verdict"].startswith("PASS") and veto_on:
                lesson["status"] = "held_td_veto_enforcing"   # Glimpse can delay locks in that mode: no proposals
                rec["pending"] = None
            elif gate["verdict"].startswith("PASS"):
                self.reg.propose_pending(cand, {"bank": gate, "tune": pr, "match": g["match"]})
                lesson["status"] = "pending_approval"
                rec["pending"] = H.name(cand)
                if self.assume_approved:
                    self.reg.approve(cand["sha256"], approver="SIMULATED (replay)")
                    lesson["status"] = "approved_simulated"
                    rec["decision"] = "approved (simulated)"
            self.lessons.append("lesson", lesson)
            rec["lesson"] = lesson
        else:
            self.lessons.append("no_entry", {"match": g["match"], "layout": g["layout"], "hard_frames": n_hard,
                                              "reason": "no candidate passed the tune check" if n_hard else
                                              "no hard frames", "receipt_refs": {"chain_head": g["chain"]["head"]}})
        rec["bank_stats"] = self.bank.stats()
        rec["spend_usd"] = 0.0 if self.mode == "replay" else budget.game_spent
        rec["would_cost_live_usd"] = round(budget.would_cost, 4)
        self.spend.append("spend", {"match": g["match"], "usd": rec["spend_usd"], "mode": self.mode,
                                    "would_cost_usd": rec["would_cost_live_usd"]})
        self.bank.save(); self.reg.save()
        rec["lab_version"] = LAB_VERSION
        path = check_write(os.path.join(self.root, "games", g["match"], "REPORT.md"), self.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(report.render(rec))
        json.dump(rec, open(path.replace("REPORT.md", "record.json"), "w"), indent=1, default=list)
        return rec
