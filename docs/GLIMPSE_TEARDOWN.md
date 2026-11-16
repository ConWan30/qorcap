# Elorian Glimpse: teardown and observer-only integration design

Status: **design draft**. This document does not change runtime behavior. No flag default moves, no prompt pin moves, and no receipt field is added.

Verified against `main` at `829e525` (v0.3.27 era: per-frame capture clock and pause-menu final screen, PR #48). The live watcher in this tree is `glimpse-watch/0.1.3`. Related drafts, neither merged: [PR #46](https://github.com/ConWan30/qorcap/pull/46) (glimpse-lab) and [PR #47](https://github.com/ConWan30/qorcap/pull/47) (capture-card flags).

QorCap's purpose, as this design uses it: honest, verifiable match recording and score receipts for Madden on a PS5 through a capture card. The reader, the checkpoint chain, the final screen, and the referee-signed receipt are the authority. Glimpse is an advisory vision watcher.

## Hard rules (every idea below)

| Rule | How a later implementation has to keep it |
|---|---|
| Glimpse stays observer-only. It never creates, changes, delays, or cancels a score lock, a receipt, or a verification result. | No call into `TouchdownJump.due`, `receipt.build`, `/events`, or `verify`. Output records carry `feeds_lock: false`. |
| `QORCAP_GLIMPSE_TD_VETO` stays off and is out of scope. | New files are not `td_verdicts.jsonl`. New code does not set `core.touchdown.veto`. |
| `receipt.v0.json` rejects unknown top-level fields (`additionalProperties: false` in `contracts/receipt.v0.json`). New artifacts are sidecar files. | Sidecars live under `%LOCALAPPDATA%\QorCapGlimpse\<match>\`. They are not written into `receipt.json`, `signals`, checkpoints, or `highlights/manifest.json`. |
| Learning and prompt changes happen after the match. | A fresh `.qorcap-run/recording.lock` (touched about every 2 s, stale after 30 s) makes a post-match tool exit before any model call. |
| Truth for a grade or a label comes from the reader's confirmed locks, the final screen, the referee-verified receipt, and human labels. | A source string containing `glimpse`, `advisory`, or `elorian` is refused as a label. Glimpse replies are graded data. |
| Every runtime change sits behind a flag that defaults off, and needs a real-game A/B with zero new wrong reads and no extra drops or lag before that default changes. | Same promotion shape as PR #47 and `docs/FRAME_CLOCK_AB.md`. |
| No reverse engineering of Sony, EA, or Activision software, or of the PS5 or DualSense firmware. | Frames already captured, on-screen pixels, and the USB HID log QorCap already writes. |
| Budget caps and fail-closed stops stay in every loop. The ceiling is a configured number, not a hard-coded $10. | `W` (week) and `G` (match) below. Missing, non-numeric, or ≤ 0 refuses every call. |

`signals` in `receipt.v0.json` does not set `additionalProperties: false` (that is how `signals.input` was added). This design still keeps Glimpse out of `signals`. A vision opinion inside the signed receipt would become part of the authority artifact.

## Price used for every cost table

From `sidecar/qorcap_glimpse_watch.py` (rates dated 2026-10-17 in the source):

| Symbol | Value | Meaning |
|---|---|---|
| Prompt price | $0.60 / 1M tokens | `PRICE_PROMPT = 0.6e-6` |
| Completion price | $3.00 / 1M tokens | `PRICE_COMPLETION = 3.0e-6` |
| Expected bill `P` | **$0.00076 / crop call** | Comment on `CROP_PROMPT_TOKENS`: measured on match 162055 |
| Reservation `R` | **$0.00135 / call** | `worst_case()` = 1000 × prompt price × 1.25 + 200 × completion price. Booked before the call, and booked in full when the call fails |
| Model | `elorian/glimpse` via OpenRouter chat completions | `temperature: 0`, reasoning off, strict JSON schema, 12 s timeout |

`authorise` refuses when `already_spent + R` would pass the cap. Successful calls are recorded at the provider's cost (planned here as `P`). Failed calls are recorded at `R`. The **Bill** column is `N × P`. The **Reservation** column is `N × R`: the ledger total if every call fails. A cap set equal to `N × P` stops a few calls early, because the pre-check adds one `R`. Stratum caps in Part 3 use `(N − 1) × P + R`, rounded up, so the advertised success-path count fits. Size `W` from the Bill column for a healthy week, or from the Reservation column if a week of timeouts should still finish the same call count.

A "match" in the full-tier live math is **75 minutes** of capture with a fresh frame available every 2 s (2,250 calls). The watcher also has a **180 minute** time stop (5,400 calls at one call per 2 s). Both are shown. Menu time, replays, and a dead tap reduce the real bill. This tree does not contain a measured per-match call count for a full game, so the tables are arithmetic on `P` and `R`, not an invoice.

OpenRouter `/key` `usage_weekly`, the local ledger, and the existing `--weekly-floor` (default `$0.82`) still feed `week_spent`. If `/key` is unreadable, today's code adds a `$0.50` margin. That margin stays. It is separate from `W`.

**What the owner sets later:** `W` dollars per rolling 7 days, `G` dollars per match. Environment names to use when this is implemented: `QORCAP_GLIMPSE_WEEKLY_LIMIT` and `QORCAP_GLIMPSE_GAME_CAP`. Today's process already has `--weekly-limit` (default `10`) and `--game-cap` (default `0.50`). This PR does not change those defaults. A full tier runs only when the owner sets `W` and `G` at or above that tier's Bill (healthy calls) or Reservation (every call fails). Until then the loop fail-closes at whatever number is configured, including today's defaults.

At `P`, **$10 buys about 13,157 successful crop calls per week** (about 7,407 calls if every call is reserved at `R` and then fails). That is why several cheap tiers look small in dollars: the crop is cheap, and the $10 line is a lot of sparse samples. The full tiers are the dense ones, and those do not fit in $10.

---

# Part 1 — Teardown

## 1.1 What it is here

Glimpse is an advisory vision watcher. QorCap's local scorebug reader owns every score lock. The watcher labels its own records `ADVISORY_AI` / AI-assisted, `advisory_only: true`, `feeds_lock: false`. Nothing in the receipt builder, the referee, `verify-bundle`, or the highlight cutter reads those records.

The pieces:

| Piece | Role |
|---|---|
| `src/glimpse.rs` | In-memory frame tap. Off unless `record --glimpse-frame` or `QORCAP_GLIMPSE_FRAME=1`. |
| `GET /frame/scorebug` in `src/api.rs` | GET-only handoff of the newest snapshot JPEG the reader already pulled. |
| `sidecar/qorcap_glimpse_watch.py` | The watcher. Version `glimpse-watch/0.1.3`. |
| `sidecar/glimpse/` | Pinned prompts and schema: `ROLE_v1.md`, `CROP_TASK_v1.md`, `LIVE_CONTEXT_v1.md`, `crop_v1.json`. |
| `sidecar/qorcap_reader.py` `TouchdownJump` | Writes held touchdown candidates to the watcher's folder. Reads a verdict only when `QORCAP_GLIMPSE_TD_VETO=1`. |
| `start-match.ps1` | Starts the watcher detached when the user env `QORCAP_GLIMPSE_FRAME` is `1`. |
| `docs/GLIMPSE_WATCH.md` | Operator notes. One drift: it says `--wait-start-s 900` (15 min). `start-match.ps1` passes `1800` (30 min) and `--idle-exit-s 60`. The script is the one that runs. |
| `sidecar/eval/human_labels.jsonl` | Two human finals (matches `20261018-084523` and `20261018-193534`). The eval README says these are for replay checks and are never fed to a receipt. |

## 1.2 How a frame gets to the model

1. ffmpeg writes `snapshot.jpg` by atomic rename. An outside process holding that file open has broken the rename before. The watcher is forbidden from opening it.
2. The live reader thread in `sidecar/qorcap_match.py` `start_reader` pulls `GET /snapshot.jpg` and then sleeps 0.25 s, so about 4 pulls per second. A byte-identical snapshot (a still end menu) is still fed to the reader about once a second (`STATIC_FRAME_S = 1.0`).
3. The `/snapshot.jpg` handler, after a valid JPEG is in hand, calls `FrameTap.offer`. Offer returns immediately. If the tap mutex is busy it skips that copy. It opens no file, starts no thread, and decodes nothing. Off: offer stores nothing.
4. `GET /frame/scorebug` calls `FrameTap.take`:
   - tap off → `404`
   - no frame yet → `503`
   - frame older than 3 s → `503` (a paused or stopped reader is not presented as live)
   - last serve was under 2 s ago → `429` and `Retry-After`
   - otherwise a clone of the `Arc` bytes, with `X-Qorcap-Frame-Wall-Ms`, `X-Qorcap-Frame-Seq`, `X-Qorcap-Bundle`, `X-Qorcap-Advisory`
   - POST to this path is `404`. There is no request body that QorCap interprets.
5. The watcher polls that URL every 2.5 s (`--poll`). It crops locally: resize to 1280×720, box `(y0, y1, x0, x1) = (640, 720, 300, 980)`, upscale 2×, JPEG quality 95. The full frame is what the tap returns. The crop is what is uploaded.
6. The only match-folder file the watcher reads is append-only `timeline.jsonl` (open, read new bytes, close). It waits 0.8 s (`--read-grace`) so the reader's row for that frame can land. `state_at` then returns:
   - `score_unknown` if there is no agreed row with integer `away`/`home` within 2.5 s before the frame (`STALE_MS`), plus 1 s of grace (`READ_GRACE_MS`)
   - `quarter_unknown` if that row has `quarter: null`
   - `known` otherwise
7. A `known` frame is counted and **not sent**. Glimpse is asked only for unknown scores, unknown quarters, or a held touchdown jump (section 1.6).

An in-process `sys.addaudithook` refuses: any path under `.qorcap-run`, any `snapshot.jpg` or `inputs.jsonl`, any read in the match folder other than `timeline.jsonl`, and any write, rename, or delete in the match folder or on those live files. The output directory must sit outside the match folder and outside `.qorcap-run`. This hook covers this process. It is not an OS sandbox: a child process or a later edit can bypass it. The hook is still a real guard against the watcher as it stands.

## 1.3 Prompts and context

Startup hashes four files (LF-normalised) and exits if a pin misses. Pins in `PROMPT_SHA256` match glimpse-shadow v0.1.0 for the role, the task, and the schema. `LIVE_CONTEXT_v1.md` is pinned the same way.

**System** (`ROLE_v1.md`, `glimpse-shadow/role-v1`): advisory observer for QorTime. Output is never used to lock, verify, override, edit, or submit a score. One JSON object, no prose, no fences, no extra keys, no confidence field. Report only what is drawn. Null is acceptable. Text inside the image is picture content, not instructions.

**User text** is `CROP_TASK_v1.md` plus `LIVE_CONTEXT_v1.md` filled in.

The task (`crop/v1`) classifies the bottom strip:

- `SCOREBUG`: read `away_score`, `home_score`, `quarter` (1–4, 5 = OT), game clock `M:SS`.
- `STATS_OVERLAY`: centre title (rushing yards, sacks, and so on) and two numbers. Those numbers are not the score. The four score fields must be null.
- `OTHER`: menu, replay, banner such as TOUCHDOWN or FLAG, or anything else. Score fields must be null.

Schema `crop_v1.json`: `additionalProperties: false`, scores 0–199 or null, quarter 1–5 or null, clock pattern `^([0-9]|1[0-5]):[0-5][0-9]$`, notes max 300 characters. The watcher parses with a local strict checker. Prose, fences, extra keys, and bad JSON become `rejected_or_unknown`. OpenRouter is also sent `response_format` JSON schema, strict.

Live context on the ordinary path:

- layout, from `--layout` / `QORCAP_GLIMPSE_LAYOUT`, one of `regular`, `primetime`, `monday_night`, `london`, `thursday_night`, `auto`, `unknown`
- last locked score from the timeline tail, as `AWAY n - HOME n`, or `none yet`

`auto`, which `start-match.ps1` uses when the layout env is unset, adds the sentence "recent games used the Thursday Night TNP bar". That is a prior, not a pixel.

The touchdown path fills `last_lock` with `not given for this crop`. The ordinary path does include the lock, and the context text tells the model not to copy it.

After a valid reply, `assess` applies football rules aligned with QorTime `implausible_transition_reason` plus "a +1 only after a +6":

- no reference lock → not plausible
- equal to the lock → plausible, flag `agrees_with_lock`
- sides swapped, a decrease, both sides changed, or a delta outside {1, 2, 3, 6, 7, 8} → not plausible
- quarter may move back never, and may skip more than one only if the reader's own quarter is older than 5 minutes
- a quarter is attached only when the score was also plausible
- a stats overlay, or a scorebug reply that also carries a stat title, yields no advisory score (`stats_overlay_no_score` / `scorebug_with_stat_title_suppressed`)

The raw model object is stored either way. `advisory_score` is the filtered view.

## 1.4 What calls the model, how often, and what it costs

One client: `Client.ask` → `POST https://openrouter.ai/api/v1/chat/completions`, model `elorian/glimpse`. The key is `OPENROUTER_API_KEY` from the environment. The watcher prints that the key is missing; it does not print the key. `start-match.ps1` copies the user-env key into the watcher process and removes it from the parent so the recorder does not keep it.

Call gates, in order, all current defaults:

| Gate | Default | Effect |
|---|---|---|
| Circuit | 3 failures, then 300 s | `refused: circuit_open` |
| Call cap | 500 / match | `refused: max_calls` |
| Key | required for a real POST | `refused: no_key`. Frames are still counted. |
| `authorise(R)` | game $0.50, week $10 | `BudgetRefused` before the socket opens |
| Week figure | max(local 7-day ledger, `/key` `usage_weekly`, `--weekly-floor` $0.82); +$0.50 if `/key` failed | checked at most every 120 s |
| Min interval | 6 s | `rate_skipped` |
| Other-streak backoff | 15 s after three `OTHER` replies in a row | replays and menus slow down |
| Confirm hold | 20 s after a plausible agree that also read a quarter, cleared if the lock pair changes | `hold_skipped` |
| Dedupe | mean abs grey diff under 2.0 on a 170×20 thumb, for 30 s | `dup_skipped` |
| HTTP timeout | 12 s | failure booked at `R`, not at $0 |
| Process lifetime | 180 min; unreachable QorCap for 300 s (60 s after the first good frame when Start Match launched it; 1800 s waiting for the first frame) | stops the watcher only |

A failed call is described in source as not billed by the provider. The local ledger still records `R` so a flapping provider cannot walk past the game cap. `/key` is the cross-check. Recording is not stopped.

**Worked budget.** At `P`, the $0.50 game cap is about 657 successful calls, and `max_calls` cuts that to 500 ($0.38 at `P`). At `R` on every failure, the cap is about 370 failures. A clean match that only sends a handful of unknown frames and a few touchdown opinions costs cents. A match whose quarter stays null (the 162055 pattern described in the watcher comments: quarter null from 8:12, so every later frame is `quarter_unknown`) walks toward the cap, because `known` is the only state that is free.

The tap serves at most one frame per 2 s. The watcher sends at most one ordinary call per 6 s. The reader sees about 4 frames per second. Most pixels the reader uses are never shown to Glimpse.

## 1.5 Outputs and where they go

Default root: `%LOCALAPPDATA%\QorCapGlimpse\` (or `~/.local/share/QorCapGlimpse`). Per match:

| File | Contents |
|---|---|
| `advisory.jsonl` | One JSON object per sent or refused call. Tags, prompt sha, model, `local_state`, reference lock, validated reply, flags, cost, `gen_id`, raw text truncated to 600 chars. |
| `td_verdicts.jsonl` | `{id, verdict, frame_wall_ms}` for touchdown second opinions. Written in shadow mode too. |
| `td_jumps.jsonl` | Written by the **reader sidecar**, not by the watcher: `pending` / `posted` / `cancelled` / `superseded` for held fast-path jumps. |
| `REPORT.md` | Live cost and the last 25 ordinary reads. Touchdown rows are not in that table (`regions_seen` is only appended on the ordinary path). |
| `watch.log` | The same lines printed to the console. |
| `crops/<id>.jpg` | The uploaded crop, unless `--no-save-crops`. |
| `ledger.jsonl` (root, not per match) | `{t, match_id, cost, gen_id, kind}`. |

The match folder, the checkpoint chain, `receipt.json`, and the highlight manifest are unchanged by a watcher run.

`td_verdicts.jsonl` is the one file a QorCap process knows how to read. `td_glimpse_veto` maps `agree` → agree, `disagree*` → disagree, anything else → wait. It is wired only when `QORCAP_GLIMPSE_TD_VETO` is exactly `1`.

## 1.6 Touchdown second opinion, and the veto flag

Fast path (`TouchdownJump`): a one-sided +6/+7/+8 can lock from fewer reads than the normal 3-read / 9-read windows, after a margin gate (0.15, or 0.18 on a look-alike digit pair 0↔6, 0↔8, 0↔9, 3↔9) and a hold (1 s, or 2 s if confusable). While the jump is held, the live sidecar appends `pending` to `td_jumps.jsonl` in the Glimpse folder.

The watcher, on the next polled frame at or after that `t_ms` (and not older than 15 s), sends **one** crop with no score hint. `td_verdict` returns `agree`, `disagree_old`, `disagree_other`, or `no_read`. The record is `kind: td_second_opinion`, `feeds_lock: false`. This call does not pass through the 6 s interval, the confirm hold, or the dedupe window. It does pass through `authorise`, the circuit, and the 500-call cap.

Shadow mode, the default: `core.touchdown.veto` is `None`. The lock posts when the hold ends. `test_shadow_by_default_writes_only_the_watchers_folder` and `test_offline_finalize_core_never_has_glimpse` cover that. Finalize's `default_core_factory` does not call `wire_touchdown_glimpse`.

Enforcing mode is off and **out of scope for every design in Part 2**. Documented so the boundary is visible: if the env var were `1`, `due` would wait up to 4 s (`TD_VETO_WAIT_MS`) after the hold. `disagree` cancels that fast-path lock (`glimpse_veto`); the normal windows still apply. `agree` posts. No verdict posts after the wait. A veto exception posts rather than blocking forever. The model still cannot create a lock. This draft does not propose turning that flag on, reading `td_verdicts.jsonl` from new code, or lengthening the wait.

## 1.7 What a record can influence

In shadow mode the watcher can:

- spend OpenRouter money, up to the caps
- write the files in section 1.5
- use CPU at below-normal priority (`BELOW_NORMAL_PRIORITY_CLASS` on Windows, `os.nice(19)` elsewhere, OpenCV pinned to 1 thread)
- exit itself

It cannot, with the veto flag off:

- post a score event, delay a lock, or cancel a lock
- write `timeline.jsonl`, checkpoints, `receipt.json`, or the bundle
- open the live snapshot or the input log
- change a challenge result, a highlight file, or a verify result
- keep recording running or stop it (`start-match.ps1` wraps launch in `try/catch` and prints that the match is unaffected)

The self-asserted `feeds_lock: false` is a label. The structural boundaries are: no POST back into QorCap, output directory outside the match, veto callback unset, finalize unwired, receipt schema closed at the top level, and no reader of `advisory.jsonl` on the authority path.

## 1.8 Current data flow and trust boundaries

```mermaid
flowchart TB
  subgraph play [Play]
    PS5[PS5 HDMI out]
    Card[USB capture card]
    PS5 --> Card
  end

  subgraph rec [Recording process]
    FF[ffmpeg atomic snapshot.jpg]
    Card --> FF
    API["GET /snapshot.jpg about 4 Hz"]
    FF --> API
    Reader[ReaderCore locks and finals]
    API --> Reader
    TL[timeline.jsonl]
    CP[checkpoint chain]
    Receipt["receipt.v0.json"]
    Reader --> TL
    Reader --> CP
    CP --> Receipt
    Tap["FrameTap.offer try_lock skip if busy"]
    API --> Tap
  end

  subgraph obs [Observer process]
    Get["GET /frame/scorebug 1 per 2 s stale over 3 s refused"]
    Tap --> Get
    Watch[qorcap_glimpse_watch.py]
    Get --> Watch
    TL -.->|only timeline.jsonl| Watch
    TD[td_jumps.jsonl in the Glimpse folder]
    Reader -.->|shadow listener| TD
    TD -.-> Watch
    Crop[local scorebug crop]
    Watch --> Crop
    OR["OpenRouter elorian/glimpse"]
    Crop --> OR
    Out["advisory.jsonl REPORT.md crops ledger td_verdicts.jsonl"]
    OR --> Out
  end

  Receipt -.-x Out
  Out -.-x Reader
```

Solid arrows are the authority path or the frame the watcher is allowed to pull. Dotted arrows are the only extra reads: timeline bytes, and touchdown candidates the sidecar already exported. The crossed lines are the trust boundary: receipts are not produced from Glimpse files, and Glimpse files are not applied to the reader while the veto flag is off.

```mermaid
flowchart LR
  subgraph zones [Trust zones]
    Z1[Zone A capture and reader]
    Z2[Zone B memory tap GET only]
    Z3[Zone C watcher and OpenRouter]
    Z4[Zone D receipt referee highlights]
  end
  Z1 --> Z2 --> Z3
  Z1 --> Z4
  Z3 -.->|no path while veto is off| Z4
```

Zone D also includes challenges, `input_proof.json`, and `highlights/manifest.json`. They are built from the bundle and the reader. They do not load `advisory.jsonl`.

## 1.9 Boundaries that keep locks and receipts untouched

1. Frame tap is opt-in, GET-only, rate-limited, and non-blocking (`src/glimpse.rs` tests `off_by_default_and_stores_nothing`, `a_held_tap_never_blocks_the_reader`).
2. Watcher audit hook: match folder is read-only, and only `timeline.jsonl` is readable (`sidecar/test_qorcap_glimpse_watch.py` installs the guard).
3. Output root refused if it is inside the match folder or `.qorcap-run`.
4. Ordinary sends happen only on `score_unknown` or `quarter_unknown`.
5. Strict schema plus plausibility filter. Stats never become an advisory score.
6. `feeds_lock: false` on every emitted record, asserted in the watcher tests.
7. Touchdown verdicts affect `due` only if `QORCAP_GLIMPSE_TD_VETO=1`. Default wiring is shadow. Finalize does not wire it.
8. Receipt top level, `match`, `result`, `players`, `referee`, each challenge, and each checkpoint set `additionalProperties: false`.
9. Start Match swallows a watcher launch failure.
10. Prompt bytes are pinned. A lab or a hand edit cannot change the live prompt without a QorCap change that updates the pin.

## 1.10 Related drafts

**PR #46, glimpse-lab** (`glimpse-lab-proposal`, draft). A separate `glimpse-lab/` tree. Nothing on `main` imports it. Post-match, observer-only, labels from hash-checked locks and a confirmed final, 40% permanent held-out bank, McNemar gate, `PENDING_APPROVAL`, per-game $0.25 in that draft, lab week stop at $7 so the live watcher kept $3. Replay on five past games spent $0 and promoted nothing. The circularity guard refuses any label source named glimpse, advisory, or elorian. This design treats that loop as prior work inside the project, not as something to invent again. Part 2 idea 5 is that loop with `W` and `G` made configurable, and with witness frames used only as graded data.

**PR #47, capture-card flags** (`cursor/capture-path-ab-ab65`, draft). Every new capture flag defaults off. Promotion needs the same footage, zero new wrong reads, no missed locked scores, no worse drops, audio lag, queue, fps, CPU, or disk, and at least one metric better than the noise floor. `--yuy2-720p60` failed on the MS2131 and stays off. Any Glimpse idea that touches `FrameTap`, the snapshot path, or ffmpeg has to pass that gate. Ideas that only read after the match do not touch that path; their A/B is "receipt bytes and verify output unchanged, and the recording process is not running."

## 1.11 Weaknesses, gaps, wasted signal

These are properties of the code as it is. They are the reason Part 2 exists.

1. **Known frames are skipped.** The locks that become the receipt are exactly the frames Glimpse does not see. The watcher spends its calls when the reader is already unsure. A second opinion on the authority path's actual inputs does not exist.
2. **The ordinary prompt includes the last lock.** The touchdown path withholds it. An "unknown" read is not a blind read. Combined with the plausibility filter, a wrong lock can be echoed back as `agrees_with_lock` and then suppress further calls for 20 s.
3. **`auto` layout biases the model** toward the Thursday Night bar.
4. **Touchdown opinion is one later frame.** The hold is 1–2 s. The watcher polls every 2.5 s and may send a frame up to 15 s later. A TOUCHDOWN or FLAG banner is `OTHER` → `no_read`. The pixels of the disputed bar are not kept. There is no ring buffer.
5. **That one call skips the 6 s spacing** the rest of the budget relies on, then returns before the ordinary path, so the unknown-state check for that same frame never runs.
6. **`td_verdicts.jsonl` is written whenever a second opinion runs**, including shadow mode. The enforcing read path is already implemented. The flag is one environment variable. New designs must not add another reader of that file.
7. **Stats titles, clocks, implausible flags, and `legal_change` flags are stored and then unused.** No highlight cutter, no eval bank, no receipt sidecar, and no human queue reads `advisory.jsonl`. `REPORT.md` drops the touchdown rows.
8. **The advisory folder is outside the bundle.** It does not travel with the match, and it is unsigned. Anyone who can write the folder can rewrite the "witness" after the fact.
9. **The audit hook is in-process.** Good against this script's own `open`. Not a boundary against another program.
10. **Fixed crop** `(640, 720, 300, 980)` on a 1280×720 frame. Layouts whose bar sits elsewhere become `OTHER` or a wrong read. End screens and pause menus are mostly outside that strip, so the current task is a weak menu labeler even when a frame is sent.
11. **Quarter is dropped unless the score was plausible.** A right quarter on a wrong score never lands in `advisory_quarter`.
12. **Human labels are two rows** and are not joined to crops.
13. **Content Credentials are not in this checkout.** Highlight clips (v0.3.27) burn `SCOREBUG LOCK`, write `highlights/manifest.json` (`qorcap-highlights/v0`), and refuse to run while `recording.lock` is fresh. There is no C2PA manifest, no `.c2pa` sidecar, and no clip pixel check.
14. **Doc drift:** `docs/GLIMPSE_WATCH.md` still says a 15 minute wait and, in one heading, watcher 0.1.2. The script waits 30 minutes and the module version is 0.1.3.
15. **`--weekly-floor` $0.82** can refuse calls when `/key` is down even if this week's real spend is lower. Fail-closed, and it can also waste a match of signal.

## 1.12 What this tree already does without Glimpse

Recording and the hash chain, the template reader and agreement windows, the touchdown fast path, end-menu and pause-menu finals, challenges (rule v2), finalize, `receipt.v0`, highlight clips, `input_proof.json` and optional `frame_clock.jsonl` (flag off, PR #48). Glimpse does not sit on any of those outputs today except the shadow touchdown log.

---

# Part 2 — Integrations

Seven ideas. Each can run at a **cheap tier** (expected spend at 25 matches in a week stays inside $10, alone and in the combined cheap stack) or a **full tier** (no weekly target; the loop still refuses to run until `W` and `G` are set high enough). Costs use `P = $0.00076`. "Reservation" is `calls × R` with `R = $0.00135`, the number `authorise` actually withholds.

Ideas are incremental: a call counted in one table is not assumed free in another. The combined table at the end removes the double count where two ideas would otherwise buy the same frame.

Ranks are by value to verifiable receipts, not by novelty.

## Idea 1 — Unhinted witness of frames the reader already locked

**Rank 1. Recommended.**

**What it adds.** A sparse or dense set of blind crop reads on frames the timeline already calls `known`, written as `kind: "witness"` with `feeds_lock: false`. After the match, those reads can be graded against the lock that was in force and against the final receipt. QorCap gains a pixel witness of the scores that actually entered the chain. Today that witness does not exist, because `known` returns before any model call.

The prompt uses the touchdown wording: `last_lock = "not given for this crop"`. The lock is stored beside the reply as `reference_lock` for the grader, and it is not placed in the user text.

**Flag.** `QORCAP_GLIMPSE_WITNESS`, default off. Off means today's skip-known behavior, byte for byte in the send decision.

**Prior work.** A second model beside a primary scorer is ordinary shadow deployment and champion/challenger practice ([DataRobot's write-up](https://www.datarobot.com/blog/introducing-mlops-champion-challenger-models/)). Disagreement between models as a reason to look harder is query-by-committee ([Seung, Opper, Sompolinsky 1992](https://doi.org/10.1103/PhysRevA.45.6056)). Broadcast scoreboard OCR that then aligns to play-by-play is published ([ASAP](https://arxiv.org/abs/2301.06866)). Products already put a vision model on the result itself: [Braket's AI referee](https://braket.gg/docs/developers/ai-referee) confirms end screens at a confidence threshold or sends a dispute, and [Sunday Clipboard](https://www.sundayclipboard.com/) OCRs Madden box scores with a human confirm. Patent [US11020657B1](https://patents.google.com/patent/US11020657B1/en) validates screenshot OCR against user-entered results and grows a mask library from validated submissions. Those systems put the vision step on the authority path or on the reporting path. This idea keeps the vision step off that path and grades it later from the reader's locks. The blind-read trick is already in this repo's touchdown path.

**Cost.**

| Tier | Calls / match | Bill / match | Reservation / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|---:|
| Cheap: 1 unhinted crop every 45 s for up to 75 min, cap 100 calls | 100 | $0.076 | $0.135 | $0.76 | $1.90 |
| Full: 1 crop every 2 s for a 75 min match | 2,250 | $1.71 | $3.04 | $17.10 | $42.75 |
| Full time-stop: every 2 s for 180 min | 5,400 | $4.10 | $7.29 | $41.04 | $102.60 |

Cheap fits $10/week at both volumes with a wide margin. Full does not. To finish the 75 min full tier, set `G` to at least **$1.72** for that stratum and `W` to at least **$17.10** (10 matches) or **$42.75** (25 matches) on top of whatever else runs. The 180 min stop needs `G` ≥ **$4.11**.

**Risk.** A biased witness that still sees the lock in a prompt would rubber-stamp bad locks; the withheld-lock rule is the mitigation, and it is testable. Dense live calls add network and CPU on the watcher process. The watcher is already below-normal and off the ffmpeg thread; the A/B still has to show no extra drops or lag, because a busy machine can miss USB frames. Wrong witness reads must not be promoted into templates. The reader pack changes only by the existing human/template path, after the match, in a separate PR.

## Idea 2 — Disagreement-triggered evidence burst

**Rank 2.**

**What it adds.** When a witness read, an unknown-frame read, or a held touchdown candidate disagrees with the lock, keep the recent frames and one blind crop of the frame closest to the event. The 13-8-versus-13-0 failure mode (match `20261017-021003`, described in `qorcap_reader.py`) was a look-alike digit on a few frames, corrected 250 ms later by the bar itself. Today's second opinion can land on the celebration banner and record `no_read`. A burst keeps the bar pixels in the Glimpse folder so a person, or the post-match grader, can see them. The burst does not cancel the fast path. That remains the veto flag, which stays off.

**Flag.** `QORCAP_GLIMPSE_BURST`, default off. A memory ring inside `FrameTap` is a second flag, `QORCAP_GLIMPSE_BURST_RING`, default off, so the offer path stays a no-op until both an A/B and an explicit enable. With the ring flag off, the burst may use only frames the watcher has already pulled (about one every 2.5 s).

**Prior work.** Disagreement and uncertainty as acquisition functions are standard in active learning for images and video ([ALWOD, ICCV 2023](https://openaccess.thecvf.com/content/ICCV2023/papers/Wang_ALWOD_Active_Learning_for_Weakly-Supervised_Object_Detection_ICCV_2023_paper.pdf); [temporal coherence for video active learning](https://arxiv.org/abs/1908.11757)). A circular pre-roll of recent frames is a normal recorder feature ([OBS replay buffer](https://obsproject.com/kb/replay-buffer)). Instant replay in broadcast trucks is the same shape. None of that is a license to feed the replay into a score lock.

**Cost.** Assumes the reserved slots, not a measured disagreement rate. PR #46's replay notes saw on the order of 0–2 hard frames per game on cached replies. If that rate holds, expected spend is near the "2 events" row even when the cap is higher. The cap is what fail-closed enforces.

| Tier | Calls / match | Bill / match | Reservation / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|---:|
| Cheap: at most 8 events, 1 crop each | 8 | $0.006 | $0.011 | $0.06 | $0.15 |
| Full cap: at most 30 events, 4 crops each | 120 | $0.091 | $0.162 | $0.91 | $2.28 |
| Expected if 2 events and 4 crops | 8 | $0.006 | $0.011 | $0.06 | $0.15 |

JPEG writes are local disk, not OpenRouter. Cheap and full both fit in $10/week on model spend. The full tier is "no cap" in the sense that 30×4 is a design ceiling the owner can raise by setting `S_burst`; the dollars stay small at crop prices.

**Risk.** A ring that takes the tap mutex will stall or skip reader copies. The spec uses `try_lock` and drops the sample, the same rule as `offer` today. PR #47's gate applies before the ring flag may default on: zero new wrong reads, no extra drops, no extra lag. Burst files are unsigned until idea 3. A disagreement is not a label.

## Idea 3 — Signed observer attestation sidecar

**Rank 3.**

**What it adds.** After the match, hash the witness and burst artifacts and sign them with a **separate** observer key. The statement says: these bytes were the observer's output, and they point at this receipt sha256 and this checkpoint head. It does not say the score is verified. Verifiers of `receipt.v0` keep ignoring the sidecar. QorCap gains a tamper-evident copy of the witness that can sit next to a match without entering the receipt.

**Flag.** `QORCAP_GLIMPSE_ATTEST`, default off. Runs only after recording has stopped.

**Prior work.** Signed sidecars that name an artifact by digest are the normal way to publish provenance: [in-toto attestations](https://github.com/in-toto/attestation), [SLSA provenance distribution](https://slsa.dev/spec/v1.2/distributing-provenance), [Sigstore model-transparency](https://github.com/sigstore/model-transparency). QorCap already signs receipts with Ed25519 over JCS. This idea reuses that shape with a different key, so an observer signature cannot be confused with a referee signature. PR #46 designed a one-way harness attestation (their N2) and did not implement the watcher change. This is that sidecar, for witness bytes rather than for a learned prompt.

**Cost.** Hash and Ed25519 are local. **$0** model cost at both tiers, at 10 matches and at 25. The configurable cap still exists: clip-audit calls inside the same post-match tool are idea 4's budget, and a missing `W` refuses those calls. The signature step itself does not call OpenRouter.

| Tier | Model calls | Bill / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|
| Cheap and full | 0 | $0 | $0 | $0 |

**Risk.** A signature from the referee key or the device key would look like authority. The key path is distinct, and the statement schema has no score field that a receipt parser accepts. Writing the sidecar into the match folder is off unless `QORCAP_GLIMPSE_ATTEST_COPY=1`, because a future directory hasher might start caring. `verify-bundle` today checks named files (manifest segments, checkpoints, inputs), not a closed file list; the copy flag still defaults off. An unsigned file must not parse as a successful attestation.

## Idea 4 — Highlight-clip audit, placed before any Content Credentials step

**What it adds.** After `qorcap highlights` has cut clips, Glimpse reads frames from each clip with no score in the prompt. The tool then compares the reply to the lock score already stored in `highlights/manifest.json`. Agree, disagree, and no_read go to `highlights_audit.jsonl` in the Glimpse folder. The mp4, the vtt, and `manifest.json` stay as the cutter wrote them. The panel on the clip still says `SCOREBUG LOCK`.

Content Credentials are **not in this repository** (no C2PA writer, no `.c2pa` file, no credential assertion on the highlight path). If a later change attaches credentials, the audit's job is to have looked at the same clip bytes that will be hashed, and to record that hash in the sidecar. Credential attachment does not wait on Glimpse, does not read the audit to decide whether to attach, and does not put Glimpse's score into the receipt. A C2PA manifest can already live as a `.c2pa` sidecar or inside the file ([C2PA 2.3](https://spec.c2pa.org/specifications/specifications/2.3/specs/C2PA_Specification.html), [manifest locations](https://opensource.contentauthenticity.org/docs/manifest/understanding-manifest/)). Putting an advisory score into a claim that consumers treat as the match result would break the observer rule, so this design does not add that assertion.

**Flag.** `QORCAP_GLIMPSE_CLIP_AUDIT`, default off. Post-match only. Requires the signed `receipt.json` the highlight tool already requires, and refuses a fresh `recording.lock` the same way.

**Prior work.** Content Credentials / C2PA are an existing provenance standard for media, including sidecar manifests. Highlight packages that bind a clip to a hash and a ledger entry are already what `qorcap-highlights/v0` does. Vision review of an esports clip before a human or a platform accepts it is the dispute-queue pattern Braket documents. None of that requires the reviewer to be allowed to edit the receipt.

**Cost.** README's example match 125430 produced 6 clips. Tables use 8 clips as a typical match. Cheap cap 15 clips. Full is 12 frames per clip (the 8 s + 4 s window at 1 frame/s) with a cap of 20 clips.

| Tier | Calls / match | Bill / match | Reservation / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|---:|
| Cheap: 1 crop on each of 8 clips | 8 | $0.006 | $0.011 | $0.06 | $0.15 |
| Cheap cap: 15 clips × 1 | 15 | $0.011 | $0.020 | $0.11 | $0.29 |
| Full: 8 clips × 12 frames | 96 | $0.073 | $0.130 | $0.73 | $1.82 |
| Full cap: 20 × 12 | 240 | $0.182 | $0.324 | $1.82 | $4.56 |

Both tiers fit in $10/week at 25 matches. Full is still the uncapped design relative to today's watcher: it spends where today spends $0, and the owner sets `S_clip` to the row they want. Raising the cap past 20×12 is a config change, not a code change.

**Risk.** A hinted prompt ("the lock says 13-8") will tend to agree. The prompt stays unhinted; the comparison is local, after the reply. Auditing the burned-in panel instead of the scorebug pixels would just OCR our own overlay. Frames are taken from the source window the manifest already names, and the crop is the scorebug strip, not the overlay corner. A disagree row is a review queue, not a broken receipt.

## Idea 5 — Post-match learning loop, from PR #46, with a configurable ceiling

**What it adds.** The glimpse-lab loop: labels from hash-checked locks and a confirmed final, a permanent held-out split, propose / probe / compare, a bank gate, `PENDING_APPROVAL`, and a lesson ledger that cites checkpoint hashes. Nothing is promoted into the pinned prompts except by a later PR the owner merges. The watcher keeps refusing unpinned prompt bytes.

What this draft adds on top of PR #46: `W` and `G` are the owner's numbers; the lab stops at those numbers instead of a hard-coded $7 and $0.25. Witness and burst replies are **graded** against labels. They are not labels. A witness agree on a frame the reader locked is not new truth. The lab still refuses to learn while `QORCAP_GLIMPSE_TD_VETO=1`, as PR #46 already says, because that flag lets Glimpse delay a fast-path lock.

**Flag.** `QORCAP_GLIMPSE_LAB`, default off. A separate process, after `recording.lock` is stale. PR #46's folder `%LOCALAPPDATA%\QorCap\glimpse-lab\` stays the write root.

**Prior work.** The loop shape is published prompt optimization: [Harness²](https://arxiv.org/abs/2610.14224) (propose / probe / keep-base, as reviewed in PR #46's `NOVELTY.md`), [GEPA](https://github.com/gepa-ai/gepa) including OCR uses, [ACE](https://arxiv.org/abs/2510.04618), [OPRO](https://arxiv.org/abs/2309.03409). Held-out reuse and look budgets go back to [Dwork et al., Science 2015](https://www.science.org/doi/10.1126/science.aaa9375). Labels from an independent game-state record exist as datasets, for example [CS2 HUD crops labelled from demo state](https://huggingface.co/datasets/ybashir/CS2-HUD-OCR-Crops). US11020657B1 already learns layouts from validated results, on the reporting path. PR #46's own novelty note lists these and does not claim the individual techniques are new. This draft does not add a "first" claim. It wires that loop to a configurable cap and to witness grades.

**Cost.** Cheap matches PR #46's measured shape, repriced at `P` (that draft used about $0.00095). Full is a cold re-probe: 8 candidate prompts × 200 tune frames, and a no-cache upper bound of 9 versions × 400 frames.

| Tier | Calls / match | Bill / match | Reservation / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|---:|
| Cheap, typical, no candidate | 60 | $0.046 | $0.081 | $0.46 | $1.14 |
| Cheap, candidate reaches the gate | 250 | $0.190 | $0.338 | $1.90 | $4.75 |
| Full, 8 candidates × 200, cache misses only | 1,600 | $1.22 | $2.16 | $12.16 | $30.40 |
| Full upper bound, no cache, 9 × 400 | 3,600 | $2.74 | $4.86 | $27.36 | $68.40 |

Cheap fits $10/week at 25 matches even if every match reaches the gate ($4.75). Full does not fit in $10. Cached replies from the live watcher and from idea 1 are $0; the full upper bound is the bill if the cache is empty. Set `G` ≥ **$0.34** for the cheap candidate row, ≥ **$2.16** for the 1,600-call row, ≥ **$4.86** for the no-cache bound.

**Risk.** Label noise from reader lag. PR #46 measured a loose interval at 22 wrong labels out of 115, and the strict interval at 0 of 68, on the games they had. That audit has to be re-run before any prompt PR. Learning from Glimpse's own agrees would launder a witness into truth; the source-name guard forbids it. A promotion that edits `ROLE_v1.md` or the stats-never-score rule is rejected by the harness validator, as in that draft.

## Idea 6 — End-screen and pause-menu candidate queue for the human bank

**What it adds.** A post-match queue of frames that look like a pause menu, an end screen, or `OTHER`, for a person to label into the held-out bank. Glimpse may propose the queue. The label that enters the bank is a human label, or the reader's own `final_screen` / end-menu read, which already exists and does not use Glimpse. `sidecar/eval/human_labels.jsonl` is the precedent: person, match, score, layout, note.

The current crop is the bottom strip, which is the wrong window for a full menu. The queue stores a full-frame JPEG from `video.mp4` at a timestamp, and the model call is still the pinned crop task unless a later PR pins a second task. Until that pin exists, the cheap tier only samples timestamps the reader already marked (final-screen detector, pause detector, `scorebug_absent`) and asks the existing crop task. Those calls mostly confirm `OTHER`. That is still useful as a negative for the scorebank, and it does not invent menu labels from the model.

**Flag.** `QORCAP_GLIMPSE_MENU_QUEUE`, default off. Post-match only.

**Prior work.** QorCap already detects pause menus and end menus with templates (`madden_final_screen`, `EndMenuReader`, pause-menu final in PR #48). Final-score screen OCR for tournaments is an old patent ([US5746656](https://www.freepatentsonline.com/5746656.html)). Choosing which frames a person should label is active learning with a frame budget ([frame-level video queries](https://arxiv.org/abs/2307.05587)). The bank those labels enter is the PR #46 bank. Glimpse does not write the label row.

**Cost.**

| Tier | Calls / match | Bill / match | Reservation / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|---:|
| Cheap: 30 timestamps the reader already flagged | 30 | $0.023 | $0.041 | $0.23 | $0.57 |
| Full: up to 400 menu-candidate frames | 400 | $0.304 | $0.540 | $3.04 | $7.60 |
| Full ceiling if the filter lets every 2 s frame through, 75 min | 2,250 | $1.71 | $3.04 | $17.10 | $42.75 |

Cheap fits $10. The 400-call full tier also fits at 25 matches ($7.60) and is the one to configure. The 2,250 ceiling is what happens if the local filter fails open and `S_menu` was set to the witness-sized number; the spec fail-closes at `S_menu`, so the owner picks 400 or 2,250 explicitly. Do not run this ceiling and idea 1's dense witness on the same frames: see the combined table.

**Risk.** Model-written labels in the bank would train the next prompt on Glimpse's mistakes. The queue file and the label file are different schemas, and the label loader refuses `source: glimpse`. A menu crop misread as a scorebug is an ordinary `assess` rejection if it fails plausibility; the human still sees the frame.

## Idea 7 — Challenge-scene note and input-gap downgrade

**What it adds.** Two post-match sidecars.

- `challenge_scene.jsonl`: for each challenge in the receipt (kickoff, halftime, end), one or a few unhinted frames around `issued_ms`. The note says whether the crop task returned `OTHER` or `SCOREBUG`. It does not set `challenges[].result`. The HID rule (rule v2, the 600 ms family of checks) stays the result.
- `input_gap.jsonl`: local join, **no model call**. For each locked score change, if `inputs.jsonl` shows no reports in the preceding 30 s, mark that interval `downgraded` so idea 5 will not label it. Input cannot create a label. This matches the limit already printed on `input_proof.json`: the log shows a USB controller in sync with the video, not who held it. No DualSense firmware, no PS5 system software.

**Flag.** `QORCAP_GLIMPSE_CHALLENGE_NOTE`, default off, for the model part. The input-gap join is `QORCAP_GLIMPSE_INPUT_GAP`, default off, and it only writes the sidecar.

**Prior work.** QorCap already records challenges and already aligns HID to video (`src/inputproof.rs`, `src/align.rs`). Kernel or server anti-cheat products (Easy Anti-Cheat, BattlEye, FACEIT and similar) attest a game process on the PC that runs the game. They are a different mechanism from a capture-card receipt, and this idea does not reimplement them or inspect console firmware. Patent literature on signed session records and on trust models over player history (for example [WO2020051517A1](https://patents.google.com/patent/WO2020051517A1/en)) already treats telemetry as an integrity signal. Controller-input hash chains in this owner's ecosystem are [QorTroller](https://github.com/ConWan30/QorTroller). Using a gap to **drop** a label, rather than to mint a score, is the small move.

**Cost.** The input-gap file is $0. The scene note:

| Tier | Calls / match | Bill / match | Reservation / match | Week at 10 | Week at 25 |
|---|---:|---:|---:|---:|---:|
| Cheap: 1 crop × 3 challenge phases | 3 | $0.002 | $0.004 | $0.02 | $0.06 |
| Full: 5 frames × 3 phases | 15 | $0.011 | $0.020 | $0.11 | $0.29 |

A match with no challenges writes a sidecar `{status: "no_challenges"}` and spends $0. Both tiers fit in $10/week.

**Risk.** A free-text `notes` field parsed as "challenge passed" would become a second judge. The schema for this sidecar is an enum: `menu_like`, `scorebug`, `unknown`. `unknown` is the fail-closed value on a bad reply, a timeout, or a miss. The receipt challenge object is not opened for write.

## Combined cost

Cheap stack, expected,  no double count. Input-gap is $0 and is omitted.

| Piece | Week at 10 | Week at 25 |
|---|---:|---:|
| Idea 1 cheap witness | $0.76 | $1.90 |
| Idea 2 cheap burst | $0.06 | $0.15 |
| Idea 3 attestation | $0 | $0 |
| Idea 4 cheap clips, 8 per match | $0.06 | $0.15 |
| Idea 5 cheap learn, typical / every match proposes | $0.46 / $1.90 | $1.14 / $4.75 |
| Idea 6 cheap menu queue | $0.23 | $0.57 |
| Idea 7 cheap challenge note | $0.02 | $0.06 |
| **Stack, learn typical** | **$1.59** | **$3.97** |
| **Stack, learn proposes every match** | **$3.03** | **$7.58** |

Today's live watcher, if it also hits the current $0.50 game cap on every match, is $5.00 / $12.50 and is **not** inside the cheap stack. Healthy matches send far fewer calls because `known` frames are skipped. The cheap stack above is the new spend. Shared `W = 10` still covers the cheap stack at 25 matches plus a quiet watcher. It does not cover 25 matches that each burn a full $0.50 **and** a full candidate lab. That is a reason to keep per-idea stratum caps, not a reason to hard-code 10.

Full stack at the design points, without buying the same frame twice. Dense witness already returns `region`, so idea 6's extra scan is $0 when idea 1 full is on. Burst, clips, learn, and challenge notes stay extra.

| Piece | Week at 10 | Week at 25 |
|---|---:|---:|
| Idea 1 full, 75 min | $17.10 | $42.75 |
| Idea 2 full cap | $0.91 | $2.28 |
| Idea 4 full cap, 20 clips × 12 | $1.82 | $4.56 |
| Idea 5 no-cache upper bound | $27.36 | $68.40 |
| Idea 6 incremental | $0 | $0 |
| Idea 7 full | $0.11 | $0.29 |
| **Full stack** | **$47.30** | **$118.28** |

If the owner turns on the 180 min witness stop as well, add the difference between $4.10 and $1.71 per match ($23.94 at 10, $59.85 at 25). If the lab cache is warm, replace the learn row with something between $0 and the 1,600-call row ($12.16 / $30.40).

`W` for the full stack at 25 matches needs to be at least **$118.28** on expected bills, and higher if `authorise` is reserving `R` on a long failure streak. A missing `W` runs none of it.

## A/B before any default flips

Live flags (witness, burst ring): same card, same fps, flag off versus flag on, on real matches. Zero new wrong score reads, no missed locks, no extra drops, no extra audio lag, no reader stall. PR #47's measurement rules are the template. One match does not flip a default.

Post-match flags (attest, clip audit, lab, menu queue, challenge note, input gap): the recording process is not running; `receipt.json` bytes are identical before and after; `qortime verify` and `verify-bundle` outcomes are identical; highlight manifest bytes are identical. A flag that fails that check stays off.

---

# Part 3 — Specs for the top three

Top three: **idea 1 witness**, **idea 2 burst**, **idea 3 attestation with idea 4's clip audit as a state inside the same post-match tool**. Ideas 5–7 keep the stop rules in Part 2 and are not re-specified here. PR #46 remains the long spec for the learning loop; only `W` and `G` change.

Shared symbols:

| Symbol | Meaning |
|---|---|
| `W` | Weekly dollar ceiling. Read from `QORCAP_GLIMPSE_WEEKLY_LIMIT`, else `--weekly-limit`. |
| `G` | Per-match dollar ceiling. Read from `QORCAP_GLIMPSE_GAME_CAP`, else `--game-cap`. |
| `S_*` | Stratum ceiling inside `G`. A stratum cannot exceed `G`. |
| `R` | $0.00135 reservation. |
| `P` | $0.00076 expected. Used for planning. The gate uses `R`. |
| Fail closed | The call is not made. A record, if written, has `advisory_score: null`, `feeds_lock: false`, and a `refused` or `error` string. No retry storm: one failure counts toward the circuit. |

If `W` or `G` is missing, not a finite number, or ≤ 0, every `ask` returns `refused: cap_unconfigured` and the process does not fall back to 10 or to unlimited.

Ledger is the existing `ledger.jsonl`, with `kind` set to `witness`, `burst`, `clip_audit`, or the current `crop_live` / `td_second_opinion`. Week spend is one number across kinds.

## 3.1 Witness sampler

**Config when the flag is off.** No witness calls. `state_at == known` returns as it does today.

**Config, cheap.** Flag on, `witness_interval_s = 45`, `S_witness = 0.08` (fits 100 calls), `call_timeout_s = 12`, circuit 3 failures / 300 s, match time stop 180 min.

**Config, full.** Flag on, `witness_interval_s = 2`, `S_witness = G`, and the owner sets `G ≥ 1.72` (75 min) or `G ≥ 4.11` (180 min stop) and `W` from the table.

**Inputs.** Tap JPEG, `X-Qorcap-Frame-Wall-Ms`, `X-Qorcap-Frame-Seq`, timeline tail, ledger, flags, `W`, `G`, `S_witness`.

**Outputs.** Append-only `advisory.jsonl` record `kind: "witness"`. Optional crop JPEG. Ledger line. No other file.

### States

| State | Entry condition | Exit |
|---|---|---|
| `OFF` | Flag unset or not `1` | Stay. Known frames skipped. |
| `ARMED` | Flag on, match open, caps configured, tap fresh | `HOLD`, `RESERVE`, or a stop state |
| `HOLD` | Last witness call was under `witness_interval_s` ago, or confirm-hold is active for the **ordinary** path only | Back to `ARMED` on the next frame. Witness interval is its own clock. It does not reset the ordinary 6 s clock. |
| `RESERVE` | Interval elapsed, frame `known` or, on the ordinary path, unknown | `STOP_CAP` or `CALL` |
| `CALL` | `authorise(R)` passed | `RECORD` or `CIRCUIT` |
| `RECORD` | Reply, empty reply, HTTP error, or 12 s timeout | `ARMED` |
| `CIRCUIT` | 3 failures in a row | `ARMED` after 300 s. During the open window, `refused: circuit_open` and no socket. |
| `STOP_CAP` | `game_spent + R > G` or `stratum_spent + R > S_witness` or `week_spent + R > W` | Process keeps polling so it can still exit on idle, and it does not call the model. |
| `STOP_TIME` | 180 min, or QorCap unreachable for the configured idle | Process exit. Recording is not signalled. |
| `STOP_MATCH` | Bundle header names a different match | Exit. One watcher per match, as today. |

```mermaid
stateDiagram-v2
  [*] --> OFF
  OFF --> ARMED: QORCAP_GLIMPSE_WITNESS is 1 and W and G are finite and positive
  ARMED --> HOLD: witness interval not elapsed
  HOLD --> ARMED: next fresh frame
  ARMED --> RESERVE: interval elapsed and frame is known
  RESERVE --> STOP_CAP: spent plus R exceeds G or S_witness or W
  RESERVE --> CALL: authorise R succeeded
  CALL --> RECORD: reply or timeout or HTTP error
  CALL --> CIRCUIT: third consecutive failure
  CIRCUIT --> ARMED: cooldown elapsed
  RECORD --> ARMED: record written feeds_lock false
  STOP_CAP --> ARMED: only if a new match id and new caps
  ARMED --> STOP_TIME: 180 min or QorCap idle
  ARMED --> STOP_MATCH: bundle id changed
  STOP_TIME --> [*]
  STOP_MATCH --> [*]
```

### Guards

A transition into `CALL` requires all of:

1. Flag is `1`.
2. `W` and `G` and `S_witness` are finite and `> 0`, and `S_witness ≤ G`.
3. `game_spent + R ≤ G`, `witness_spent + R ≤ S_witness`, `week_spent + R ≤ W`.
4. Frame age from the tap header is ≤ 3 s. The watcher does not call `GET /snapshot.jpg`.
5. `state_at` is `known` for this stratum. Unknown frames stay on today's path and today's `S` (the remainder of `G`).
6. The user text contains the exact lock phrase `not given for this crop` and does not contain the decimal representation of the locked away or home score.
7. Output path is outside the match folder. The audit hook is installed.
8. This `frame_seq` has not already been sent.

Any failed guard: stay in `ARMED` or go to `STOP_CAP`. Do not call.

### Outputs of `RECORD`

```text
tag = ADVISORY_AI
ai_assisted = true
advisory_only = true
feeds_lock = false
kind = witness
prompt_sha, model, role_version, tool_version
reference_lock = the timeline lock, stored beside the reply, not sent
model_reply, flags, cost, gen_id, refused, error
```

`assess` still runs, so a stats overlay cannot become `advisory_score`. The grader downstream may also look at raw `model_reply`. The grader is post-match and is not this loop.

### Stops, timeouts, fail-closed

| Condition | Behavior |
|---|---|
| Cap would be exceeded by `R` | `STOP_CAP` before the socket. |
| Timeout 12 s | Record `error`, ledger books `R`, increment failure count. |
| Bad JSON or extra keys | `valid: false`, `advisory_score: null`. Not a witness score. |
| Circuit open | No call for 300 s. |
| Key missing | `refused: no_key`. Frames counted. No socket. |
| Cap unconfigured | `refused: cap_unconfigured`. No socket. |
| Model says a score | Stored as advisory. The reader is not notified. No file the reader polls is written. In particular this path does not append `td_verdicts.jsonl`. |

**Stop condition for the match:** time stop, idle stop, match change, or the operator closing the process. Hitting the cap stops calls and leaves recording alone.

### Invariants

1. With the flag off, the sequence of model calls on a given timeline and tap fixture equals the sequence `glimpse-watch/0.1.3` produces.
2. Every `kind: witness` record has `feeds_lock: false` and `advisory_only: true`.
3. The user message for a witness call does not contain the locked score's numerals.
4. After a witness run, the match folder's file set is unchanged by the watcher (timeline growth comes from the recorder).
5. `receipt.json` bytes are identical before and after.
6. `core.touchdown.veto` remains `None` when `QORCAP_GLIMPSE_TD_VETO` is unset.
7. `game_spent` never exceeds `G + R` for a single in-flight call, and it never exceeds `G` once that call is booked at the actual cost or at `R` on failure. The pre-check is `spent + R ≤ G`.
8. A witness record is not a label source: a loader that implements idea 5's guard rejects `kind: witness` as truth.

### Tests

| Test | Enforces |
|---|---|
| `test_witness_flag_off_sends_no_known_frames` | Invariant 1 |
| `test_witness_prompt_withholds_lock_digits` | Invariant 3, guard 6 |
| `test_witness_record_is_advisory_only` | Invariant 2 |
| `test_witness_stops_before_call_when_reservation_would_exceed` | Invariant 7, `STOP_CAP` |
| `test_witness_missing_cap_refuses` | Unconfigured cap |
| `test_witness_timeout_books_worst_case_and_opens_circuit` | Timeout and circuit |
| `test_witness_does_not_write_match_folder_or_td_verdicts` | Invariants 4 and 6, and the veto file |
| `test_witness_stats_overlay_has_null_advisory_score` | Stats never become a score |
| `test_witness_receipt_bytes_unchanged` | Invariant 5 |

## 3.2 Disagreement burst

**Purpose.** Preserve pixels around a disagreement and buy at most a few extra blind reads. Never touch a lock.

**Config, cheap.** `QORCAP_GLIMPSE_BURST=1`, ring flag off, `max_events = 8`, `crops_per_event = 1`, `S_burst = 0.02`, event max age 15 s.

**Config, full.** Both flags on after the capture A/B, `max_events = 30`, `crops_per_event = 4`, `S_burst = 0.17`, ring depth 8 frames or 16 s, whichever is smaller.

**Trigger** (any one):

- A witness or ordinary `SCOREBUG` reply has both scores, and the pair is not the reference lock.
- `assess` flags an implausible transition (`score_decrease_vs_lock`, `sides_swapped_vs_lock`, `both_sides_changed_vs_lock`, a non-football delta, `plus1_without_td`).
- A `td_jumps.jsonl` event is `pending`.

**Not a trigger:** `STATS_OVERLAY`, `rejected_or_unknown`, `no_read`, `agrees_with_lock`, a second trigger with the same `frame_seq` or the same touchdown `id`.

### States

| State | Meaning |
|---|---|
| `OFF` | Flag off. Disagreements stay inside today's `advisory.jsonl` line and nowhere else. |
| `RING` | Flag on. Hold the last frames. Ring flag off: only JPEGs the watcher already decoded. Ring flag on: `FrameTap` keeps up to 8 `Arc` clones, `try_lock`, drop on contention. |
| `TRIGGERED` | A trigger fired and the event age is ≤ 15 s and `events_used < max_events`. |
| `EXPIRED` | Age > 15 s. Increment `td_expired`-style counter `burst_expired`. No call. |
| `WRITE` | Write JPEGs under the Glimpse `burst/` directory. Match folder untouched. |
| `RESERVE` | Same cap math as the witness, against `S_burst`, `G`, and `W`. |
| `OPINE` | One or more unhinted calls, at most `crops_per_event`, on ring frames nearest the event time. Dedup by `frame_seq`. |
| `SEAL` | Append `burst.jsonl`. `feeds_lock: false`. |
| `STOP_CAP` | Reservation would exceed. `SEAL` a row `refused: budget` with the JPEGs still on disk if `WRITE` already happened. No further events this match. |
| `STOP_TIME` | Same 180 min / idle stops as the watcher. |

```mermaid
stateDiagram-v2
  [*] --> OFF
  OFF --> RING: QORCAP_GLIMPSE_BURST is 1 and caps configured
  RING --> RING: frame cloned or skipped on try_lock
  RING --> TRIGGERED: disagreement or pending touchdown and under event cap
  TRIGGERED --> EXPIRED: event older than 15 s
  EXPIRED --> RING: no model call
  TRIGGERED --> WRITE: JPEGs to Glimpse burst directory
  WRITE --> RESERVE: crops still allowed for this event
  RESERVE --> STOP_CAP: spent plus R exceeds S_burst or G or W
  RESERVE --> OPINE: authorise R succeeded
  OPINE --> SEAL: reply timeout or schema reject
  SEAL --> RING: feeds_lock false and not td_verdicts
  STOP_CAP --> [*]
```

### Guards

Into `OPINE`:

1. Burst flag on, caps configured, `S_burst ≤ G`.
2. `authorise(R)` passes for this crop.
3. Prompt lock phrase is `not given for this crop`.
4. Destination path fails `forbidden_read` and is not under the match directory.
5. This path does not open `td_verdicts.jsonl` for write or read.
6. Ring flag on implies the offer implementation uses `try_lock` and returns without waiting. A unit test holds the mutex and asserts `offer` returns.

Into `WRITE`: disk free under the Glimpse root is at least 1 GiB, else skip JPEGs, record `refused: disk`, and do not delete anything in the match folder.

### Fail-closed

| Case | Result |
|---|---|
| Schema reject or timeout | `verdict: no_read` in `burst.jsonl`. JPEGs kept. No second try beyond `crops_per_event`. |
| Ring mutex busy | That frame is absent from the ring. The reader copy is unchanged. |
| Trigger during `STOP_CAP` | Count `burst_dropped`. No call. |
| Veto env is `1` | This tool still does not write `td_verdicts.jsonl`. The old watcher path is untouched and remains out of scope. The burst does not become a veto. |

### Invariants

1. Flag off: no `burst/` directory is created, and `FrameTap.offer` does not allocate a ring.
2. Every `burst.jsonl` row has `feeds_lock: false`.
3. No row is written to `td_verdicts.jsonl` by this component.
4. A pending touchdown still posts or cancels on the reader's own hold rules when the veto flag is off, whether the burst agrees, disagrees, or expires.
5. `offer` while the tap mutex is held does not block (existing test `a_held_tap_never_blocks_the_reader` extended to the ring).
6. Match-folder bytes other than the recorder's own appends are unchanged.
7. Event count never exceeds `max_events`, and crops per event never exceed `crops_per_event`.

### Tests

| Test | Enforces |
|---|---|
| `test_burst_flag_off_creates_nothing` | Invariant 1 |
| `test_burst_ring_try_lock_does_not_block_reader` | Invariant 5 |
| `test_burst_does_not_write_td_verdicts_or_match_folder` | Invariants 3 and 6 |
| `test_burst_expired_event_makes_no_call` | `EXPIRED` |
| `test_burst_cap_seals_budget_refusal` | `STOP_CAP` |
| `test_burst_one_event_respects_crop_cap` | Invariant 7 |
| `test_burst_disagreement_does_not_change_fast_path_post` | Invariant 4 |
| `test_burst_prompt_withholds_lock_digits` | Guard 3 |
| `test_burst_bad_json_is_no_read` | Fail-closed schema |

The capture A/B for the ring flag is the PR #47 procedure, named here as `capture_ab_burst_ring`: zero new wrong reads, no extra drops, no extra lag, against the same footage with the ring flag off.

## 3.3 Post-match attestation and clip audit

One process, two flags. Attestation can run with clip audit off (signature over whatever witness and burst files exist). Clip audit can run with attestation off (the audit JSON is still written, `signed: false`). Neither runs during recording.

**Cheap.** Audit 1 frame per clip, `max_clips = 15`, `frames_per_clip = 1`, `S_clip = 0.03`. Then sign.

**Full.** `frames_per_clip = 12`, `max_clips = 20`, `S_clip = 0.33`. Then sign.

**Inputs.** Match folder read-only: `receipt.json`, `highlights/manifest.json`, clip files, `checkpoints.jsonl` or `score_checkpoints.jsonl`. Glimpse folder read-write: `advisory.jsonl`, `burst.jsonl`, crops. Observer key at `%LOCALAPPDATA%\QorCap\keys\glimpse-observer.key`. Caps `W`, `G`, `S_clip`.

**Outputs.** `highlights_audit.jsonl`, `glimpse_attestation.json`, ledger lines `kind: clip_audit`. No write to `receipt.json`, `manifest.json`, clip bytes, `.c2pa`, or `td_verdicts.jsonl`.

### States

| State | Guard | On failure |
|---|---|---|
| `REFUSE_RECORDING` | `recording.lock` mtime newer than 30 s | Exit 3. No files. Same number the highlight tool uses. |
| `NEED_RECEIPT` | `receipt.json` exists and parses as JSON | Write attestation `{status: "receipt_absent", signed: false}` and stop. Do not invent a receipt. |
| `PIN` | sha256 of receipt bytes, sha256 of highlight manifest if present, checkpoint chain head if the file parses | A parse failure of the chain records `chain: unreadable` and continues. The receipt hash is the pin. |
| `CLIP_OFF` | Clip flag off | Skip to `SEAL`. |
| `CLIP_NEXT` | More clips, `clips_done < max_clips` | Else `SEAL`. |
| `CLIP_RESERVE` | `authorise(R)` against `S_clip`, `G`, `W` | This clip and the rest get `status: not_run`, `reason: budget`. Then `SEAL`. |
| `CLIP_CALL` | ffmpeg extracts the frame from the clip file named by the manifest. Timeout 12 s on the model. Prompt unhinted. | `no_read` for that frame. Continue until `frames_per_clip` or the cap. |
| `CLIP_COMPARE` | Local compare of parsed scores to the manifest lock. The lock was not in the prompt. | `disagree` / `agree` / `no_read`. |
| `SEAL` | Build the attestation body. | |
| `SIGN` | Key file exists, and its public key is not the device key and not the referee key | `{signed: false, reason: "no_observer_key"}` or `reason: "key_collides_with_authority"`. Exit 0 so a missing key is visible and not a signed lie. |
| `COPY` | `QORCAP_GLIMPSE_ATTEST_COPY=1` | Copy the sidecar into the match folder under a fixed name `glimpse_attestation.json`. Default: do not copy. |
| `DONE` | | Exit 0. |

```mermaid
stateDiagram-v2
  [*] --> REFUSE_RECORDING
  REFUSE_RECORDING --> NEED_RECEIPT: recording.lock stale or absent
  REFUSE_RECORDING --> [*]: fresh lock exit 3
  NEED_RECEIPT --> PIN: receipt.json parses
  NEED_RECEIPT --> [*]: status receipt_absent unsigned
  PIN --> CLIP_OFF: clip flag off
  PIN --> CLIP_NEXT: clip flag on
  CLIP_OFF --> SEAL
  CLIP_NEXT --> CLIP_RESERVE: clip remains and under max_clips
  CLIP_NEXT --> SEAL: no more clips
  CLIP_RESERVE --> SEAL: cap would be exceeded remaining clips not_run
  CLIP_RESERVE --> CLIP_CALL: authorise R succeeded
  CLIP_CALL --> CLIP_COMPARE: reply timeout or reject becomes no_read
  CLIP_COMPARE --> CLIP_NEXT: row appended feeds_lock false
  SEAL --> SIGN
  SIGN --> COPY: signed or explicit unsigned status
  COPY --> DONE
  DONE --> [*]
```

There is no state that attaches Content Credentials. A future credential tool is a different program. Its preconditions are the clip bytes and `highlights/manifest.json`. It does not read `highlights_audit.jsonl` to decide attachment, and it does not block on this process. If this process is still in `CLIP_CALL`, credentials proceed. The audit records the clip sha256 it actually read so a later human can see whether the bytes match the credentialled bytes.

### Attestation body (sidecar schema `qorcap-glimpse-attestation/v0`)

Required keys: `schema`, `status`, `signed`, `match_id`, `receipt_sha256`, `observer_key_id`, `created_ms`, `artifacts`.

`artifacts` lists `{name, sha256, bytes}` for `advisory.jsonl`, `burst.jsonl`, `highlights_audit.jsonl`, and the highlight manifest, each omitted if the file was absent (`sha256: null`, `present: false`).

`status` is one of `observed`, `receipt_absent`, `unsigned`. `observed` is allowed only when `signed` is true.

Forbidden keys: `away_score`, `home_score`, `winner`, `trust_tier`, `signatures` in the receipt's shape. The statement does not carry a score. Scores stay in the audit JSONL as advisory rows.

Signature: Ed25519 over RFC 8785 JCS of the body without the signature object, the same canonicalization family as receipts, with `key_kind: glimpse-observer`. Verifiers that only implement `qor-receipt/v0` never see this file.

### Stops and fail-closed

| Condition | Behavior |
|---|---|
| Fresh recording lock | Exit 3, no writes. |
| Receipt missing | Unsigned `receipt_absent`. No model calls. |
| Cap unconfigured and clip flag on | No model calls. Attestation may still sign existing files if `W` is unconfigured **only when** there is nothing to buy. If any call might be made, refuse the calls. Signing local hashes stays allowed at $0. |
| Model timeout or bad JSON | `no_read` for that frame. |
| Clip ffmpeg failure | `no_read`, `reason: extract_failed`. Next clip. |
| Key collides with device or referee | Do not sign. |
| Copy flag off | Match folder unchanged. |

Timeouts: 12 s per model call, 60 s per ffmpeg extract, 10 min wall clock for the whole tool. On the wall-clock stop, `SEAL` whatever rows exist, sign that partial file, set `status` only if the signature succeeds, and list `partial: true`.

### Invariants

1. `receipt.json` sha256 is unchanged by the process.
2. `highlights/manifest.json` sha256 is unchanged.
3. No clip file's sha256 changes.
4. No `.c2pa` file is created.
5. No `td_verdicts.jsonl` is created or appended.
6. Every audit row has `feeds_lock: false` and `kind: clip_audit`.
7. The user text for a clip call does not contain the manifest lock numerals.
8. `signed: true` implies the public key is neither the device key nor the referee key, and the JCS signature verifies.
9. `status: observed` implies `signed: true` and `receipt_sha256` matches the file.
10. A fresh `recording.lock` prevents every write.
11. Clip calls stop before the socket when `spent + R` would exceed `S_clip` or `G` or `W`.
12. The receipt's verify result is not an input to signing. A FAIL receipt can still be attested as "the observer saw these bytes." The attestation does not say PASS.

### Tests

| Test | Enforces |
|---|---|
| `test_attest_refuses_while_recording_lock_fresh` | Invariant 10 |
| `test_attest_receipt_and_manifest_bytes_unchanged` | Invariants 1 and 2 |
| `test_attest_does_not_create_c2pa_or_touch_clips` | Invariants 3 and 4 |
| `test_attest_does_not_write_td_verdicts` | Invariant 5 |
| `test_clip_audit_row_is_advisory_only` | Invariant 6 |
| `test_clip_audit_prompt_withholds_lock_digits` | Invariant 7 |
| `test_attest_rejects_authority_key` | Invariant 8 |
| `test_attest_observed_requires_signature_and_receipt_pin` | Invariant 9 |
| `test_clip_audit_stops_when_reservation_would_exceed` | Invariant 11 |
| `test_attest_signs_a_failing_receipt_without_claiming_pass` | Invariant 12 |
| `test_attest_missing_receipt_is_unsigned_absent` | `NEED_RECEIPT` |
| `test_clip_audit_bad_json_is_no_read` | Fail-closed parse |
| `test_attest_copy_flag_off_leaves_match_folder` | Default copy off |
| `test_credentials_tool_is_not_invoked` | No C2PA writer exists; this test imports the new module and asserts it has no credential attach function |

Idea 5's stop rules, restated so the learning loop is not the exception: post-match only, `recording.lock` refuses, `W` and `G` unconfigured refuse calls, labels cannot name Glimpse, `PENDING_APPROVAL` is the only promotion state, and the pinned prompt in `qorcap_glimpse_watch.py` changes only in a later PR.

---

# Recommendation

Build **idea 1, the unhinted witness**, first, at the cheap tier (100 blind crops per match, about $0.08, well inside $10/week at 25 matches). Pair it with **idea 2's cheap burst** so a disagreement keeps the frames, and with **idea 3** so the sidecar is signed after the match. That trio is what makes Glimpse serve the receipt: a blind pixel reading of the locks that were actually committed, evidence when it disagrees, and a signature that is not the referee's.

Leave the full tiers off until the owner picks `W` and `G` from the tables. The 75-minute dense witness alone is about $17/week at 10 matches and about $43/week at 25. The full stack with a cold learning loop is about $47 and $118. The loop code should refuse to run if those numbers were not set.

Do not turn on `QORCAP_GLIMPSE_TD_VETO`. Do not put any of these records in `receipt.json`.
