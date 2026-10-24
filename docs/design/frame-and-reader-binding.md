# Frame binding and reader binding

Draft. Flag off by default (`--frame-bind` / `QORCAP_FRAME_BIND`). Nothing in this document changes the receipt, signing keys, trust roots, or QorTime.

Reviewed against QorCap `src/evidence.rs` and QorTime `dd01b6c` (`spec/match-receipt-v0.md` §4, `qor_receipt/checkpoints.py`).

## What is wrong today

QorTime's spec says `frames_hash` is "the SHA-256 over the SHA-256 values of the window's frames." `window_hash` in `checkpoints.py` is that function: it feeds each frame's bytes through SHA-256 and hashes the digests together.

QorCap does not do that. `CheckpointChain::next_with_input` sets

```text
frames_hash = SHA-256(JCS(qorcap))
```

`qorcap` is one object per checkpoint: trigger, segment sequence, segment hash, chain head, score, `mono_ms`, match id, role, and sometimes a nonce or a final. The segment hash is the SHA-256 of a whole fMP4 fragment, about two seconds of video. A frame is localized only to that fragment.

`--frame-clock` already writes `frame_clock.jsonl`: one line per video frame with the QorCap monotonic time of the MPEG-TS packet that starts that frame. That file is not in the hash chain, not in the receipt, and not checked by `verify-bundle`. It can be edited after the match without breaking a checkpoint.

Clip time and that capture clock are not the same clock. Highlight cuts map file time (`frame / fps`) onto `mono_ms` by interpolating fragment-close stamps in the manifest. On the match that motivated the frame-clock work, fragment-close residuals were hundreds of milliseconds (about 246 / 573 / 799 ms). A sidecar that records both the frame index and `mono_ms` makes that skew measurable instead of folding it into the cut.

No checkpoint records which scorebug reader or template pack produced the reads. The hosted referee re-reads finals with its own pinned reader. A bundle cannot show that the live reads came from a different pack.

## What this draft does not change

- `contracts/receipt.v0.json` is untouched. Top-level `additionalProperties` is false. The receipt gains no fields.
- Receipt checkpoints stay `{seq, t_ms, frames_hash, input_hash, prev, hash, referee_ts_ms, referee_sig}`. QorTime `body_of` copies only `seq, t_ms, frames_hash, input_hash, prev`. The `qorcap` object is not a receipt field.
- `frames_hash`'s formula stays `SHA-256(JCS(qorcap))`. This draft does not start hashing per-frame digests into `frames_hash`. Doing that would silently change every checkpoint hash and would still not match a spec the verifier does not recompute from pixels.
- Signing keys and trust roots are not touched. QorTime is not edited.
- `--frame-clock` stays what it is: advisory, unsealed, default off. `--frame-bind` is a separate flag. Turning on the clock does not turn on the binding.

When the flag is on, `qorcap` gains `frame_bind` and, on checkpoint 0 only, `readers`. Those keys change the preimage of `frames_hash` for that recording, because `frames_hash` already commits to the whole `qorcap` object. The formula does not change. Flag-off checkpoints do not grow those keys, so old and new flag-off bundles hash exactly as before.

## `frames_hash`, documented and not redefined

| | Meaning |
|---|---|
| QorTime spec §4 and `window_hash` | SHA-256 over the SHA-256 of each frame in the window |
| QorCap `evidence.rs`, unchanged | SHA-256 of the JCS of the segment-level `qorcap` object |

A verifier that only checks `hash = SHA-256(JCS({seq, t_ms, frames_hash, input_hash, prev}))` does not notice the difference. QorCap `verify-bundle` recomputes `frames_hash` from `qorcap` and would reject a checkpoint that used `window_hash` instead.

**Proposed spec sentence (not applied).** Replace the `frames_hash` sentence in `spec/match-receipt-v0.md` §4 with:

> `frames_hash` is the agent's commitment for this window. QorCap sets it to `SHA-256(JCS(qorcap))`, where `qorcap` is the segment-level evidence object carried beside the checkpoint and stripped before the receipt is built. It is not a hash of per-frame image hashes. A per-frame claim, when present, is a sidecar whose running head is `qorcap.frame_bind.head`. Absence of `frame_bind` is not a per-frame claim.

