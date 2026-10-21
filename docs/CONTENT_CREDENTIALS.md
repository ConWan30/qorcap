# Content Credentials on a QorCap clip

QorCap's signed match receipt (`receipt.json`, schema `qor-receipt/v0`) is the source of truth for a result. A shared clip can carry a [C2PA](https://c2pa.org/) manifest that binds that file to the receipt by hash. Anyone can read the manifest with standard Content Credentials tools and, with the match bundle, check that the clip really came from that receipt and that segment.

`qorcap clip export` is off unless you ask for it. `receipt.json` is not modified. Start Match and Finalize Last Match are different: after the receipt is signed they cut highlight clips and then attach credentials. `--no-auto-highlights` opts out. That step does not set `QORCAP_CONTENT_CREDENTIALS`, so `qorcap record` and `clip export` stay default-off.

## Turn it on

```
qorcap clip export --bundle MATCH_DIR --start-mono-ms 12000 --end-mono-ms 20000 --out clip.mp4 --content-credentials
```

The same switch is the environment variable `QORCAP_CONTENT_CREDENTIALS` set to `1`, `true`, `yes`, or `on` (the same words as `QORCAP_FRAME_CLOCK`). Default is off. With it off, the output is the ffmpeg cut and nothing else: no manifest, no extra bytes.

`--start-mono-ms` and `--end-mono-ms` are on the match's own monotonic clock (the `mono_ms` in `manifest.jsonl`). The range is `[start, end)`. The cut uses ffmpeg, which QorCap already uses: stream copy first, and a libx264 re-encode only if copy fails.

Check a clip:

```
qorcap clip-verify clip.mp4 --bundle MATCH_DIR
```

Exit 0 is a pass, 1 is a fail with `reason:` lines, 2 means the clip path could not be opened. Without `--bundle` the signature can still be read, but the hash check fails closed (`hashes were not recomputed`).

