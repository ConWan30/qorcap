"""Harness versions, candidates and rollback. A harness is data: which prompt/crop version per layout plus
code-only checks. ROLE (observer-only, never a lock) is immutable and not part of what can be edited."""
import json, os, copy
from .jcs import sha
from .chain import legal_step

EDITABLE = ("base", "routes", "checks")          # task text / crop version, per-layout skill routes, checks
CHECKS = {}

def check(name):
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco

@check("lockguard")
def _lockguard(reply, frame):
    """Abstain on a score that is not a legal football step from the reader's lock in force (code-only)."""
    lp = frame.get("lock_in_force")
    if reply and lp and reply["away"] is not None and reply["home"] is not None:
        if not legal_step(lp[0], lp[1], reply["away"], reply["home"]):
            return dict(reply, away=None, home=None, abstain="lockguard")
    return reply

@check("stats_never_score")
def _stats(reply, frame):
    if reply and reply.get("region") in ("STATS_OVERLAY", "STATS"):
        return dict(reply, away=None, home=None)
    return reply

def make(base, routes=None, checks=()):
    h = {"base": base, "routes": dict(sorted((routes or {}).items())), "checks": sorted(checks)}
    h["sha256"] = sha({k: h[k] for k in EDITABLE})
    return h

def name(h):
    s = h["base"]
    if h["routes"]:
        s += "+" + ",".join(f"{k}->{v}" for k, v in h["routes"].items())
    if h["checks"]:
        s += "+" + "+".join(h["checks"])
    return s

def version_for(h, frame):
    return h["routes"].get(frame["layout"], h["base"])

def run(h, frame, pool):
    """Replay one frame. Returns (reply or None if no cached reply for that version, version used)."""
    v = version_for(h, frame)
    r = pool.get(v, {}).get(frame["id"])
    if r is None:
        return None, v
    for c in h["checks"]:
        r = CHECKS[c](r, frame)
    return r, v

class Registry:
    """champion pointer + full history; rollback = point back at any earlier approved version."""
    def __init__(self, path, initial):
        self.path = path
        if os.path.exists(path):
            self.s = json.load(open(path))
        else:
            self.s = {"versions": {initial["sha256"]: dict(initial, status="champion", note="initial")},
                      "champion": initial["sha256"], "history": [initial["sha256"]], "pending": []}

    def save(self):
        json.dump(self.s, open(self.path, "w"), indent=1, sort_keys=True)

    @property
    def champion(self):
        return self.s["versions"][self.s["champion"]]

    def propose_pending(self, h, evidence):
        v = dict(h, status="PENDING_APPROVAL", evidence=evidence)
        self.s["versions"][h["sha256"]] = v
        if h["sha256"] not in self.s["pending"]:
            self.s["pending"].append(h["sha256"])
        return v

    def approve(self, sha256, approver):
        """Only Con (approver) calls this; the lab never does on its own."""
        if sha256 not in self.s["pending"]:
            raise ValueError("not pending")
        self.s["versions"][self.s["champion"]]["status"] = "previous"
        self.s["versions"][sha256].update(status="champion", approved_by=approver)
        self.s["champion"] = sha256
        self.s["history"].append(sha256)
        self.s["pending"].remove(sha256)

    def rollback(self):
        if len(self.s["history"]) < 2:
            raise ValueError("nothing to roll back to")
        cur = self.s["history"].pop()
        self.s["versions"][cur]["status"] = "rolled_back"
        self.s["champion"] = self.s["history"][-1]
        self.s["versions"][self.s["champion"]]["status"] = "champion"
