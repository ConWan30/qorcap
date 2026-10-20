# Frame clock A/B

QorCap's input-to-frame clock is a straight line through fragment-close times. Each `manifest.jsonl` media segment stores `mono_ms` (the QorCap monotonic clock when the fMP4 fragment was hashed) and `frames` (ffmpeg's progress counter then). `align.rs` fits those points. A press is placed at `(t_us - start) / ms_per_frame`. That is not the time the frame was captured.

On a real match the fragment-close line wanders: minute averages from about -187 ms to +420 ms, with a step of about a second between 2-second segments. A straight-line fit and a per-segment timestamp both leave that error in place. The capture command rewrites MPEG-TS timestamps to a nominal 30 fps (`-vf fps=30,format=nv12 -fps_mode cfr`), so the packet PTS inside the file is 0, 1/30, 2/30, … and is not a capture time.

`--frame-clock` (or `QORCAP_FRAME_CLOCK=1`) records `frame_clock.jsonl` next to the bundle. Each line is the QorCap monotonic time when this process read the MPEG-TS packet that starts that video frame — the same `Instant` as `inputs.jsonl` `t_us` and manifest `mono_ms`. The ffmpeg command is not changed. The file is not in the hash chain, not in the receipt, and not checked by `verify-bundle`. Alignment uses a stamp only when one sits within 500 ms of the press. Otherwise it uses the fragment-close fit, and that JSON is unchanged.

The flag defaults off. Do not turn it on for scored matches until the procedure below has been run on real games. An error near one frame (33 ms) plus device jitter is an estimate, not a result.

This does not change scoring, signing, or verification. Glimpse stays observer-only.

## What to compare

Two numbers, on each match:

- Fragment-close residual: typical / p95 / worst of `|mono_ms - fitted time|` for media segments. On the match that motivated this, that was about 246 / 573 / 799 ms.
- Input proof: `alignment.match_permille`, `alignment.chance_permille`, and `alignment.offset_ms` in `input_proof.json`.

With the flag on, also read `measured.vs_fit_*` (how far real arrivals sit from the old line) and `measured.interval_*` (frame-to-frame jitter of the arrivals). `fit_alignment` vs `measured_alignment` is the proof score with the stamps ignored vs used.

## Offline, on a bundle you already have

No second capture. This does not write the receipt or the proof.

```text
qorcap align-ab PATH/TO/BUNDLE --no-video
```

Clock residuals only. If `frame_clock.jsonl` is absent, `measured` is null and you are looking at the flag-off clock.

```text
qorcap align-ab PATH/TO/BUNDLE
```

Decodes the video once and prints `fit_alignment` and `measured_alignment`.

```text
python tools/frame_clock_ab.py PATH/TO/BUNDLE
```

Same residual math, no video decode, no QorCap binary. Use this when you only want the clock error.

## Real-game procedure

Record two matches on the same card, the same fps, and a controller QorCap is already reading. Leave every other capture flag off (including the ones in the capture-card draft). Do not promote this flag from one match.

1. Match A, flag off. Start Match as usual. Do not set `--frame-clock` and do not set `QORCAP_FRAME_CLOCK`. Play long enough for a full game, or at least several minutes of snaps and a pause. Stop. Note `input_proof.json` → `alignment.match_permille`, `chance_permille`, `offset_ms`. Run `qorcap align-ab <bundle A> --no-video` and note `fit.resid_abs_p50_ms`, `resid_abs_p95_ms`, `resid_abs_max_ms`.

2. Match B, flag on. Same setup, plus `--frame-clock` (or `QORCAP_FRAME_CLOCK=1` before Start Match). The bundle must contain `frame_clock.jsonl` with one `frame` line per captured frame. Stop. Run `qorcap align-ab <bundle B>`. Note the same fit residuals, plus `measured.vs_fit_abs_p50_ms` / `p95` / `max`, `measured.interval_abs_dev_p50_ms`, and both alignment blocks (`fit_alignment` and `measured_alignment`: match_permille, chance_permille, offset_ms).

3. Compare. The fit residuals on B should look like A's (the old clock is still being recorded). The measured interval jitter is the new clock's frame-to-frame noise. The question for promotion is whether `measured_alignment` beats `fit_alignment` on B, and whether that gap is about a frame rather than the hundreds of milliseconds in the fit residuals. One match pair is not a promotion. If the measured match rate is not clearly above its own chance line, leave the flag off.

You can also re-score a single flag-on bundle without a second match: `fit_alignment` is the flag-off score, `measured_alignment` is the flag-on score, on the same decode. That is the comparison to write down. It is still one match.

## What this machine cannot show

No capture card, no DualSense, and no copy of match-20261020-171032 were available here. The 33 ms figure is not measured. Encode delay (the frame is stamped when its PES arrives, after `-bf 0` encode) and several frames sharing one read are inside the stamp. Those have not been measured on the MS2131.
