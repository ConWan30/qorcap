# Haptic / rumble detection from the controller IMU (v0.3.1)

**Advisory, corroborating evidence only. Not proof.**

## Why it is indirect

The DualSense / DualSense Edge plays the PS5 over **Bluetooth** and is **also plugged into the laptop by USB**
(the intended dual path; keep both). The PS5 sends its rumble and haptic commands over Bluetooth, so the laptop
never sees them. What the laptop does see, in every USB input report, is the pad's own gyroscope and
accelerometer. When the actuators run, the pad shakes, and the IMU picks that up. QorCap looks for those shakes.

## What it does

* Reads gyro + accel from USB report `0x01` (bytes 16..21 gyro, 22..27 accel, 3 x i16 LE each; sensor timestamp
  28..31). Offsets follow Linux `hid-playstation.c` `struct dualsense_input_report` (same for DualSense `0x0CE6`
  and DualSense Edge `0x0DF2`). Raw counts, no per-pad calibration needed.
* Measures the real IMU report rate (shown by Input Test) and designs its filters for it.
* Per axis: 4th-order Butterworth high-pass at 20 Hz (removes gravity and hand motion), plus a 200 Hz low-pass
  when the rate is high enough. At ~250 reports/s the usable band is 20..~112 Hz (Nyquist limit), which still
  covers DualSense rumble/haptics.
* Energy over a 32 ms window, divided by a rolling baseline (seeded by a **3 s hold-still calibration** at the
  start, then a slow ~3 s average that freezes during events). Event on at 8x baseline, off below 3x for 24 ms;
  events shorter than 20 ms are dropped.
* Any button press/release or big trigger jump within 30 ms of an event marks it **low confidence**
  (`flags: ["button_coincident"]`, confidence <= 250): pressing buttons jolts the pad too.

## Where it goes in the bundle

* `inputs.jsonl`: `{"type":"haptic", ...}` records inside the normal chunks, so they are covered by each
  checkpoint's `input_hash` and the inputs chain, exactly like button records. `{"type":"imu","present":..,
  "rate_hz":..}` records commit whether IMU data was there. The header lists the detector parameters.
* `haptics.jsonl`: the same haptic records, one per line:
  `{t_us, t_mono_ms, t_wall_ms, chunk_index, duration_ms, peak (x baseline), peak_accel_rms, peak_gyro_rms,
  energy (x-baseline*ms), confidence (permille), confidence_label, flags, imu_rate_hz}`. `t_us` / `t_mono_ms` are
  on the same QorCap monotonic clock as the video chunks (`manifest.jsonl` / `checkpoints.jsonl` `mono_ms`).
  `chunk_index` is the video segment whose checkpoint committed the record.
* `input_proof.json` `haptics`: event count, high/low confidence counts, events per minute (x10), IMU present /
  rate, score changes from the scorebug timeline and how many had a haptic event from 30 s before to 5 s after,
  `haptics_sha256` (file hash) and `haptics_file_matches_inputs` (the file equals the committed records).
  `receipt.draft.json` `signals.input` and `bundle.json` `input` gain `haptic_events`, `haptic_high_confidence`
  and `haptics_sha256` (integers/strings only, JCS-safe).

## What it does not show

Vibration patterns vary by game, setting (vibration intensity can be turned down or off on the PS5), and how the
pad is held or rested. Hard hits on a table or fast flicks can look like vibration. A match with zero events is
not suspicious by itself. Use it only to corroborate other evidence (video, inputs, scorebug), never alone.

## Laptop test steps (Windows)

1. Keep the controller paired to the PS5 over Bluetooth **and** plugged into the laptop by USB.
2. Double-click **Input Test.cmd** (or `qorcap input-test --secs 60`).
3. For the first 3 s, **put the controller down or hold it still** (baseline). You will see
   `IMU baseline calibrated; haptic detection is live`.
4. Play something on the PS5 that rumbles. Each detected burst prints
   `HAPTIC  peak=..x dur=..ms energy=.. confidence=.. (high|low)`.
5. Press buttons without any rumble: events should be rare and, if any, marked `[near a button press]`.
6. Press Ctrl+C. The summary shows `[ OK ] IMU readable: N IMU reports at R reports/s` or
   `[WARN] IMU not readable`. Send that output back if anything looks wrong.

## Not verified yet

Only synthetic IMU streams are tested. Not yet run on a real DualSense or Edge; thresholds, offsets on the Edge,
the real USB report rate on Windows, and how strong real rumble looks in raw counts are all assumptions until
Input Test is run on the laptop.
