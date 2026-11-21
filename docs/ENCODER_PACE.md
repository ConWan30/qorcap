# Encoder pace report (offline)

`tools/encoder_pace.py` reads a QorCap log (`qorcap.log`, or a console copy of it) and reports how fast the capture encoder kept up. It's read-only and stdlib-only. It writes nothing, and it never touches a recording, checkpoint or receipt. Every report starts with the input file's sha256 and its line counts: parsed, skipped (unreadable) and blank.

```
python tools/encoder_pace.py path\to\qorcap.log            # JSON
python tools/encoder_pace.py path\to\qorcap.log --table    # short text table
python tools/encoder_pace.py path\to\qorcap.log --fps 30   # override the nominal rate for every segment
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The report was printed, and both pace and drop messages were readable. |
| 2 | The file is missing, or an fps is not a finite number above 0. The fps can come from `--fps` or from an argv `-r`; `-r 0`, `nan` and `inf` are all rejected. |
| 3 | The report was printed, but pace or drop messages are UNKNOWN for the whole file. Pace is UNKNOWN when there is no readable progress window. Drop messages are UNKNOWN when no segment is drop-capable. UNKNOWN values are `null`, never 0. |

## Segments

`qorcap.log` is opened in append mode, so one file can hold several sessions and writer restarts.
- Every `capture ffmpeg argv:` line starts a new segment. That segment's nominal fps comes from that line's `-r N`, unless `--fps` overrides it.
- A frame counter or clock that goes backwards also starts a new segment.
- No window, deficit, episode or drop run ever spans two segments. Each segment reports its own totals, and the file totals are their sum.

## What each number is

| Field | Meaning |
|---|---|
| `windows[].fps` | The frame delta between two `encoder progress:` lines in one segment (about 5 s apart, QorCap #149), divided by the wall time between their log timestamps. |
| `windows[].deficit_s` | Wall seconds since the segment's first progress line, minus the frames encoded since then divided by the segment's nominal fps. It's how far the output is behind real time, relative to that line. |
| `episodes[]` | Two or more windows in a row below 59/60 of nominal that cover at least 9.5 s. An episode closes after 30 s of windows at or above 59.5/60; if it never closes, it says so. |
| `drops` | DirectShow `real-time buffer … frame dropped!` lines (video, audio or other) plus the N in `Last message repeated N times` right after them. Another `ffmpeg:` line ends a run; QorCap's own lines don't. This is the same rule as `src/capstop.rs`. |
| drop-capable | A segment that has its argv line or at least one `ffmpeg:` line. Anywhere else, a missing drop line says nothing, so the counts are UNKNOWN. A file with some drop-capable segments and some not reports `PARTIAL`. |

## Limits

- Drop counts are warning messages, not frames. `dropped_frames` is always `UNMEASURED`. Repeat sums are a lower bound, because ffmpeg prints a pending repeat count only when its next different message arrives.
- Wall time is when QorCap logged the line (when it read ffmpeg's stderr), not when the card produced the frame.
- The capture runs `-vf fps=N … -fps_mode cfr`, so frames lost at the input are filled with duplicates before the encoder. The encoder's `dup=` and `drop=` don't count them, and neither does this report.
- This report can't tell what caused a slow window: the GPU, heat, another app, or the pipe to QorCap. `cause` is always `UNMEASURED`.

## Slices and empty segments

- **A slice reports what it contains.** A cut-off slice of a log that still has its `capture ffmpeg argv:` line is drop-capable and pace-readable. It reports `MEASURED` and exits 0, but its totals cover only the lines it contains. The repo fixture is exactly that case: it reports 126 drop messages, against 23,219 in the full log (below). `MEASURED` means measured from this file, not from the whole session.
- **Empty segments are dropped.** A segment that has only an argv line, with no progress, drop or `ffmpeg:` line after it, isn't listed. That happens when a writer was started and replaced before it logged anything.

## Fixture (reproducible)

`tools/fixtures/encoder_pace/match-230248-redacted.txt` comes from practice match-20261114-230248 (Nov 14 2026, 11:02–11:11 PM CT).
- **sha256:** `e24ccc3f55d0b71081e88a3d19d8e482c8466dc1829523eea8a38679f1a5e199`
- **Size:** 168 lines, LF line endings.
- **Selection:** from the local console log, these lines were kept:
  - all 99 `encoder progress:` lines
  - the `capture ffmpeg argv:` line
  - every `ffmpeg:` line from 05:04:28Z to 05:04:38Z. The first drop line in that stretch is at **11:04:34 PM CT** (05:04:34Z).
  - the `out_#0:0` "100 buffers queued" line, and the startup "Guessed Channel Layout" and "stream format change" lines
  - the `live tap stats` lines before 05:05Z
- **Redaction:**
  - `@ 0x…` pointer addresses are zeroed.
  - The ffmpeg and snapshot paths are `C:\REDACTED\…`.
  - No user path, gamertag or address remains.

The fixture's own totals are the only numbers this repo can reproduce:
- 1 segment at 60 fps from the capture argv
- 98 windows, 62 of them slow, with a minimum of 46.08 fps
- a peak deficit of 21.02 s at 11:10:03 PM CT (05:10:03.865Z)
- 1 episode from 11:04:28 PM CT, with 59 slow windows
- 126 drop messages: 38 video drop lines plus 88 repeats, and 0 unread repeats

## Full-log figures (not reproducible from the repo)

The full-match figures come from the **unredacted local console log**, which is not in the repo because it contains a user path: QorStream's `nov14-night/qc/console.log`, 1,378 lines, sha256 `d78ef2063a86b15b61e88d994138ed33aa04a6bd5f840504b5849adf7db47428`.
- The pace figures are the same as the fixture's: 62 of 98 windows slow, a minimum of 46.08 fps, and a peak deficit of 21.02 s.
- Drop messages: 652 video drop lines plus 22,567 repeats, for 23,219 messages.
