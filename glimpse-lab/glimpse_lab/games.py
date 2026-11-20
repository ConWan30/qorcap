"""Replay inputs: the past games on the box, in the order Con played them, with their lock evidence.
Eye labels in /workspace/glimpse-harness2/data are loaded ONLY for the label audit, never by the loop."""
import datetime, json, re
from pathlib import Path
from . import chain as C, labels as L

DATA = Path("/workspace/glimpse-harness2/data")
LOCAL_LOCKS = Path("/workspace/glimpse-shadow/eval/local_locks.json")
NEGATIVE_LABELS = False   # reader 'scorebug_absent' disagreed with eye labels on 10/10 MNP frames: off by default
CDT = datetime.timezone(datetime.timedelta(hours=-5))

GAMES = [  # chronological
    dict(match="match-20261016-125430", layout="regular", lock_src="checkpoints",
         path="/workspace/vu/b125430/checkpoints.jsonl", timeline="/workspace/vu/b125430/timeline.jsonl",
         receipt="/workspace/vu/b125430/receipt.json", clock="t_ms", final=None),
    dict(match="match-20261017-002326", layout="primetime", lock_src="reader_locks_unsigned", final=None),
    dict(match="match-20261017-010703", layout="thursday_night", lock_src="reader_locks_unsigned",
         final={"away": 40, "home": 34, "confirmed": "operator"}),
    dict(match="match-20261017-021003", layout="primetime", lock_src="reader_locks_unsigned", final=None),
    dict(match="match-20261017-162055", layout="monday_night", lock_src="checkpoints",
         path="/workspace/glimpse-shadow/in/match-20261017-162055/score_checkpoints.jsonl",
         timeline="/workspace/glimpse-shadow/in/match-20261017-162055/timeline.offline.jsonl",
         clock="video_ms", final=None),
]

def eye_frames():
    fr = {}
    for s in ("tune", "heldout"):
        for l in open(DATA / f"{s}.jsonl"):
            f = json.loads(l); fr[f["id"]] = f
    return fr

def frame_time(f, clock):
    base = Path(f["src"]).name
    if clock == "video_ms":
        return int(re.search(r"f_(\d+)", base).group(1))
    hh, mm, ss = re.search(r"s_(\d{2})(\d{2})(\d{2})", base).groups()
    return int(datetime.datetime(2026, 10, 17, int(hh), int(mm), int(ss), tzinfo=CDT).timestamp() * 1000)

def region_obs_from_timeline(path, clock):
    absent, present = [], []
    for l in open(path):
        r = json.loads(l)
        t = r.get("video_ms") if clock == "video_ms" else r.get("t_ms")
        if t is None:
            continue
        if r.get("obs") == "scorebug_absent":
            absent.append((t, t + (r["until_ms"] - r["t_ms"])))
        elif "away" in r:
            present.append(t)
    def obs(t):
        if any(a <= t <= b for a, b in absent):
            return False
        if any(abs(p - t) <= 1500 for p in present):
            return True
        return None
    return obs

def build(game, eye):
    """Returns dict(frames, labels, segs, chain) for one game. Frames = the cached-read frames of that match."""
    g = dict(game)
    if g["lock_src"] == "checkpoints":
        ok, head, cps = C.verify_checkpoints(g["path"])
        locks = C.locks_from_checkpoints(cps, clock=g["clock"]) if ok else []
        g["chain"] = {"verified": ok, "head": head, "n": len(cps)}
        if g.get("receipt"):
            import hashlib
            g["receipt_sha256"] = hashlib.sha256(open(g["receipt"], "rb").read()).hexdigest()
        clock = g["clock"]
        region = region_obs_from_timeline(g["timeline"], clock)
    else:
        raw = json.load(open(LOCAL_LOCKS))[g["match"]]
        locks = [(int(t * 1000), a, h, f"reader-lock:{g['match']}:{int(t)}") for t, a, h in raw]
        g["chain"] = {"verified": False, "head": None, "n": len(locks)}
        clock, region = "t_ms", None
    segs = C.segments(locks)
    frames = {}
    for i, f in eye.items():
        if f["match"] != g["match"]:
            continue
        t = frame_time(f, clock)
        lif = None
        for (tt, a, h, _) in locks:
            if tt <= t:
                lif = (a, h)
        frames[i] = {"id": i, "t": t, "layout": f["layout"], "lock_in_force": lif, "match": g["match"]}
    labels = L.label_frames(list(frames.values()), segs, source=g["lock_src"],
                            chain_verified=g["chain"]["verified"], final=g["final"], region_obs=region)
    for i, lab in labels.items():
        if NEGATIVE_LABELS and lab.get("label") is None and lab.get("scorebug_present") is False:
            lab.update(label={"no_scorebug": True}, grade="B", basis="timeline:scorebug_absent",
                       source=g["lock_src"], independent_of_glimpse=True)
    g.update(frames=frames, labels=labels, segs=segs, locks=locks)
    return g

def label_audit(g, eye):
    """Free labels vs Con's eye labels (audit only)."""
    agree = dis = na = 0; bad = []
    for i, lab in g["labels"].items():
        L_ = lab.get("label"); gt = eye[i]["gt"]
        if not L_:
            continue
        if L_.get("no_scorebug"):
            ok = gt["region"] != "SCOREBUG"
        elif gt["region"] != "SCOREBUG":
            na += 1                      # score-in-force label on a frame with no scorebug: not comparable
            continue
        else:
            ok = (gt["away"], gt["home"]) == (L_["away"], L_["home"])
        agree += ok; dis += (not ok)
        if not ok:
            bad.append((i, L_, {k: gt[k] for k in ("region", "away", "home")}))
    return {"labelled": agree + dis + na, "not_comparable_no_scorebug": na, "agree_with_eye": agree, "disagree": dis, "examples": bad[:5]}


def label_capacity(g, step_ms=4000):
    """How many frames a live lab could label for free if it sampled the recording every step_ms."""
    if not g["locks"]:
        return {"sampled": 0, "labelled": 0}
    t0, t1 = g["locks"][0][0], g["locks"][-1][0] + 120_000
    grid = [{"id": f"grid{k}", "t": t} for k, t in enumerate(range(t0, t1, step_ms))]
    labs = L.label_frames(grid, g["segs"], source=g["lock_src"], chain_verified=g["chain"]["verified"],
                          final=g["final"])
    return {"sampled": len(grid), "labelled": sum(1 for x in labs.values() if x.get("label")),
            "step_s": step_ms // 1000}
