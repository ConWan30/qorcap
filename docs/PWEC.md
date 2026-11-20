# Press-window engagement capture (PWEC): engagement window index (info only)

After a **practice** Call of Duty match stops, Start Match writes `engagement_windows.jsonl` and
`engagement_windows.summary.json` to `%LOCALAPPDATA%\QorCapEngagement\<match>\`, outside the match
folder (the same pattern as the Glimpse watcher's `QorCapGlimpse`). It reads only the two files
QorCap already wrote, `inputs.jsonl` and `frame_clock.jsonl`. No pixels, no model, no network, $0.
Module: `sidecar/qorcap_engagement.py`. Plan: PLAN-press-window-engagement-capture.md (sha256 ff4a12fb...),
ONE RECOMMENDATION and section 3b.

**Info only. Never Counted.** Nothing in these files feeds a receipt, a lock, a timeline, a referee
submit, an upload or any Counted value. `receipt.v0.json`, `contracts/`, `schema/`, `src/` and
REAL MATCH are unchanged.

## When it runs

- `QORCAP_LANE=practice` **and** game set `cod`. `QORCAP_PWEC=0` (or false/no/off) turns it off there.
- REAL MATCH, Madden and any run without `QORCAP_LANE=practice` write nothing and print nothing.
  `QORCAP_PWEC=1` alone does not turn it on. A leftover user env `QORCAP_LANE=practice` still turns
  the sidecar on for a Start Match Call of Duty run, including one intended as REAL MATCH (the same
  pattern already noted in `docs/CAPTURE_STOP.md`). That path still writes only the info-only files
  outside the match folder and cannot change the receipt.
- The hook is `persist_stats`, immediately after `maybe_write_cod_reader_stats`. That is the `finally`
  of `run_with_reader_stats(save_tail, persist_stats)`: after the seal, the countersign and the signed
  receipt. It is not inside `maybe_write_cod_reader_stats` itself. Any error prints one line and is
  swallowed; the save result and exit code do not change.
- `QORCAP_PWEC_OUT` overrides the output root. A root inside the match folder (or one that contains it)
  is refused. The match folder's leaf name must be a single name: empty, `.`, `..`, an absolute name,
  or a name with a path separator is refused so the output cannot escape the root.
- Offline: `python sidecar/qorcap_engagement.py --match-dir <match folder> [--out <root>] [--match-b4-fail]`.
- An `inputs.jsonl` that exists but does not parse (no valid `in` or `hb` row) writes no index. The
  reason is logged. Status is UNKNOWN; a 0-window file is not written, so it cannot be read as a quiet match.

## What a window is (controller input only)

| setting | value |
|---|---|
| held | trigger value > 60 (rows are logged on change) |
| merge gap | holds of R2 and L2 within 500 ms of the running end join one episode |
| core | first trigger down to last trigger up |
| window | core start - 500 ms to core end + 1000 ms |
| long core | core > 8 s, flagged |
| frame uncertainty W(T) | [T - 233.3 ms, T + 16.7 ms]; the capture offset is UNMEASURED |

Classes: `L2+R2` (some R2-held time overlaps L2-held time), `R2-only` (R2 held, L2 not held, controller input), `L2-only`
(L2 held, R2 not held, controller input), `split` (both held, never together). A window says "L2 and R2 were held (input)".
It does not say that ADS was seen on screen or that there was a gunfight.

Frame time T = `pts_90k * 100 / 9 + env`, env = min over `video.mp4` frames of
`t_mono_us - pts_90k * 100 / 9`. Per window: `frames.window` (W touches the padded window),
`frames.core_possible` (W touches the core) and `frames.core_certain` (W lies wholly inside the core;
only these frames may carry "trigger held at this frame" as an input fact; none when the core is under 250 ms).

## Exclusion flags

The window is still written, with `excluded: true` and its `flags`:

- `hid_doubt`: a heartbeat in the window reports faults, stale_over_50ms, seq_breaks, short_reads,
  parse_faults or max_rx_gap_us > 50 ms; heartbeats more than 1.5 s apart; a leading gap of more than
  1.5 s from the padded start to the first heartbeat when none precedes it; or no heartbeat after the window.
- `backlog`: a frame whose arrival trails T by more than 500 ms within [start - 1 s, end + 0.5 s].
- `long_core`, `open_at_log_end`, `out_of_span`.
- `match_b4`: only when the caller passes `--match-b4-fail`. The B4 alignment check is not run here,
  so the summary records `match_b4` as UNMEASURED.

## Seal and upload isolation

- The files are written outside the match folder, so no bundle file changes. The tests hash every file
  in a synthetic bundle before and after, and compare `qorcap_submit.meta_zip` bytes.
- The seal, input proof and upload read named files only (`contracts/compat.json` `bundle_files`,
  `qorcap_submit.META_FILES`); neither name is on those lists.
- A child-process audit hook refuses any write-mode open, rename, replace, remove or mkdir under the
  match folder while the CLI runs; the run finishes with none.

## Parity

On the 20 box practice logs the module reproduces `count_windows.py` (COUNTS-pwec.json sha256 b204719b...)
window for window: 1521 windows, 0 differences (class, core length, holds, three frame counts, flags),
with `--match-b4-fail` on the three matches that plan marks B4 fail / no fitted segment. 730 clean
L2+R2 windows. One reduced log (match-20261119-191816, 82 windows) is the CI golden
(`sidecar/fixtures/pwec`). This module does not compute `has_prior_cut_frame` (that mark depends on
box-only labeller frame lists, not on the controller log). Of the 1521 COUNTS-pwec windows, 171 carry
it. Any later sampler that reads this sidecar must re-apply that filter itself.
