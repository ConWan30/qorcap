# glimpse-lab: a post-match, observer-only self-improvement loop for Elorian Glimpse in QorCap

Status: **proposal + offline prototype**. The replay ran on games already on the box, at **$0**, using only cached
Glimpse replies. Nothing in QorCap, the MyTurn laptop or any main branch was changed. Times are CDT.
Novelty review: see `NOVELTY.md`. Its result is folded in at §9.

## 0. Rules this design obeys (Con's project rules), and how they are enforced
| rule | enforcement |
|---|---|
| Glimpse is OBSERVER ONLY: no locks, receipts, recording, referee or verification code; never turns a FAIL into gold | The lab does not import QorCap/QorTime scoring code and has no network imports (test `ObserverOnly`). It writes only under its own root (`safety.check_write`, tested). Its sole outputs are a harness *proposal* and a report, both tagged `ADVISORY_AI`. The live watcher still refuses any prompt whose sha256 isn't pinned in QorCap's own source, so a lab result can only reach the watcher through a QorCap PR that Con approves. |
| Its own box, after the match, never during capture, no slowdown, free-disk check first | `%LOCALAPPDATA%\QorCap\glimpse-lab\`, a separate process at below-normal priority. It refuses if `.qorcap-run\recording.lock` exists (`check_not_recording`) and refuses below 3 GiB free (`check_disk`). The lab folder itself is capped at 1 GiB, with crops pruned. |
| Every change gated by held-out; nothing promoted without Con; versioning and rollback | `bank_gate` (no new wrong + McNemar p<0.05, or equal at lower cost). A pass becomes `PENDING_APPROVAL`; `Registry.approve(sha, approver)` is never called by the lab; `Registry.rollback()` is available. Tested. |
| Budget: hard cap per game, $10/week shared, ledger, stop at cap | `budget.Budget` books the worst case before every live call: $0.25 per game. The weekly figure is the max of the local ledger and OpenRouter `usage_weekly`; the lab stops at $7.00 so the live watcher always keeps ≥$3 headroom. The spend ledger is hash-linked. Replay spends $0 (the live mode is not even enabled in this build). |

## 1. Free per-game labels (no Glimpse in the loop)
Sources, all already produced by QorCap for its own purposes:
1. **Hash-chained score locks.** Live `checkpoints.jsonl`, or finalize's `score_checkpoints.jsonl`. The lab re-verifies
   the chain (`hash = SHA-256(JCS(body))`, `frames_hash = SHA-256(JCS(qorcap))`) before using anything from it.
   A broken chain means no labels from that file. Verified on box: 162055 (100/100 entries) and 125430 (593/593).
2. **Confirmed final:** `final.offline.json` / `final.tail.json` / receipt `result.final_confirmed` (end-of-game menu).
3. **Legality:** a chain is split at every illegal step (a decrease, both sides changing, or a non-football delta).
4. **Timeline:** reader `score` rows and `scorebug_absent` observations, used for region signals.

Rules (`labels.py`):
* A frame gets the lock in force only inside a **trusted interval**: `[lock_i + 4 s, lock_{i+1} − 45 s]`, or `− 60 s`
  before a +7/+8 lock (a touchdown and the try merged, so the +6 was on screen first). The step `i → i+1` must be
  legal. The open interval after the last lock is labelled only if a confirmed final equals that last lock.
  The gap before an illegal step is never labelled.
* Grade **A** = chain-verified, single legal segment, end-menu final. **B** = everything else that is labelled.
  Every label carries `independent_of_glimpse: true`, its `source`, and its `basis` (the checkpoint hash it came from).
* **Circularity guard:** `assert_independent()` refuses any source naming glimpse/advisory/elorian. That means the
  Glimpse-assisted `labeled_candidates.csv` (162055) can never be used. Glimpse replies are graded data only.
* A label means "*if* a scorebug is on screen, it shows X". When Glimpse says "no scorebug here", that frame counts as
  neutral, because the labels say nothing about region.
* **Measured on the box (audit against Con's eye labels):**
  * First version: 20 s margins and bridging illegal steps. It produced **22 wrong labels out of 115** comparable frames (19%).
    The reader's locks lagged the screen by 22–32 s, and 13–14 → 20–21 merged several changes into one step.
    The reader's `scorebug_absent` disagreed with the eye on 10/10 Monday Night frames.
  * Strict version (shipped): **0 wrong labels out of 68**, with fewer frames labelled.
  * Negative ("no scorebug") labels are therefore **off by default** (`NEGATIVE_LABELS=False`).
  * **Coverage:** sampled every 4 s, the 5 games would yield **1,284 free labelled frames**: 106, 89, 608, 257 and 224.

## 2. The per-game loop
```
 match ends ──► QorCap verify (unchanged) ──► receipt.json / checkpoints / timeline / video.mp4      [AUTHORITY]
                                                   │  read-only, sha256-pinned
                                                   ▼
 ┌──────────────────────── glimpse-lab sandbox (%LOCALAPPDATA%\QorCap\glimpse-lab) ───────────────────┐
 │ 0 guards: not recording? ≥3 GiB free? budget left?                                                 │
 │ 1 LABEL  verify chain → trusted intervals → labels{A,B} (+ circularity guard)                      │
 │ 2 SPLIT  sha256(match|frame) → 40 % BANK (never tuned on) / 60 % TUNE; assignment is permanent    │
 │ 3 HARD   champion's reads on tune frames: wrong / unsure / disagree-with-reader                    │
 │            (watcher's advisory.jsonl replies are reused, never re-bought)                          │
 │ 4 PROPOSE (scoped to this layout) LESSON-first, then                                               │
 │            INV = code-only checks (lockguard, stats_never_score, …)                                │
 │            DOM = task text / crop / layout hint / per-layout skill route                           │
 │ 5 PROBE  candidates on the game's tune frames (cached replies or ≤ cap paid calls)                 │
 │ 6 COMPARE no new wrong AND (more right OR fewer wrong OR same at lower cost) → best one            │
 │ 7 BANK GATE on the held-out bank for that layout: no new wrong + McNemar p<0.05 or equal-cheaper   │
 │            (bank looks counted; refresh required after 20 looks)                                   │
 │ 8 DISTIL ≤1 lesson {key:(game,layout), cue, action, evidence, receipt_refs, replay_digest}         │
 │            → hash-linked LESSON LEDGER (or NO_ENTRY)                                               │
 │ 9 PENDING_APPROVAL (never auto) → "Glimpse learned" report                                        │
 └────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                   │ Con approves
                                                   ▼
            QorCap PR: new pinned prompt pack (e.g. CROP_TASK_v2.md + sha256) → watcher, still SHADOW ONLY
            rollback = revert the pin (registry keeps every version)
```
**What may be edited:** task text, crop box and scale, layout hints, per-layout skills (routes), and small code-only checks.
**Never editable:** ROLE (observer-only wording), the strict schema's "stats never yield a score", the watcher's
plausibility guard, its budgets, and anything in QorCap.
**Proposer:**
* Offline it is deterministic: cached variants plus a library of checks.
* Live it would be one text-only LLM call that sees the hard crops, the champion's replies and the *labels of tune
  frames only*, and returns a structured edit. It is validated like Harness²'s `tree.validate`: size cap, no ROLE
  edits, schema unchanged.
* As in Harness², `KEEP_BASE` / `NO_ENTRY` is a normal outcome.

## 3. The growing held-out bank
* Permanent hash assignment: 40% of a game's labelled frames go to the bank. A tuned frame is remembered and can never
  enter the bank. The prototype had a bug here; it was caught by the tests and fixed.
* Per-layout cap of 400 active frames; the oldest games retire first. Retired frames stay "burned" and are never tuned on.
* **Look budget:** each promotion check counts a look per layout. After 20 looks the layout's bank must be refreshed
  with newer games before the next check. This adapts the reusable-holdout idea (Dwork et al. 2015) to a cheap counter.
  Every look is in the ledger.
* **Promotion:** `new_wrong == 0` AND (exact one-sided McNemar p<0.05, which needs ≥5 net fixes with 0 losses,
  OR equal correct at lower cost). There is also a minimum of 30 bank frames with reads from both versions.

## 4. Layout and game awareness
* Lessons are keyed by `{game, layout}`. The layout comes from the receipt's `match.scorebug_profile` / watcher
  `--layout` (madden27: regular, primetime, thursday_night, monday_night, london).
* **A new layout is detected** (designed; the prototype only keys by the layout it is given) when the profile is unknown, or when the champion's reads on trusted-label frames drop
  below 90% for that game. It then gets its own key and an empty bank. Its first lessons are `candidate` only; no
  promotion is possible until its bank reaches the 30-frame minimum.
* **Other games** (e.g. the Call of Duty kill feed): same pipeline, keyed `{game: cod, layout: killfeed}`. It needs its own
  authority signal for labels (QorCap's CoD reader or end-of-match screen). Until that exists, no labels means no learning.
* **Controller input as corroboration, not truth:** QorCap's `inputs.jsonl` / `input_proof.json` can flag
  label intervals with implausible input activity, e.g. no inputs across a supposed scoring drive. Such intervals are
  down-graded to "unlabelled". Input data can never create a label.

## 5. Optional JEV tie-breaker (off)
`jev.py`:
* It is asked only where harness versions disagree, and its options are only the readings those versions produced,
  plus "unreadable". The test `test_never_mints_digits` checks it can never mint a new reading.
* It must calibrate first on ≥50 *independently labelled* disagreements and reach 99% precision at some confidence
  threshold. Below threshold it abstains.
* Its pick is advisory and can only break ties inside the lab's comparison. It never labels and never touches the watcher.
* Reality check: disagreements are 0–2 per game today, so calibration would take dozens of games. TypeSafe says Jev
  is weak at numeric precision, and image input is behind a beta header. **Recommendation: leave it off.**

## 6. What Con sees after each game (`games/<match>/REPORT.md`)
```
# Glimpse learned: match-20261017-010703  (ADVISORY_AI, not a score lock)
- Layout: thursday_night. Free labels: 21 frames {B: 21}.
- Live Glimpse on this game: right 7, wrong 4, unsure 8 (of labelled frames).
- Got wrong: tnp-089, tnp-153, tnp-193, tnp-263; unsure: 4; disagreed with the reader: 4.
- Lesson added: thursday_night: champion wrong on 4 tune frame(s) -> route thursday_night to crop_v1 (candidate).
- Held-out check: INSUFFICIENT_BANK (n=8).   - Waiting for your approval: nothing.
- Spend: $0.00 (replay); a live lab run would have cost about $0.038.
```
(The example is from the cold-start replay, where the champion is the old full-frame prompt. The 4 "wrong" frames are stat
overlays that full_v0 misread as scores.)

## 7. Integration points (QorCap **v0.3.27** on origin/main `d187199`; watcher **0.1.3** there, 0.1.2 on the box copy)
| item | path | lab access |
|---|---|---|
| signed receipt | `<match>\receipt.json` (`match.scorebug_profile`, `result.final_confirmed`) | read, sha256 recorded |
| lock chain | `checkpoints.jsonl` (live) / `score_checkpoints.jsonl` + `final.offline.json` (finalize) | read + re-verify |
| timeline | `timeline.jsonl` / `timeline.offline.jsonl`, `final.tail.json` | read |
| bundle | `bundle.json` (`match_id`, `qorcap_version`), contract `contracts/qorcap-bundle.v0.json` | read |
| video | `video.mp4` → ffmpeg `-ss` at sampled label times only (≈60 crops ≈ 2 MB per game) | read |
| input proof | `inputs.jsonl`, `input_proof.json` | read (corroboration only) |
| watcher output | `%LOCALAPPDATA%\QorCapGlimpse\<match>\advisory.jsonl`, `crops\`, `ledger.jsonl` | read (replies = data) |
| watcher prompts | `sidecar/glimpse/*.md` pinned by `PROMPT_SHA256` in `qorcap_glimpse_watch.py` | **never written**; promotion = QorCap PR |
| TD veto | `QORCAP_GLIMPSE_TD_VETO=1` (enforcing) | lab **refuses to propose** promotions while it is on, because then Glimpse can delay a fast-path lock |
| lab | `%LOCALAPPDATA%\QorCap\glimpse-lab\` {lesson_ledger.jsonl, spend_ledger.jsonl, heldout_bank.json, harness_registry.json, games\<match>\REPORT.md} | only place it writes |
Trigger: a separate `Glimpse Lab.cmd`, run by hand after `verify.txt` exists. Optionally later it could start itself
after Finalize/Start Match exits, but that needs a QorCap change and approval.

## 8. Cost per game (live; estimates from measured $0.00095 per crop call)
| step | calls | $ |
|---|---|---|
| labels, split, bank bookkeeping, report | 0 | 0 |
| champion reads on new sampled frames not read live (≈60/game) | 60 | 0.057 |
| probe 2 candidates × ≈36 tune frames (only if hard frames exist) | 72 | 0.068 |
| proposer (1–2 text calls) | 1–2 | 0.01–0.03 |
| bank gate, only for a tune-passing candidate (≤120 bank frames, champion reads already cached) | ≤120 | ≤0.114 |
| **typical game with no hard frames** | ~60 | **≈ $0.06** |
| **game with a candidate reaching the gate** | ~250 | **≈ $0.25 (cap)** |
The lab's weekly ceiling is $7 of the shared $10, so the watcher (≤$0.50/game) keeps headroom. At ~10 games/week, typical
lab spend is ≈ $0.6–2.5/week.

## 9. Novelty, folded in (details and sources: NOVELTY.md)
**Not novel:**
* self-improving prompt/context loops (Harness², GEPA, ACE, OPRO, TextGrad, MIPRO, Reflexion);
* shadow/champion-challenger promotion with approval and rollback (MLOps practice);
* reusable-holdout ideas (Dwork 2015);
* hash-chained audit logs and model signing (Sigstore model-transparency, llm-audit-trail);
* scoreboard OCR and labels from game state (ASAP, CS2-HUD-OCR-Crops);
* screenshot OCR result verification (US11020657B1, TrustBridge, PLLAY, Braket's AI referee).

**The combination where I found no prior art in 17 searches:** a vision **observer** that improves its own harness only from labels
derived from a **verified, hash-chained scoring authority's** record. Every learning step is written to an
**evidence-bound, hash-linked lesson ledger** whose entries cite the authority's checkpoint hashes and a replay digest. Promotion
needs a logged, look-budgeted held-out gate, and nothing the observer learns can flow back into the authority.

Three mechanisms added because the search showed a gap:
* **(N1) evidence-bound lesson ledger** (implemented): `receipt_refs` + `replay_digest` mean any auditor can recompute a lesson.
* **(N2) one-way harness attestation** (designed, needs a watcher change): every advisory record carries the harness sha256,
  the lesson-ledger head and the receipt sha256. The observer points *at* the authority, never the reverse.
* **(N3) lag-aware authority-interval labelling with a measured label-noise audit** (implemented): this is what took label
  error from 19% to 0% here.

## 10. Rollout plan
1. **Approve the proposal.** Offline only; $0; this branch. *(Done on the box.)*
2. **Label-only trial, 3 games, $0.** The lab runs after each match and writes labels, bank and report. Con eye-checks 20 labels
   per game, and the target is 0 wrong labels. No Glimpse calls by the lab.
3. **Paid reads trial, 3 games, cap $0.10/game, lab week cap $1.** Champion reads on sampled bank frames only. No candidates yet.
4. **Candidate trial, cap $0.25/game, lab week cap $3.** Candidates + gate. Promotions stay `PENDING`, and Con reviews the
   report, diff and ledger.
5. **First promotion, if ever.** QorCap PR with the new pinned prompt pack, merged by Con. The watcher stays shadow-only, and
   rollback is a revert.
6. Only after weeks of stable shadow results: discuss raising caps or adding layouts/games. TD-veto enforcing mode stays a
   separate decision.

## 11. Replay results (offline, $0; `replay_out/`)
| game (in play order) | layout | chain | cached frames | free labels (eye-audit) | champion watch_012 on labels | lab outcome |
|---|---|---|---|---|---|---|
| 125430 (Oct 16) | regular | ✓ 593 | 0 (no watcher then) | 0 (capacity 106) | – | NO_ENTRY: labels only, reads needed |
| 002326 | primetime | unsigned reader locks | 17 | 1 (1/1) | 1 right | NO_ENTRY |
| 010703 | thursday_night | unsigned reader re-run | 55 | 21 (13/13 comparable) | 13 right, 0 wrong | NO_ENTRY |
| 021003 | primetime | unsigned reader locks | 43 | 11 (9/9) | 9 right, 0 wrong | NO_ENTRY |
| 162055 | monday_night | ✓ 100 | 90 | 45 (45/45) | 45 right, 0 wrong | NO_ENTRY |

* **Forward** (today's watcher): 0 hard frames, 5 NO_ENTRY ledger rows, bank 36 frames (mnp 20, tnp 9, primetime 7),
  nothing pending, $0.
* **Cold start** (old full-frame prompt): 2 candidate lessons (route thursday_night → crop_v1; route primetime → crop_v0),
  both `INSUFFICIENT_BANK`. Lowering the bank floor to 8 *as an illustration only* gives `FAIL_NOT_SIGNIFICANT` (1 net fix,
  p=0.5). **Nothing would have been promoted** in any scenario. Both ledgers verify.

## 12. Risks
* Label noise from reader lag: measured, and mitigated by strict intervals. A new layout the reader doesn't know gives
  few or no labels, so learning there is slow by design.
* Survivorship: labels exist only where the reader was confident. Glimpse's real value is on frames where the reader
  was *unsure*, which are exactly the frames we can't label for free. The human review queue covers some of these.
* Small banks mean promotions are rare (≥5 net fixes with 0 losses).
* Overfitting a layout: mitigated by the permanent split, the look budget and per-layout banks.
* Cost drift: hard caps, plus the lab yielding to the watcher.
* Disk on MyTurn: 3 GiB floor, 1 GiB lab cap, crops pruned.
* Scope creep toward authority: the pin-by-sha in QorCap, the refusal while TD-veto is on, and approval-only promotion.
