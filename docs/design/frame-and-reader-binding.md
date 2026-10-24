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

Header, written when the file is created:

```json
{"clock":"qorcap-mono-ms","fps_den":1,"fps_num":30,"hash_of":"encoded-video-sample","schema":"qorcap-frame-bind/v0","type":"header"}
```

One line per video frame:

```json
{"au_sha256":"<64 hex>","frame":361,"mono_ms":12045,"seg":12,"type":"frame"}
```

| Field | Meaning |
|---|---|
| `seg` | Manifest segment sequence of the fMP4 fragment that contains the sample |
| `frame` | Video frame index from the start of the recording, in sample order |
| `mono_ms` | QorCap monotonic clock, in milliseconds, when this process read the MPEG-TS packet that starts the frame. Same `Instant` as manifest `mono_ms` and `inputs.jsonl` `t_us`. Truncated to a millisecond. |
| `au_sha256` | SHA-256 of the encoded video access unit (the sample bytes inside the fragment) |

`hash_of` is `encoded-video-sample`, not decoded pixels.

**Why the encoded sample is what a referee can recompute.** The segment hash already commits to the exact fragment bytes. The sample is a slice of those bytes: `trun` sample size and `data_offset` from the start of the `moof` (`default-base-is-moof`, which the recorder mux sets). Recomputing the digest is a box walk and a SHA-256. It does not depend on ffmpeg, swscale, or color conversion. The capture command is `-bf 0` and the recorder is `-c copy`, so sample order is presentation order. A non-zero composition offset is refused (`align_gap`) rather than labeled as a timed frame.

**Why decoded pixels are not the commitment.** The scorebug reader sees pixels, and a decoded hash would match that view only for one decoder, one pixel format, and one scaler. The hosted referee's OpenCV decode of a highlight will not match a hash taken of a different ffmpeg build. An encoded hash does not prove "these pixels were read." It proves "this access unit, at this capture-clock time, is the one inside this sealed fragment." Which code interpreted the pixels is the reader sidecar below.

If the PES arrival count and the sample count disagree, or the PTS jumps backwards by more than a second, the segment gets one `align_gap` line and no per-frame times. Later segments stay gapped: once the pairing is lost, a later line would be a guess. A gap is not a frame-level time claim. The fragment is still covered by its segment hash.

## Running hash

```text
head_0 = 64 zero bytes
head_n = SHA-256(JCS({"prev": head_{n-1}, "sha256": SHA-256(line_n)}))
```

`line_n` is the exact JCS bytes of that line, without the newline. Every non-empty line counts, including the header and any `align_gap`. Blank lines are not written. A blank line inserted later is an extra hashed line and breaks the chain.

Each checkpoint's `qorcap.frame_bind` is:

```json
{"schema":"qorcap-frame-bind/v0","n":1281,"head":"<running hash after line n>"}
```