A later receipt version could add an explicit per-frame root. `additionalProperties: false` on the checkpoint object means that root cannot be added to `qor-receipt/v0`.

## Per-frame sidecar

File: `frames.jsonl`, one JSON line per record, RFC 8785 JCS, integers only. The newline is a separator and is not hashed.

Header, written when the file is created. PTS is in the 90 kHz time base (`num` 1, `den` 90000). The `--fps` flag is not a field. Pairing and skew use that PTS.

```json
{"clock":"qorcap-mono-ms","hash_of":"encoded-video-sample","schema":"qorcap-frame-bind/v0","time_base":{"den":90000,"num":1},"type":"header"}
```

One line per video sample that matched an arrival:

```json
{"au_sha256":"<64 hex>","frame":361,"mono_ms":12045,"pts":32521500,"seg":12,"type":"frame"}
```

| Field | Meaning |
|---|---|
| `seg` | Manifest segment sequence of the fMP4 fragment that contains the sample |
| `frame` | Video frame index from the start of the recording, in sample order |
| `mono_ms` | Process clock when this process read the MPEG-TS packet that carries this frame's PTS. See provenance below. |
| `pts` | Exact 90 kHz presentation timestamp of the encoded sample (`tfdt` base media decode time plus sample durations, timescale converted to 90 kHz). Composition offset is forced to 0. |
| `au_sha256` | SHA-256 of the encoded video access unit (the sample bytes inside the fragment) |

`hash_of` is `encoded-video-sample`, not decoded pixels.

**Why the encoded sample is what a referee can recompute.** The segment hash already commits to the exact fragment bytes. The sample is a slice of those bytes: `trun` sample size and `data_offset` from the start of the `moof` (`default-base-is-moof`, which the recorder mux sets). Recomputing the digest is a box walk and a SHA-256. It does not depend on ffmpeg, swscale, or color conversion. PTS comes from `tfdt` plus each sample's duration. A non-zero composition offset is an `align_gap` for that stretch, not a timed frame. The capture command is `-bf 0` and the recorder is `-c copy`.

**Why decoded pixels are not the commitment.** The scorebug reader sees pixels, and a decoded hash would match that view only for one decoder, one pixel format, and one scaler. The hosted referee's OpenCV decode of a highlight will not match a hash taken of a different ffmpeg build. An encoded hash does not prove "these pixels were read." It proves "this access unit, at this PTS, is the one inside this sealed fragment." Which code interpreted the pixels is the reader sidecar below.

**Pairing is exact PTS, per segment.** Each MPEG-TS video PES start is an arrival: its 90 kHz PTS and the `mono_ms` of the read. Each fMP4 video sample gets a PTS from `tfdt` plus durations. A sample is a timed frame only when an unused arrival in the current generation has that exact PTS. The nominal `--fps` is not used to invent a time. A dropped or extra sample does not slide later frames onto the wrong arrival.

A miss (no arrival at that PTS, an unreadable fragment, or a non-zero composition offset) writes one `align_gap` for that segment. `samples` is the sample count, `bound` is how many of those samples became frame rows, and `reason` is one of `sample_pes_mismatch`, `composition_offset`, `unreadable`, `pts_regression`, or `clock_lost`. An unreadable fragment has no sample count, so that gap is one index wide. The next segment pairs on its own PTS values. A gap is not a frame-time claim and does not fail the match. The fragment is still covered by its segment hash. Only a committed hash that does not match the bytes on disk fails verification.

**`mono_ms` is a read time, not glass time.** It is the QorCap monotonic clock when this process read the encoder's MPEG-TS from the pipe, after encode and pipe delay. Truncated to a millisecond. Manifest `mono_ms` uses the same `Instant`. HID controller lines use the same clock base, but they are stamped when the USB report is read, not when the encoder emits a frame. A press-to-frame delta therefore includes encode and pipe latency. That delta is not a mismatch claim until the latency of that capture rig has been measured. Skew reported from the sidecar is arrival time minus PTS media time, anchored at the first timed frame so a constant offset drops out. It is not a claim that the picture left the glass at `mono_ms`.

## Running hash

```text
head_0 = 64 zero bytes
head_n = SHA-256(JCS({"prev": head_{n-1}, "sha256": SHA-256(line_n)}))
```

