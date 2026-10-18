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
