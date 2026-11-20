"""Growing, rotating held-out bank. A frame is assigned once, by hash, and never moves back to tuning.

* assignment: sha256(match_id|frame_id) -> 'bank' for BANK_PERMILLE of labelled frames, else 'tune'.
* rotation: at most CAP_PER_LAYOUT active bank frames per layout; the oldest games retire first.
  Retired frames stay 'burned' (never tuned on) and are kept for audit.
* exposure: every promotion check that reads the bank is counted (looks). After MAX_LOOKS checks the
  layout's bank must be refreshed with frames from newer games before the next check (limits adaptive
  overfitting to a reused holdout).
"""
import hashlib, json, os

BANK_PERMILLE = 400
CAP_PER_LAYOUT = 400
MAX_LOOKS = 20

def assign(match_id, frame_id):
    h = int(hashlib.sha256(f"{match_id}|{frame_id}".encode()).hexdigest()[:8], 16) % 1000
    return "bank" if h < BANK_PERMILLE else "tune"

class Bank:
    def __init__(self, path):
        self.path = path
        self.s = {"frames": {}, "looks": {}, "games": []}
        if os.path.exists(path):
            self.s = json.load(open(path))

    def save(self):
        tmp = self.path + ".tmp"
        json.dump(self.s, open(tmp, "w"), indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def add_game(self, match_id, frames, labels):
        """frames: [{'id','layout'}]; only labelled frames enter. Returns (bank_ids, tune_ids)."""
        bank, tune = [], []
        for f in frames:
            if not (labels.get(f["id"]) or {}).get("label"):
                continue
            if f["id"] in self.s["frames"] or f["id"] in self.s.get("tuned", {}):   # never re-assign
                continue
            split = assign(match_id, f["id"])
            if split == "tune":
                self.s.setdefault("tuned", {})[f["id"]] = match_id   # remembered so it can never enter the bank
                tune.append(f["id"]); continue
            if f["id"] in self.s.get("tuned", {}):
                continue
            if split == "bank":
                self.s["frames"][f["id"]] = {"match": match_id, "layout": f["layout"], "state": "active",
                                             "label": labels[f["id"]]["label"], "grade": labels[f["id"]]["grade"],
                                             "basis": labels[f["id"]]["basis"]}
                bank.append(f["id"])
            else:
                tune.append(f["id"])
        self.s["games"].append(match_id)
        self._rotate()
        return bank, tune

    def _rotate(self):
        by_layout = {}
        for fid, r in self.s["frames"].items():
            if r["state"] == "active":
                by_layout.setdefault(r["layout"], []).append(fid)
        order = {m: i for i, m in enumerate(self.s["games"])}
        for lay, ids in by_layout.items():
            if len(ids) > CAP_PER_LAYOUT:
                ids.sort(key=lambda i: order.get(self.s["frames"][i]["match"], 0))
                for fid in ids[: len(ids) - CAP_PER_LAYOUT]:
                    self.s["frames"][fid]["state"] = "retired"
            self.s["looks"].setdefault(lay, 0)

    def active(self, layout=None):
        return {i: r for i, r in self.s["frames"].items()
                if r["state"] == "active" and (layout is None or r["layout"] == layout)}

    def is_burned(self, frame_id):
        return frame_id in self.s["frames"]

    def look(self, layouts):
        """Record a promotion check. Returns False if any layout used up its look budget."""
        ok = True
        for lay in layouts:
            self.s["looks"][lay] = self.s["looks"].get(lay, 0) + 1
            ok &= self.s["looks"][lay] <= MAX_LOOKS
        return ok

    def stats(self):
        st = {}
        for r in self.s["frames"].values():
            k = (r["layout"], r["state"])
            st[f"{k[0]}:{k[1]}"] = st.get(f"{k[0]}:{k[1]}", 0) + 1
        return st