`line_n` is the exact JCS bytes of that line, without the newline. Every non-empty line counts, including the header and any `align_gap`. The merged schema has no seal line. Blank lines are not written. A blank line inserted later is an extra hashed line and breaks the chain.

The line is written and flushed before `head` and `n` move. A write error sticks `frame_bind.state` to `failed`, commits that state on later checkpoints, and does not advance a hash for a line that did not land. `state: failed` is not a frame claim (`frame_status` is `broken`). The same rule applies to `readers.json`: the hash is stored only after the file rename succeeds. A failed readers write leaves checkpoint 0 as `readers.state: unbound`, which is `reader_status: missing`, not a reader claim.

Each checkpoint's `qorcap.frame_bind` is:

```json
{"schema":"qorcap-frame-bind/v0","n":1281,"head":"<running hash after line n>"}
```

or, after a write error:

```json
{"schema":"qorcap-frame-bind/v0","n":12,"head":"<hash of lines that landed>","state":"failed"}
```

`n` is a prefix. It grows as lines land and must not go backwards. The last checkpoint's `n` equals the number of lines in the file. `tail: sealed` means the stop checkpoint covers every line. Dropping that checkpoint leaves a consistent prefix with `tail: open`. That prefix is not a frame claim. Lines past the last checkpoint fail verification. Because `frames_hash` covers `frame_bind`, changing a line, dropping one, inserting one, or editing `mono_ms` changes a checkpoint hash unless the whole chain is rebuilt. Rebuilding the chain changes `hash`, which is what the referee signature covers. `verify-bundle` itself only checks consistency; the referee signature is still QorTime's job.

QorTime's commitment schema is `{schema, n, head}` only. QorCap still adds `state: failed` after a write error. That extra key deliberately does not match the vendored checkpoint schema. qortime 585ccc4 `verify_frame_bind` reads the bundle as `broken`, which is fail-closed: it is not a frame claim. A later qortime change should accept `state: failed` as an honest failed write, distinct from tampering. Until then the key stays. Omitting the commitment looks like a stripped sidecar, and freezing the last good `{n, head}` would make an incomplete file look sealed.

## Reader sidecar

File: `readers.json`. One JSON object, JCS, plus a trailing newline. The commitment is SHA-256 of those exact bytes.

```json
{
  "schema": "qorcap-readers/v0",
  "qorcap_version": "0.3.32",
  "readers": [
    {
      "id": "standard",
      "version": "0.3.32",
      "files": [
        {"kind": "code", "path": "sidecar/qorcap_standard.py", "sha256": "<64 hex>"},
        {"kind": "pack", "path": "sidecar/packs/madden27_standard_v2.npz", "sha256": "<64 hex>"}
      ]
    }
  ],
  "runtimes": [
    {"id": "cv2", "version": "4.10.0", "sha256": "<64 hex of the cv2 build>"},
    {"id": "ffmpeg", "version": "n7.0.1", "sha256": "<64 hex of the binary>"}
  ]
}
```

The object is closed. Unknown keys, including `reader_verified_by_referee`, `python`, `numpy`, and `glimpse`, are rejected. `runtimes` is required and names the builds that produced the reads (`ffmpeg`, `cv2`). A build that is not on the machine is omitted. If neither build has a hash, no `readers.json` is written and checkpoint 0 stays unbound. Reader ids and runtime ids are unique after NFC and casefolding, including a trailing dot or space. Paths are unique the same way, ASCII only, and a path cannot be both code and pack. A path segment is refused when `_key` (any case) is followed by `.` or the end of that segment, including `x.y_key.py`. Windows device names (`CON`, `NUL`, `AUX`, `PRN`, `COM1`–`COM9`, `LPT1`–`LPT9`, including `CON.py` and `nul.txt`) are not emitted. The file is JCS plus one newline and is refused above 64 KiB, the cap qortime uses.

`path` is relative (`sidecar/...` or `qor_receipt/...`). Absolute paths, `..`, a `keys` path component, and names ending in `.key`, `.pem`, `.p12`, `.pfx`, `.jwk`, `.ppk`, or `.jks` are rejected. Key material is not hashed and not written. Hashes are raw file bytes. `.gitattributes` pins `*.py` to LF and `*.npz` as binary so a Windows checkout hashes the same bytes as the repo.

