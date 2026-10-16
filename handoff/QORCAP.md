# QorCap

**Repo:** https://github.com/ConWan30/qorcap · **Site:** https://conwan30.github.io/qorcap/ (served from `docs/`, so don't put private docs there) · **Releases:** v0.1.0, v0.2.0, v0.2.1 (https://github.com/ConWan30/qorcap/releases)

A Windows capture-card recorder that produces QorTime evidence bundles. There is no CI workflow in this repo yet. Run tests locally.

## Architecture
- **Rust** binary (`src/`) that drives a **child ffmpeg** process (`src/ffmpeg.rs`; `get-ffmpeg.ps1` fetches ffmpeg).
- **Watchdog / reopen** (`engine.rs`): if the device drops or ffmpeg dies, it reopens the device and keeps recording.
- About **2 s segments**, each **SHA-256** hashed into a **hash chain** (`evidence.rs`, `ring.rs`, `cfr.rs`), plus a **manifest** and **checkpoints**.
- **/challenge** endpoint for QorTime nonce challenges.
- **Sidecar** (`sidecar/qorcap_reader.py`, `qorcap_match.py`) runs QorTime's score reader on frames.
- **Match bundle** (`bundle.rs`) plus **`qorcap verify-bundle <dir>`**.
- **Start Match.cmd / start-match.ps1** one-click launcher (v0.2.0+): prerequisite checks, record plus scorebug reading, live local countersign, signed self-attested QorTime receipt, verify.
- **`qorcap finalize`** / **Finalize Last Match.cmd** (v0.2.1): offline finalize of the last bundle.
- **Test Without Card.cmd** / `--test-pattern`: dry run with no card attached.
- **Self-attested local referee keys** in `%LOCALAPPDATA%\QorCap\keys`. They are local, so receipts are self-attested and not VERIFIED.
- CLI: `devices`, `record`, `save-replay`, `stop`, `status`, `verify-bundle`, `finalize`.

## Local API: `http://127.0.0.1:8788`
`GET /status`, `/events`, `/timeline`, `/checkpoints`, `/checkpoints/stream` · `POST /record/start`, `/record/stop`, `/replay/save`, `/bundle/write`, `/challenge`, `/shutdown` (see `src/api.rs` for the exact methods).

## Tests
```bash
cargo test
python -m pytest sidecar
```

## Capture card (Con's)
- Video device: **"USB3.0 Video"**. Audio: **"Digital Audio Interface (USB3.0 Audio)"**.
- The card offers raw **yuyv only at 1080p or higher**. Use **mjpeg** for 1280x720.
- Working setting: **mjpeg 1280x720 @ 30 fps**, measured at about 29.7 fps.

## FIRST REAL TEST: 2026-10-14, laptop "MyTurn"
1. **OBS was holding the card**, so the device was busy. Fix: close OBS. v0.2.1 now detects OBS and offers to close it.
2. **Combined video+audio dshow input failed.** Fixed in v0.2.1 (separate audio input plus a video-only fallback). **The fix is untested on hardware.**
3. mjpeg 720p30 worked at **29.7 fps**.
4. Recorded about **14.5 min, 439 segments**. **verify-bundle: OK.**
5. Bundle: `C:\Users\Contr\Documents\QorCap Matches\live-20261014-164312`
6. The last frame read **NO 12, NYG 22, 4th 0:21**. The clock never reached 0:00, so **no signed receipt** was produced. That is correct fail-closed behavior.
7. **v0.2.1 is NOT on MyTurn yet.** v0.2.0 is unzipped at `C:\QorCap\qorcap-v0.2.0-windows-x64`. Next: copy the v0.2.1 release zip there and use Start Match for a full game.

## v0.3.0: controller input proof (2026-10-16)
- New modules: `src/hid.rs` (DualSense/Edge USB 0x01 + BT 0x31 parser ported from Qoresence `sync/hid_report.py`; Windows via `hidapi` windows-native, Linux via hidraw), `src/inputs.rs` (logger on the engine's monotonic clock, per-segment chunk hash -> checkpoint `input_hash`, `inputs.jsonl`), `src/humanlike.rs`, `src/align.rs`, `src/inputproof.rs`.
- `qorcap input-test` / **Input Test.cmd**: live HID check. `qorcap input-proof DIR`: recompute `input_proof.json` + `signals.input`.
- Con's setup: pad on PS5 over Bluetooth AND USB into MyTurn at the same time (intended). Never tell him to unplug USB.
- Not yet run against a real DualSense. First step at MyTurn: Input Test.cmd.
- Proposed CI: `handoff/ci.yml.example` -> copy to `.github/workflows/ci.yml` (push needs a token with `workflow` scope; the bot token lacks it).
