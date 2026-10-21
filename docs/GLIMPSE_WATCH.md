# Glimpse shadow watcher (advisory, shadow-only)

QorCap's local scorebug reader owns every score lock. The Glimpse watcher is a **live second opinion** that
is labelled `ADVISORY_AI` / AI-assisted and is never read by QorCap, the referee, the receipt or the bundle.

## Frame source: `GET /frame/scorebug` (opt-in, read-only)

The watcher must never open `.qorcap-run/snapshot.jpg` or `inputs.jsonl` while recording (an outside reader
holding the snapshot open made ffmpeg's atomic rename fail). So QorCap hands it a frame from memory instead:

* Off unless QorCap starts with `QORCAP_GLIMPSE_FRAME=1` (or `record --glimpse-frame`). Off: `404`.
* The tap stores the bytes the scorebug reader already pulled through `GET /snapshot.jpg` (4 fps). It opens **no
  file**, starts no thread, decodes nothing. The reader's request stores the frame with `try_lock`, so it never
  waits on the watcher; the watcher's request only clones an `Arc`. A slow, crashed or absent watcher cannot affect
  capture, the snapshot rename, or the reader.
* One frame per 2 s across all callers (`429` + `Retry-After`); a frame older than 3 s is refused (`503`), so a
  paused or stopped reader is never presented as live. Headers: `X-Qorcap-Frame-Wall-Ms`, `X-Qorcap-Frame-Seq`,
  `X-Qorcap-Bundle`. GET only: nothing can be sent back through it. The full frame is returned; the watcher crops.

## What the watcher does (`sidecar/qorcap_glimpse_watch.py`)

* Reads the match folder's append-only `timeline.jsonl` (open, read new bytes, close) and nothing else there. An
  in-process audit hook refuses any open of `.qorcap-run/*`, `snapshot.jpg`, `inputs.jsonl`, any other match file,
  and any write/rename/delete inside the match folder.
* Sends a frame to Glimpse **only** when the reader has no fresh agreed row for it (score unknown: no row in the
  2.5 s before the frame) or its row has `quarter: null` (quarter unknown). Known frames are never sent.
* Glimpse gets the scorebug crop, the layout (`regular` / `primetime` / `monday_night` / `london`) and the last
  locked score. Prompt, schema and role are glimpse-shadow v0.1.0's, pinned by sha256, plus a short live context.
* Strict schema: prose, fences, extra keys or bad JSON are recorded as unknown. A stats box stays a stats read and
  never yields a score. A score suggestion is **plausible** only if it equals the last lock or is one legal football
  step from it (single side, +1/2/3/6/7/8, +1 only after a +6, no decrease, no swapped sides).
* Budget: $0.50 per game (worst case checked before each call; failed calls are booked at worst case), $10/week
  (OpenRouter `/key` usage_weekly, the local ledger and a floor; +$0.50 margin when `/key` is unreadable), at most one
  request per 6 s, none for 20 s after Glimpse confirms the lock (unless the lock changes), 15 s after three "no scorebug" replies in a row, near-identical crops deduped for 30 s, 500 calls.
* Output (default `%LOCALAPPDATA%\QorCapGlimpse\<match>\`): `advisory.jsonl`, live `REPORT.md` with the game cost,
  `watch.log`, `crops/`. Ledger: `%LOCALAPPDATA%\QorCapGlimpse\ledger.jsonl`. The key comes from
  `OPENROUTER_API_KEY` only and is never printed.

## Run (Windows, after installing a QorCap build that has the tap)

```bat
setx QORCAP_GLIMPSE_FRAME 1
setx OPENROUTER_API_KEY <key>          (once; open a new window afterwards)
Start Match.cmd                        (as usual)
"Glimpse Watch.cmd" monday_night       (any time after recording starts; runs minimized, below-normal priority)
```

It stops by itself when QorCap has been unreachable for 5 minutes, after 180 minutes, or when the game cap is
reached. Closing its window stops it at any time; recording is unaffected either way.

## Automatic start with Start Match (v0.3.22)

When the user environment variable `QORCAP_GLIMPSE_FRAME` is `1`, `start-match.ps1` starts the
watcher by itself just before recording begins:

- detached (`Start-Process`), minimized window, `BelowNormal` priority — it never blocks or slows capture;
- `--layout auto` (override with user env `QORCAP_GLIMPSE_LAYOUT`, e.g. `thursday_night`);
- `--wait-start-s 900`: it waits up to 15 min for the first scorebug frame, then exits;
  `--idle-exit-s 60`: it exits a minute after frames stop; it also stops if a different match starts;
- `OPENROUTER_API_KEY` is read from the user environment only for the watcher's launch and is never printed;
- any failure prints one line ("not started …; the match is unaffected") and the match continues.

Set `QORCAP_GLIMPSE_FRAME=0` (or remove it) to turn both the frame tap and the watcher off.
Caps stay in force: $0.50 per game, $10 per week (`budget.py`). Glimpse remains ADVISORY_AI only.

## Second opinion on touchdown jumps (v0.3.23, watcher 0.1.3) — shadow only

QorCap's fast path can lock a +6/+7/+8 from a few reads (held 1 s, 2 s for look-alike digits). While a jump is
held, the live sidecar appends it to `td_jumps.jsonl` in the watcher's own folder (never the match folder). The
watcher reads the next frame with Glimpse **without** any score hint and logs a `td_second_opinion` record
(`agree`, `disagree_old`, `disagree_other`, `no_read`) to `advisory.jsonl`, plus `td_verdicts.jsonl`. Same model,
prompts, budget and caps as every other Glimpse call ($0.50/game, $10/week). Nothing is fed back: in shadow mode
QorCap never reads the verdicts.

**Enforcing mode** (`QORCAP_GLIMPSE_TD_VETO=1`) is OFF by default and needs the owner's explicit OK before it is
turned on. When on, a held fast-path lock waits up to 4 s for the verdict: `disagree` cancels that fast-path lock
(the normal 3/9-read windows still apply), `agree` posts it, no verdict posts it after the wait. Glimpse can only
veto or delay a fast-path lock; it can never create a lock, and offline finalize never uses it.

## Witness, burst, attestation (cheap tier, all default off)

Separate flags, off unless set to `1`: `QORCAP_GLIMPSE_WITNESS`, `QORCAP_GLIMPSE_BURST`,
`QORCAP_GLIMPSE_BURST_RING`, `QORCAP_GLIMPSE_ATTEST`, `QORCAP_GLIMPSE_CLIP_AUDIT`. Start Match does not enable
them. Flag-off output matches today's watcher. See [GLIMPSE_WITNESS.md](GLIMPSE_WITNESS.md). The loop specs are
in [PR #50](https://github.com/ConWan30/qorcap/pull/50).