Start Match builds this before `qorcap record` starts (`write_prerecord_manifest`) and passes it as `--readers-manifest`. The builder hashes modules it imports for a live match (`qorcap_match`, `qorcap_psn`, the local readers, and the vendored QorTime scorebug reader when that import succeeds). A vendored pack is the last `packs/madden27_standard_v*.npz` next to that reader, the file load would open. QorCap reads the manifest in `Engine::new`, writes canonical `readers.json` (JCS plus a newline), and hashes those bytes. There is no `/events` post and no wait. A later `POST /events` with `type: readers` is rejected and cannot change the sealed file.

Checkpoint 0 carries:

```json
{"schema":"qorcap-readers/v0","sha256":"<file hash>"}
```

or, if the manifest was missing or could not be written:

```json
{"schema":"qorcap-readers/v0","state":"unbound"}
```

Later checkpoints do not repeat it. They link back through `prev`, so editing checkpoint 0's `qorcap` still breaks the chain.

Checkpoint 0 is written as soon as recording starts (and, with the flag on, before the API accepts events, so a challenge cannot take seq 0). It does not wait. The 4 second startup heartbeat still runs until the first media segment. Flag off does not write the files and does not add the keys.

`state: unbound` is `reader_status: missing`. It is not a reader claim and it does not mean "the pinned referee reader was the one that ran." `frame_bind` present with `readers` absent on checkpoint 0 is `broken` for both statuses.

## How a verifier checks

`qorcap verify-bundle` already recomputes each checkpoint hash and `frames_hash` from `qorcap`. It also fills a machine-readable bind report. None of this reads a signing key. The printed fields are `frame_status`, `reader_status`, `frame_chain_head`, `coverage_timed`, `coverage_samples`, `coverage_gaps`, `au`, and `tail`.

`frame_status` and `reader_status` are each `verified`, `missing`, `stripped`, or `broken`.

| Status | Meaning |
|---|---|
| `missing` | No `frame_bind` on any checkpoint, or checkpoint 0 says `readers.state: unbound`. Not a claim. |
| `stripped` | `frames.jsonl` or `readers.json` is on disk but no checkpoint commits `frame_bind`. Not a claim. |
| `broken` | A commitment does not match the file, a row is invalid, an AU hash does not match the fragment, a write failed, or `frame_bind` is present without `readers` on checkpoint 0. |
| `verified` | The chain matches and the rows are sane. A frame claim still requires `tail: sealed`, zero gap samples, and at least one timed frame. |

`tail` is `sealed` (the stop checkpoint covers every line), `open` (a consistent prefix; dropping the stop checkpoint looks like this), or `checkpoints_missing` (sidecar files exist and `checkpoints.jsonl` does not). `open` and `checkpoints_missing` are not a frame claim.

Row checks, when a chain is present: the header matches the schema, each frame or gap `seg` is the manifest sequence of that checkpoint's segment hash, frame numbers are strictly increasing, `mono_ms` is at most 48 hours and does not jump by more than 10 minutes, and PTS does not go backwards inside a segment. When the committed fragments are in the bundle, `au` is `checked` after recomputing sample hashes, `mismatch` (and `broken`) when one disagrees, or `unavailable` when the bytes are not in the bundle. `unavailable` is not a second claim and does not by itself fail the chain.

A gap, `unbound`, a missing sidecar, a stripped file, an open tail, or an unverified chain is no claim. Code that asks for the capture time of a frame returns no claim in those cases. It does not fall back to the fragment-close fit and call that a frame measurement.

Skew, only when there is at least one timed frame and zero gap samples:

```text
clip_ms    = (pts - pts_0) * 1000 / 90000
capture_ms = mono_ms - mono_ms_0
skew_ms    = capture_ms - clip_ms
```

That rate is the PTS spacing, not `--fps`. It measures arrival time against media time. It is not a glass-time or press-to-frame claim. A second comparison, for the highlight cutter, interpolates manifest `(frames, mono_ms)` pairs the way `mono_bounds` does. That is the fragment-close residual. Both are integers.

## Failure modes

