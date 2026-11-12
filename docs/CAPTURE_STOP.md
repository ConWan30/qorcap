# Capture stop record (practice lane)

`capture_stop.v0.json` sits beside the bundle on the practice lane only (`--practice-lane` or
`QORCAP_LANE=practice`). REAL MATCH and every other run write nothing new, and their stop
checkpoint is unchanged. The capture command, the encode settings and the video bytes are unchanged.

## What it holds

One record per capture writer (the capture ffmpeg that reads the card and encodes), in order:

- **writer**: `ended_by` (`self_exit`, `qorcap_kill`, `spawn_failed`, or `UNKNOWN`), the OS
  `exit_code` (Windows reports ENOSPC as -28; `UNKNOWN` when there is no code), `kill_sent`
  (`stop`, `watchdog_no_data`, `stdout_closed`, or null), and `stderr_drained`. A writer that closes
  stdout on its own gets up to 2 s to exit before QorCap kills it, so its own exit code is kept.
- **encoder**: the last `-progress` block's `frame`, `dup` and `drop`, `end_seen` (ffmpeg's closing
  `progress=end` block arrived), and `counters_basis` (`end_block`, `last_progress_block`, `none`).
  QorCap ends the writer with a kill on a normal stop, so `end_seen` is usually false there and the
  counters stop at the last block, about 1 s before the end.
- **input_drops**: DirectShow "real-time buffer ... frame dropped!" warning lines, counted apart from
  the encoder counters (those frames never reach the encoder). Video and audio lines are split, and
  `Last message repeated N times` after a warning is summed. `dropped_frames` stays `UNMEASURED`:
  one line per frame was not checked.

At stop, a **frame-clock gap check** runs per part on `frame_clock.jsonl`, because ffmpeg cannot see
frames lost before it: the rate measured from the modal PTS step, PTS gaps above 3/2 of that step
(and the missing slots), arrival gaps above 3 frame periods, and the largest of each. Thresholds are
PROVISIONAL. No frame clock, or fewer than 10 PTS steps for a part: `UNMEASURED`.

Anything not seen is `UNMEASURED` or `UNKNOWN`, never 0.

## Binding

The file is written as each writer ends (`state: open`, not bound), so a crash still leaves the
writers that already ended. At stop it is rewritten with `state: stop` and its SHA-256 goes into the
`stop` checkpoint's evidence as `capture_stop_sha256` with `capture_stop_file`, the same way
`pad_link.v0.json` is bound. The checkpoint's `frames_hash` commits that evidence and the checkpoint
chain links it. `qorcap verify-bundle` re-hashes the file when the stop checkpoint names it.

Not covered: the fMP4 remux ffmpeg's exit status; a crash before the stop checkpoint (the `open`
file is not bound); recordings made before this change.
