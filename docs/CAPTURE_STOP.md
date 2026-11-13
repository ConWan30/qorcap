# Capture stop record (practice lane)

`capture_stop.v0.json` sits beside the bundle on the practice lane only (`--practice-lane` or
`QORCAP_LANE=practice`). REAL MATCH and every other run write nothing new, and their stop
checkpoint is unchanged. The capture command, the encode settings and the video bytes are unchanged.

## What it holds

One record per capture writer (the capture ffmpeg that reads the card and encodes), in order:

- **writer**: `ended_by` (`self_exit`, `qorcap_kill`, `spawn_failed`, or `UNKNOWN`), the OS
  `exit_code` (Windows reports ENOSPC as -28; `UNKNOWN` when there is no code), `killed_by_qorcap`,
  `kill_sent` (`stop`, `watchdog_no_data`, `stdout_closed`, `read_error`, or null), and
  `stderr_drained`. When QorCap killed the writer, the OS status is the kill's, not the writer's
  (Windows `TerminateProcess` exits with 1; a Unix signal gives no code), so `exit_code` is
  `UNKNOWN` and the OS value is kept only as `exit_code_raw`, with `exit_code_note`. A writer whose
  stdout ended (EOF: `stdout_closed`; a read error: `read_error`) gets up to 2 s to exit before
  QorCap kills it, so its own exit code is kept when it exits in time.
- **encoder**: the last `-progress` block's `frame`, `dup` and `drop`, `end_seen` (ffmpeg's closing
  `progress=end` block arrived), and `counters_basis` (`end_block`, `last_progress_block`, `none`).
  QorCap ends the writer with a kill on a normal stop, so `end_seen` is usually false there and the
  counters stop at the last block, about 1 s before the end.
- **input_drops**: DirectShow "real-time buffer ... frame dropped!" warning lines, counted apart from
  the encoder counters (those frames never reach the encoder). Video and audio lines are split, and
  `Last message repeated N times` in a run that started at a warning is summed into
  `repeated_after_warning_lower_bound`. `-progress` keys are written outside ffmpeg's log, so they do
  not end the run. A stats line is treated as ending it; whether ffmpeg prints that line through its
  log at the capture's `-loglevel` is UNKNOWN for the pinned build, and if not, this can only
  under-count. It is a **lower bound**: ffmpeg prints a pending repeat count
  only when its next different log message arrives, so a run still pending when the writer ends is
  never printed. `dropped_frames` stays `UNMEASURED`: one line per frame was not checked.

At stop, a **frame-clock gap check** runs per part on `frame_clock.jsonl`: the rate measured from the
modal PTS step, PTS gaps above 3/2 of that step (and the missing slots), equal or backwards PTS steps
(`pts_nonincreasing`), arrival gaps above 3 frame periods, and the largest of each. Thresholds are
PROVISIONAL. No frame clock, or fewer than 10 consecutive PTS steps for a part: `UNMEASURED`.

**What it cannot see.** The check reads the encoder's *output* PTS. The capture runs
`-vf fps=N ... -fps_mode cfr` (`src/ffmpeg.rs`), so input-side loss is filled with duplicate frames
before the encoder and leaves no PTS hole; those duplicates are not in ffmpeg's `dup` counter
either. **`pts_gaps: 0` does not mean no drops**, and the check cannot see frames lost before
ffmpeg. Every part carries this as `pts_gap_note`. Arrival gaps can show delivery stalls, not loss.
`input_drops.dropped_frames` stays `UNMEASURED`.

Anything not seen is `UNMEASURED` or `UNKNOWN`, never 0.

## Binding

The file is written as each writer ends (`state: open`, not bound), so a crash still leaves the
writers that already ended. At stop it is rewritten with `state: stop` and its SHA-256 goes into the
`stop` checkpoint's evidence as `capture_stop_sha256` with `capture_stop_file`, the same way
`pad_link.v0.json` is bound. The checkpoint's `frames_hash` commits that evidence and the checkpoint
chain links it. `qorcap verify-bundle` re-hashes the file when the stop checkpoint names it. A record
file that no checkpoint binds (the stop write failed, a crash, a stale `state: open` file) is printed
as `WARN: ... UNBOUND, not evidence`; it is not an error, so bundles without a record still pass.
The unbound file is untrusted: only `state` `open` or `stop` is echoed, anything else prints as
`UNKNOWN`, so its text cannot add lines to the output. A leftover `capture_stop.v0.json.tmp` (an
interrupted write) is also a `WARN`.
Readers should use the record only when `state` is `stop` and the stop checkpoint binds its SHA-256.

The referee countersigns the stop checkpoint **only when a referee is attached**. Practice runs
usually have none, so there the record is chained and sealed by the local chain but not countersigned.

Not covered: the fMP4 remux ffmpeg's exit status; a crash before the stop checkpoint (the `open`
file is not bound); recordings made before this change.

## Known limits (documented, not fixed here)

- **Tail rewrite.** Editing the file *and* rewriting the stop checkpoint (the chain tail) passes
  `verify-bundle`; only a signature over the tail hash (receipt or referee countersign) catches it.
  This is the existing trust model, the same as `pad_link.v0.json`.
- **Reopen delay.** On the practice lane, a writer that ended stdout and lingers can delay the
  reopen by up to about 4 s (up to 2 s exit grace plus up to 2 s stderr drain). The lane off has no
  grace.
- **Env lane.** The lane is a flag or `QORCAP_LANE=practice` only. A persistent user env
  `QORCAP_LANE=practice` would turn this on during a Start Match run; nothing refuses it there.
- **Test Without Card.** `Test Without Card.cmd` does not set the lane, so practice runs through it
  get no record unless `QORCAP_LANE=practice` is set by hand. Launch scripts are unchanged here.
- **Windows engine test.** The stubbed-writer engine tests are Unix-only, so the Windows kill status
  is covered by unit tests only.