| What happened | Result |
|---|---|
| Flag off, or an old bundle with no `frame_bind` | Bundle verifies. `frame_status` and `reader_status` are `missing`. Not a claim. |
| Sidecar files present but no checkpoint commits them | `stripped`. Not a claim. |
| Sidecar files present and `checkpoints.jsonl` missing | `broken`, `tail: checkpoints_missing`. Not a claim. |
| Any committed line edited, including `mono_ms` | `frame_bind.head` mismatches. `frame_status: broken`. |
| Line inserted, dropped, or appended after the last checkpoint | `broken`. The last `n` no longer matches the file. |
| Stop checkpoint dropped | Consistent prefix, `tail: open`. Not a frame claim. |
| `frame_bind` committed and `frames.jsonl` deleted | `broken`. Not a silent pass. |
| `readers` unbound | `reader_status: missing`. Not a reader claim. |
| `frame_bind` present and `readers` absent on checkpoint 0 | Both statuses `broken`. |
| `readers.json` edited, deleted, or never written after a hash was committed | `reader_status: broken`. |
| `POST /events` with `type: readers` | Rejected. Does not write or replace `readers.json`. |
| No arrival at a sample's PTS, unreadable fragment, or non-zero composition offset | `align_gap` for that stretch. Later segments still pair by their own PTS. The match is not failed. `frame_claim` stays false while any gap sample remains. |
| `frames.jsonl` or `readers.json` write fails | Hash does not advance for the unwritten bytes. Frames: `state: failed`, `frame_status: broken`. Readers: checkpoint 0 stays `unbound`, `reader_status: missing`. Neither is `verified`. |
| Could not create `frames.jsonl` at open | Flag degrades to off for that process. No `frame_bind` field. Status `missing`. |
| Millisecond truncation | `mono_ms` drops the sub-millisecond part of the read. That is at most 1 ms. It is still a pipe-read time, not glass time. |
| Encoded hash vs pixels | A referee that only decodes cannot check `au_sha256`. A disagreement between this reader and a pinned reader is a reader-hash mismatch, not a frame-hash mismatch. |
| Full re-seal of checkpoints and sidecar | `verify-bundle` sees a consistent chain. The referee signature over the old `hash` fails in QorTime. This draft does not pretend `verify-bundle` is the referee. |

## Cost

Flag off: no file, no extra hash, no wait, checkpoints unchanged.

Flag on:

- One extra SHA-256 per video sample, on bytes the segment hash already reads. At a few megabits per second this is noise next to the encode.
- One small JCS hash per sidecar line, after that line is flushed.
- `frames.jsonl` is about 180 bytes a line. At 30 fps that is on the order of 20 MB per hour. The real rate is the PTS spacing of the encode, not a hardcoded 30.
- `readers.json` is one small file, hashed once, before checkpoint 0.
- `verify-bundle` walks the sidecar once, in order, and extends the running hash. A three-hour match is a few hundred thousand lines, one sequential read.
- Checkpoint 0 does not wait. The reader manifest is already on disk when `qorcap record` starts. The 4 second startup heartbeat is unchanged.

## Old bundles

A bundle with no `frame_bind` and no `readers` on its checkpoints verifies as before. Absence means "this bundle makes no frame-level claim and no reader-identity claim." It must not be read as "frame times checked out" or "the pinned reader produced these reads."

Code that asks "what is the capture time of frame 361?" returns no claim when the sidecar is missing or failed. It does not fall back to the fragment-close fit and call that a frame measurement. The fragment-close fit remains the clock `--frame-bind` does not touch, including input-proof alignment.

## Flag

```text
qorcap record --frame-bind --readers-manifest readers.input.json
QORCAP_FRAME_BIND=1
```

Same words as the other QorCap switches: `1`, `true`, `yes`, `on`. Default off. Start Match adds `--frame-bind` and writes the manifest only when the environment variable is set, then passes `--readers-manifest`. A normal match does not write the files. `frames.jsonl` and `readers.json` are in the vendored `contracts/compat.json` upload list.

## Schemas

`receipt.v0.json` stays frozen, and `frames_hash` stays `SHA-256(JCS(qorcap))`. The sidecar schemas are vendored from QorTime `585ccc4`: `contracts/qorcap-frame-bind.v0.json` and `contracts/qorcap-readers.v0.json`. Absence of the sidecars remains "no claim," not a failed match. The flag does not change a published verdict.

The bundled runtime verifier stays at `0204e6d`. That checkout does not run `verify_frame_bind`. Bump it to at least `585ccc4` before `--frame-bind` is turned on by default.
