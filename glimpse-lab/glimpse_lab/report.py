def render(r):
    L = [f"# Glimpse learned: {r['match']}  (ADVISORY_AI, not a score lock)", ""]
    ch = r["champion_on_game"]
    L.append(f"- Layout: **{r['layout']}**. Evidence: chain verified = {r['chain']['verified']}"
             f" ({r['chain']['n']} entries). Free labels: {r['labelled']} frames {r['grades']}.")
    if r.get("note"):
        L.append(f"- {r['note']}.")
    L.append(f"- Live Glimpse on this game ({r['champion']}): right {ch.get('right',0)}, wrong {ch.get('wrong',0)},"
             f" unsure {ch.get('abstain',0)} (of labelled frames).")
    hard = r["hard"]
    L.append(f"- Got wrong: {', '.join(hard['wrong']) or 'nothing'}; unsure: {len(hard['unsure'])};"
             f" disagreed with the reader: {len(hard['disagree_reader'])}.")
    if r["lesson"]:
        les = r["lesson"]
        L.append(f"- Lesson added: *{les['cue']}* -> `{les['action']}` (status {les['status']}).")
    else:
        L.append("- Lesson added: none (NO_ENTRY).")
    if r.get("bank_gate"):
        bg = r["bank_gate"]
        L.append(f"- Held-out check for `{bg['harness']}`: {bg['verdict']} " +
                 (f"(n={bg['n']}, right {bg.get('champ_right')}->{bg.get('cand_right')}, "
                  f"new wrong {len(bg.get('new_wrong', []))}, p={bg.get('p_mcnemar')})" if "champ_right" in bg else f"(n={bg['n']})"))
    L.append(f"- Waiting for your approval: {r['pending'] or 'nothing'}" + (f" ({r['decision']})" if r["decision"] else ""))
    L.append(f"- Spend: ${r['spend_usd']:.2f} (replay); a live lab run would have cost about ${r['would_cost_live_usd']:.3f}.")
    L.append(f"- Held-out bank now: {r['bank_stats']}")
    return "\n".join(L) + "\n"
