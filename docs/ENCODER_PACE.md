# Encoder pace report (offline)

`tools/encoder_pace.py` reads a QorCap log (`qorcap.log`, or a console copy of it) and reports how fast the capture encoder kept up. It's read-only and stdlib-only. It writes nothing, and it never touches a recording, checkpoint or receipt.

```
python tools/encoder_pace.py path\to\qorcap.log            # JSON
python tools/encoder_pace.py path\to\qorcap.log --table    # short text table
python tools/encoder_pace.py path\to\qorcap.log --fps 30   # override the nominal rate
```

## What each number is

| Field | Meaning |
|---|---|
| `windows[].fps` | The frame delta between two `encoder progress:` lines (about 5 s apart, QorCap #149), divided by the wall time between their log timestamps. |
| `windows[].deficit_s` | Wall seconds since the segment's first progress line, minus the frames encoded since then divided by the nominal fps. It's how far the encoder output is behind real time, relative to that first line. |
| `nominal_fps` | From `--fps`. Otherwise from `-r N` in the `capture ffmpeg argv:` line. Otherwise 60, and the source says so. |
| `segments[]` | A frame counter that goes backwards starts a new segment, which is a writer restart. |
| `episodes[]` | Two or more windows in a row below 59/60 of nominal that cover at least 9.5 s. An episode closes after 30 s of windows at or above 59.5/60. If it never closes, the episode says so. |
| `drops` | DirectShow `real-time buffer … frame dropped!` lines (video, audio or other) plus the N in `Last message repeated N times` right after them. Another `ffmpeg:` line ends a run; QorCap's own lines don't. This is the same rule as `src/capstop.rs`. |

## Limits

- Drop counts are warning messages, not frames. `dropped_frames` is always `UNMEASURED`. Repeat sums are a lower bound, because ffmpeg prints a pending repeat count only when its next different message arrives.
- Wall time is when QorCap logged the line (when it read ffmpeg's stderr), not when the card produced the frame.
- The capture runs `-vf fps=N … -fps_mode cfr`, so frames lost at the input are filled with duplicates before the encoder. The encoder's `dup=` and `drop=` don't count them, and neither does this report.
- This report can't tell what caused a slow window: the GPU, heat, another app, or the pipe to QorCap. `cause` is always `UNMEASURED`.

## Fixture

`tools/fixtures/encoder_pace/match-230248-redacted.txt` comes from practice match-20261114-230248 (Nov 14 2026, 11:02–11:11 PM CT). It keeps:
- all 99 progress lines
- the redacted argv line
- the drop stretch from 11:04:28 to 11:04:38 PM CT
- a few other lines

Pointer addresses and local paths are redacted. On the full log, the tool reports:
- 62 of 98 windows slow, with a minimum of 46.08 fps
- a peak deficit of 21.02 s at 11:10:03 PM CT
- 652 drop lines plus 22,567 repeats, for 23,219 messages
