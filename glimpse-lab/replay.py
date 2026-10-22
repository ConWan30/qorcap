"""Replay past games in order through glimpse-lab, offline, $0 (cached replies only).
    (run with the OpenRouter key unset)  python3 replay.py --scenario forward|coldstart [--assume-approved]"""
import argparse, json, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from glimpse_lab import games as G, harness as H, replies
from glimpse_lab.session import Lab
from glimpse_lab.ledger import verify

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="forward", choices=["forward", "coldstart"])
    ap.add_argument("--assume-approved", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-bank", type=int, default=None, help="ILLUSTRATION ONLY: lower the bank-size floor")
    a = ap.parse_args()
    if a.min_bank is not None:
        from glimpse_lab import loop
        loop.MIN_BANK = a.min_bank
    root = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_out",
                                 a.scenario + ("_approved" if a.assume_approved else "")
                                 + (f"_minbank{a.min_bank}" if a.min_bank is not None else ""))
    shutil.rmtree(root, ignore_errors=True)
    init = H.make("watch_012" if a.scenario == "forward" else "full_v0")
    lab = Lab(root, init, assume_approved=a.assume_approved)
    pool = replies.load_pool(); eye = G.eye_frames(); versions = sorted(pool)
    summary = []
    for game in G.GAMES:
        g = G.build(game, eye)
        audit = G.label_audit(g, eye)
        rec = lab.run_game(g, pool, versions)
        rec["label_audit"] = audit
        rec["label_capacity"] = G.label_capacity(g)
        summary.append(rec)
        print(f"== {g['match']} [{g['layout']}] chain_verified={g['chain']['verified']} frames={rec['frames_cached']} "
              f"labelled={rec['labelled']} {rec['grades']} audit={audit['agree_with_eye']}/{audit['agree_with_eye']+audit['disagree']}"
              f" (n/a {audit['not_comparable_no_scorebug']})")
        print(f"   free-label capacity if sampled every 4 s: {rec['label_capacity']}; review queue: {len(rec['review_queue'])}")
        print(f"   champion={rec['champion']} on game {rec['champion_on_game']} hard={ {k: len(v) for k, v in rec['hard'].items()} }")
        for c in rec["candidates"]:
            print(f"   cand {c['kind']:6s} {c['harness']:45s} {c['verdict']:9s} "
                  + " ".join(f"{k}={c[k]}" for k in ("d_right", "d_wrong", "new_wrong", "calls_needed") if k in c))
        if rec.get("bank_gate"):
            print(f"   bank gate: {rec['bank_gate']}")
        print(f"   lesson: {rec['lesson']['cue'] + ' -> ' + json.dumps(rec['lesson']['action']) + ' [' + rec['lesson']['status'] + ']' if rec['lesson'] else 'NO_ENTRY'}")
        print(f"   pending: {rec['pending']}  bank: {rec['bank_stats']}  spend $0.00  live-equivalent ${rec['would_cost_live_usd']}")
    ok_l, n_l = verify(os.path.join(root, "lesson_ledger.jsonl"))
    ok_s, n_s = verify(os.path.join(root, "spend_ledger.jsonl"))
    print(f"lesson ledger verified={ok_l} entries={n_l}; spend ledger verified={ok_s} entries={n_s}")
    print(f"final champion: {H.name(lab.reg.champion)}; pending: {[H.name(lab.reg.s['versions'][s]) for s in lab.reg.s['pending']]}")
    json.dump(summary, open(os.path.join(root, "summary.json"), "w"), indent=1, default=list)

if __name__ == "__main__":
    main()