`c2patool clip.mp4` (c2patool 0.28.2 reads a manifest written by c2pa 0.91.4) and [contentcredentials.org/verify](https://contentcredentials.org/verify) also read the manifest. They do not recompute QorCap's hashes. That recomputation is `clip-verify`.

`c2patool` reports `signingCredential.untrusted` for this key. Its `validation_state` can still say `Valid`: that means the signature math and the BMFF hash checked, not that the signer is on the C2PA trust list. The failure entry is the untrusted certificate. Trust in the match result still comes from the receipt.

## What is embedded

One custom assertion, label `org.qorcap.match-receipt-v0`. The label is hyphenated on purpose. c2pa-rs treats a dotted `.vN` suffix as an assertion version and drops it from the published label when N is below 2, so `org.qorcap.match-receipt.v0` would show up as the unversioned name `org.qorcap.match-receipt`. The hyphen keeps the version visible.

The assertion holds hashes and identifiers only:

| Field | Meaning |
| --- | --- |
| `receipt_sha256` | SHA-256 of the exact `receipt.json` bytes |
| `match_id` | `match.match_id` from that receipt |
| `start_mono_ms`, `end_mono_ms` | Clip range on the match clock |
| `start_frame`, `end_frame` | That range on the 30 fps frame clock interpolated from `manifest.jsonl` |
| `video_span_sha256` | SHA-256 of the manifest segment bytes the range overlaps, in sequence order |
| `video_segment_seqs` | Those segment sequence numbers. The file stores a JSON array. c2patool 0.28.2 prints that one field as base64 (`AA==` is `[0]`) because it re-encodes an unknown assertion through CBOR. `clip-verify` prints the numbers. |
| `video_chain_head` | Video hash-chain head from `qorcap verify-bundle` |
| `input_span_sha256` | SHA-256 of JCS of the `inputs.jsonl` records whose `t_us` is in the range, or of `[]` when the file is absent |
| `qorcap_version` | The QorCap version that signed the clip |
| `verification_status` | `verified`, `self_attested`, `partial`, or `fail` |
| `verifier_status` | QorTime's own word: `VERIFIED`, `SELF_ATTESTED`, `PARTIAL`, or `FAIL` |
| `verified` | True only when `verification_status` is `verified` |
| `edit` | `trimmed+repackaged` (stream copy) or `trimmed+transcoded` |

The manifest also records the source span as a C2PA parent ingredient (`parentOf`, title `qorcap match video span`) and the actions `c2pa.opened`, `c2pa.edited`, and either `c2pa.repackaged` or `c2pa.transcoded`. The ingredient's description repeats `video_span_sha256`. The span bytes are not copied into the clip. c2pa-rs's ingredient `hash` field is a builder dedup hint and is not the C2PA data hash of those bytes; the hash that `clip-verify` recomputes is the one in the custom assertion.

There is no timestamp authority and no network call. OCSP and remote-manifest fetch are off.

## What is not embedded

No raw input log, no usernames, no file paths, no device names, no device serials, no gamertags, no player ids. A device name or a gamertag can sit in `manifest.jsonl` or `inputs.jsonl` and still be covered by a hash. The clip stores the hash, not the text.

Nothing is added to `receipt.json`. That file rejects extra fields (`additionalProperties: false`). New data lives in the clip manifest.

## After each match

Start Match (`start-match.ps1`) and Finalize Last Match pass `--auto-highlights`. The order is fixed:

1. Recording stops. A fresh `.qorcap-run/recording.lock` means highlights do not run.
2. The receipt is signed and verified. That result is what gets submitted.
3. `qorcap highlights` cuts `highlights/`. The burned-in title stays **SCOREBUG LOCK**.
4. `qorcap clip sign` signs each clip in place when the receipt verifies (`VERIFIED`, `SELF_ATTESTED`, or `PARTIAL`, and `verify-bundle` is clean).
5. The submit prompt runs. A highlights or credentials failure does not change its exit, the receipt bytes, or whether it is offered.

`verified: true` on a clip still requires the verifier's own word `VERIFIED`. A local Start Match receipt is `SELF_ATTESTED`, so the clip is signed with `verified: false`. If the receipt does not verify, step 4 leaves the clip bytes alone. `highlights/manifest.json` then says `attached: false`, `verified: false`, and `reason: receipt did not verify`. The picture never says VERIFIED.

`qorcap clip sign` does not read `QORCAP_CONTENT_CREDENTIALS`. The flag that keeps `clip export` byte-identical stays off for the recorder.

## Honesty rule

`verified: true` is written only when both of these are true:

1. `python -m qor_receipt.cli verify` returns `VERIFIED`.
2. `qorcap verify-bundle` reports no errors.

Anything else is not upgraded:

| Verifier | Bundle | Clip |
| --- | --- | --- |
| `VERIFIED` | clean | `verified: true`, status `verified` |
| `SELF_ATTESTED` | clean | `verified: false`, status `self_attested`. A local QorCap referee is this case. |
| `PARTIAL` | clean | `verified: false`, status `partial` |
| `FAIL`, unknown, or bundle errors | | Refused. No credentialed file is written. Highlight clips are still exported, unsigned, with `verified: false`. |

A missing QorTime checkout is also a refusal. Clip export does not download QorTime and does not invent a status. Pass `--qortime DIR` or set `QORTIME` to a checkout that contains `qor_receipt/`. The pin QorCap already uses is `0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2`.

`clip-verify` runs that check again. `RESULT: PASS` means the C2PA signature validates and the recomputed hashes and the fresh verifier status match the manifest. It does not mean a referee marked the match `VERIFIED`. The report says `verified: false` when that is the truth, and it says so in a note. A tampered clip fails the signature. A different bundle fails a hash comparison (`receipt hash does not match`, `video span hash does not match`, and the same shape for the input span and the chain head). A receipt the verifier rejects fails with `receipt did not verify`. A manifest that claims `verified` when the verifier does not fails with `manifest says verified but the receipt verifier does not`.

## The signing key is self-signed

The receipt key is Ed25519 over JCS (`device.key` / `referee.key`). C2PA wants an X.509 certificate. Those are different jobs, so the clip key is not the receipt key. The clip is tied to the receipt by `receipt_sha256`, which anyone can recompute from the file.

On first use QorCap writes a local ES256 key:

- Windows: `%LOCALAPPDATA%\QorCap\keys\c2pa-es256.cert.pem` and `c2pa-es256.key.pem`
- Elsewhere: `~/.qorcap/keys/` (or `$QORCAP_HOME/keys`)

The certificate subject is `QorCap self-signed content credentials`. It is not on the C2PA trust list. `c2patool` and contentcredentials.org/verify will show the signer as untrusted. That is the honest result. The useful check is the binding: the signature shows the manifest was not edited after signing, and `clip-verify --bundle` shows the hashes still match the receipt and the segment. Trust in the result still comes from the receipt's referee, not from this certificate.

## Build notes

The crate is `c2pa` 0.91.4 with default features off and `rust_native_crypto` on. That is pure Rust (`p256`, `ed25519-dalek`, `rsa` via `c2pa_raw_crypto`). The default OpenSSL backend and the HTTP client (remote manifests, TSA, OCSP) are not compiled, which is what keeps the Windows x64 build free of a system OpenSSL. The certificate is minted with `rcgen` using the `ring` backend. The Windows CI job is `cargo test --locked` and `cargo build --release --locked` on `windows-latest`.

## Limits

- The signer is self-signed. A standard validator will not call it trusted.
- A local receipt stays `self_attested`. `clip-verify` can pass while `verified` is false. That is the point of the honesty rule.
- The parent ingredient names the span and its hash. It does not embed the span, and it is not a full C2PA data-hash of those bytes. c2patool reports `ingredient.unknownProvenance` for that ingredient, which is expected: the span was not itself a credentialed asset.
- The C2PA BMFF hash excludes `ftyp`, `free`, `skip`, `mfra`, and the `uuid` box that holds the manifest. Changing the picture (`mdat` / `moov`) fails the signature. Changing only `ftyp` does not. `clip-verify` does not re-hash the clip's picture against the span, because the cut is a remux or a re-encode. It re-hashes the bundle segments the manifest names.
- Standard validators do not know what `org.qorcap.match-receipt-v0` means. They can show it. `clip-verify` is what checks it against a bundle.
- Highlight clips keep the SCOREBUG LOCK title. Credentials, when attached, are the C2PA manifest on those files. `qorcap highlights` run by hand does not sign them; Start Match does, after the receipt exists.
- There is no live signing of the capture as it is recorded. Credentials are attached to a clip cut after the match.

ProofMode (Guardian Project) signs media with C2PA and OpenTimestamps. Truepic, Streamplace, and several cameras sign video or stills the same family of way. What this adds is a game-result receipt: the clip points at one signed match, one clock range, and the hash of the segment it was cut from.