`n` is a prefix. It grows as frames arrive and must not go backwards. The last checkpoint's `n` equals the number of lines in the file. Lines after that checkpoint are unsealed: `verify-bundle` fails, and they are not a claim. Because `frames_hash` covers `frame_bind`, changing a line, dropping one, inserting one, or editing `mono_ms` changes a checkpoint hash unless the whole chain is rebuilt. Rebuilding the chain changes `hash`, which is what the referee signature covers. `verify-bundle` itself only checks consistency; the referee signature is still QorTime's job.

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
        {"kind": "pack", "path": "sidecar/packs/madden27_standard_score_110028.npz", "sha256": "<64 hex>"}
      ]
    }
  ]
}
```

`path` is relative (`sidecar/...` or `qor_receipt/...`). Absolute paths, `..`, and names ending in `.key`, `.pem`, `.p12`, or `.pfx` are rejected. Key material is not hashed and not written.

The Python sidecar builds this from modules it has already imported and from pack paths those modules actually point at (`PACK`, `PACK_FOUR`, and the same on the vendored reader when it is loaded). It POSTs the object to `/events` with `type: readers`. QorCap writes the canonical file. The sidecar does not write `readers.json` itself, so there is one byte string to hash.

Checkpoint 0 carries:

```json
{"schema":"qorcap-readers/v0","sha256":"<file hash>"}
```

or, if nothing arrived in time:

```json
{"schema":"qorcap-readers/v0","state":"unbound"}
```

Later checkpoints do not repeat it. They link back through `prev`, so editing checkpoint 0's `qorcap` still breaks the chain.

With the flag on, `record` waits up to 8 seconds before writing checkpoint 0 so the reader post can land in that checkpoint. A post after checkpoint 0 is refused and does not become a claim. Flag off does not wait and does not write the file.

`state: unbound` means there is no reader claim. It does not mean "the pinned referee reader was the one that ran."

## How a verifier checks

`qorcap verify-bundle` already recomputes each checkpoint hash and `frames_hash` from `qorcap`. It now also does the following. None of this reads a signing key.

1. If no checkpoint has `qorcap.frame_bind`, stop. Frame claim is false. Do not read `frames.jsonl` as evidence. A file sitting next to an old bundle is ignored. This is not an error.
2. If any checkpoint has `frame_bind`, every checkpoint must have it, each `schema` must be `qorcap-frame-bind/v0`, and `n` must be non-decreasing. Recompute the running hash over the first `n` lines of `frames.jsonl` and compare `head`. The last checkpoint's `n` must equal the line count.
3. On checkpoint 0, if `readers` is missing, the reader claim is false and that is not an error. If `state` is `unbound`, the reader claim is false and that is not an error. If `sha256` is set, `readers.json` must exist and hash to that value.
4. A frame claim is true only after step 2 matches. A reader claim is true only after step 3's hash matches. Skew is reported only when the frame claim is true, so a tampered file is not described as a measurement.

Skew from the sidecar alone, anchored at the first frame row so a constant offset drops out:

```text
clip_ms    = (frame - frame_0) * 1000 * fps_den / fps_num
capture_ms = mono_ms - mono_ms_0
skew_ms    = capture_ms - clip_ms
```

`fps_num` and `fps_den` come from the header. A second comparison, for the highlight cutter, interpolates manifest `(frames, mono_ms)` pairs the way `mono_bounds` does and subtracts that clip clock from the sidecar's `mono_ms`. That is the fragment-close residual. Both are integers.

Someone who wants the encoded hash itself walks the fragment named by `seg`, takes the video track's samples in order, and checks sample `frame` (within that segment's range) against `au_sha256`. This draft's verifier checks the sidecar chain, not a second walk of every sample. The chain pins the digests the recorder computed. A referee with the video can recompute a digest it cares about; that second walk is not required for `verify-bundle` to reject a rewritten line.

## Failure modes

| What happened | Result |
|---|---|
| Flag off, or an old bundle with no `frame_bind` | Bundle verifies. Frame claim is false. Reader claim is false. Not a pass. |
| `frames.jsonl` present but no checkpoint mentions it | Ignored. Not a claim and not an error. |
| Any committed line edited, including `mono_ms` | `frame_bind.head` mismatches. `verify-bundle` fails. Frame claim is false. |
| Line inserted, dropped, or appended after the last checkpoint | Same. The last `n` no longer matches the file. |
| `frame_bind` committed and `frames.jsonl` deleted | Error. Frame claim is false. Not a silent pass. |
| `readers` unbound, or absent on an old bundle | Reader claim is false. Not an error by itself. |
| `readers.json` edited or deleted after a hash was committed | Error. Reader claim is false. |
| Reader post arrives after checkpoint 0 | Refused. The file is not written. Reader claim stays unbound. |
| Sample count ≠ PES arrivals, PTS runs backwards, or a sample has a composition offset | `align_gap` for that stretch. No per-frame time claim there. The segment hash still covers the bytes. |
| Could not create `frames.jsonl` | Flag degrades to off for that process. No `frame_bind` field. No frame claim. |
| Millisecond truncation | `mono_ms` drops the sub-millisecond part of the same `Instant`. That is at most 1 ms, which does not explain a 120 ms skew. |
| Encoded hash vs pixels | A referee that only decodes cannot check `au_sha256`. A disagreement between this reader and a pinned reader is a reader-hash mismatch, not a frame-hash mismatch. |
| Full re-seal of checkpoints and sidecar | `verify-bundle` sees a consistent chain. The referee signature over the old `hash` fails in QorTime. This draft does not pretend `verify-bundle` is the referee. |

## Cost

Flag off: no file, no extra hash, no wait, checkpoints unchanged.

Flag on, 30 fps:

- One extra SHA-256 per video sample, on bytes the segment hash already reads. At a few megabits per second this is noise next to the encode.
- One small JCS hash per sidecar line.
- `frames.jsonl` is about 160 bytes a line, about 20 MB per hour.
- `readers.json` is one small file, hashed once.
- `verify-bundle` walks the sidecar once, in order, and extends the running hash. A three-hour match is a few hundred thousand lines, one sequential read.
- Checkpoint 0 can wait up to 8 seconds for the reader post. That wait is the part that can press on QorTime's 10 second first-checkpoint gap. It is an open question below, not a change to the gap rule.

## Old bundles

A bundle with no `frame_bind` and no `readers` on its checkpoints verifies as before. Absence means "this bundle makes no frame-level claim and no reader-identity claim." It must not be read as "frame times checked out" or "the pinned reader produced these reads."

Code that asks "what is the capture time of frame 361?" returns no claim when the sidecar is missing or failed. It does not fall back to the fragment-close fit and call that a frame measurement. The fragment-close fit remains the clock `--frame-bind` does not touch, including input-proof alignment.

## Flag

```text
qorcap record --frame-bind
QORCAP_FRAME_BIND=1
```

Same words as the other QorCap switches: `1`, `true`, `yes`, `on`. Default off. Start Match adds `--frame-bind` and posts the reader manifest only when the environment variable is set, so a normal match does not wait and does not write the files.
