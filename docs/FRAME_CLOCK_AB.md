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

## Measured-clock latency (one-sided search, lower-only)

A `frame_clock.jsonl` stamp is when a frame *arrived* on this machine, after game render, HDMI, the capture card, USB, and the encoder. That pipeline latency is roughly constant within a session. The measured path estimates it once per session and reports it as `measured_alignment.latency`:

- The offset search on the measured clock is one-sided: 0 to 1500 ms in one-frame steps (the fit path keeps its +/-3 s search in 3-frame steps). The picture of a press cannot arrive before the press. A negative offset on this clock lands on screen changes that came before the press, such as a player reacting to a menu or a whistle.
- Only presses with a stamp within 500 ms are scored on the measured clock. Presses without one are skipped rather than placed on the manifest fit, which sits about half a second away from the stamps on a real card. Mixing the two clocks in one offset search smears the offset.
- `latency.latency_ms` is the median of the held-out matched press-to-screen anchors (calibration offset + response lag), with the same sign as `offset_ms` on the fit path. Picture time = `t_mono_us` - `latency_ms`. `latency.iqr_ms` is their spread, and `latency.halves_apart_ms` is how far apart the two calibration halves landed.
- `latency.status` is `unscorable` with fewer than 3 anchors, an IQR above 100 ms, or halves more than 100 ms apart.

In `input_proof.json` (flag on only), the fit is still the default. The measured score replaces it only when its latency is scorable and its z is not above the fit's, so the frame clock can lower the alignment and cannot raise it. Both scores and the latency are under `alignment.frame_clock`. The receipt fields and `receipt.v0.json` do not change. The flag stays off by default.

`measured.vs_fit_signed_p50_ms` (Rust and `tools/frame_clock_ab.py`) gives the sign of the arrival-vs-fit gap. Negative means arrivals sit before the fragment-close line.

Practice session match-20261025-101040 (13 min, before this change): fit residual 253 / 516 / 718 ms (n=392); arrivals vs fit 522 / 549 / 573 ms; arrival jitter 4 / 33 / 116 ms. `fit_alignment` 738 vs chance 345 permille (z 4.92, offset 767 ms, IQR 167, halves 500 / 600 ms). `measured_alignment` 655 vs chance 361 (z 3.68, offset 0 ms, IQR 600, halves -100 / -800 ms). The two halves landed 700 ms apart, one of them on a negative offset. With the arrivals about 0.5 s ahead of the fit line, the measured offset should sit near 767 - 522, about 250 ms. Re-run `qorcap align-ab` on that bundle to get the post-change numbers. The synthetic tests are not a measurement.

## What this machine cannot show

No capture card, no DualSense, and no copy of match-20261020-171032 were available here. The 33 ms figure is not measured. Encode delay (the frame is stamped when its PES arrives, after `-bf 0` encode) and several frames sharing one read are inside the stamp. Those have not been measured on the MS2131.
