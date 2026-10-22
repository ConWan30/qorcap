# Red team

Synthetic bundles built from the in-tree fixtures (`humanlike::synth`, the hash chain, Ed25519 over JCS, `contracts/receipt.v0.json`). Each row is scored by the checks that already ship. No threshold was widened and no check was skipped to change a verdict.

Run `qorcap redteam` to print the JSON matrix. `qorcap redteam --out tests/redteam/matrix.v0.json --markdown docs/REDTEAM.md` rewrites these two files. `cargo test --test redteam` checks that a fresh run matches both files.

Rule v2 ceiling is 2500 ms. Successful latencies must sit within ±300 ms of their median (a gap of exactly 300 ms passes). `verify-bundle` re-hashes bytes. It does not score human-likeness, alignment, the controller-identity claim, the receipt schema, or rule v2.

## Matrix

| id | attack | verdict | caught by | verify-bundle |
|---|---|---|---|---|
| `honest_control` | control | pass | — | OK |
| `replay_stale_no_fresh_nonce` | pre-recorded video replayed as if live | caught | challenge rule v2 | OK |
| `relay_0_5s` | constant relay delay, video versus pad log | weak | — | OK |
| `relay_1_0s` | constant relay delay, video versus pad log | weak | — | OK |
| `relay_1_5s` | constant relay delay, video versus pad log | weak | — | OK |
| `relay_2_0s` | constant relay delay, video versus pad log | weak | — | OK |
| `relay_2_4s` | constant relay delay, video versus pad log | weak | — | OK |
| `relay_3_0s` | constant relay delay, video versus pad log | caught | challenge rule v2 | OK |
| `virtual_capture_device` | fake or virtual capture device identity | missed | — | OK |
| `splice_sealed_segment` | edited or spliced video segment | caught | hash chain | FAIL |
| `splice_rehashed_resigned` | edited or spliced video segment | weak | — | OK |
| `turbo_macro` | turbo or macro input | caught | humanlike check | OK |
| `second_pad` | second pad mid-match | caught | input proof | OK |
| `receipt_tamper_score` | receipt field tampering | caught | signature | OK |
| `receipt_extra_field` | receipt extra field | caught | schema | OK |

`weak` means a layer reports the attack and still accepts the bundle. `missed` means every layer accepts it. `caught` means a layer rejects it. The human-likeness catch is advisory: the label is bot-like and `verify-bundle` stays OK. The controller-identity catch fails the input-proof `controller_corroboration` claim; `verify-bundle` stays OK.

## Missed and weak rows

### `relay_0_5s`

constant relay delay, video versus pad log — 0.5 s.

Rule v2 accepts a constant latency of 500 ms (ceiling 2500 ms, and every success within ±300 ms of the median). The aligner reports the same delay as offset_ms and still scores a match; its search is ±3 s so a fixed pipeline delay is calibration, not a failure. Software can close a delay of this size by bounding accepted latency to the band measured on a live capture (recorded pause onsets sit near 1750 ms, spread under 100 ms). This suite does not add that bound. A relay smaller than that honest spread would remain inside the band.

Software can close this. The check is not added in this suite.

- **input-video alignment** (weak): injected delay 500 ms; match_permille 1000 chance_permille 0 offset_ms 700 presses_tested 100. The ±3 s search keeps the match and records the delay as the offset; nothing rejects the bundle

### `relay_1_0s`

constant relay delay, video versus pad log — 1.0 s.

Rule v2 accepts a constant latency of 1000 ms (ceiling 2500 ms, and every success within ±300 ms of the median). The aligner reports the same delay as offset_ms and still scores a match; its search is ±3 s so a fixed pipeline delay is calibration, not a failure. Software can close a delay of this size by bounding accepted latency to the band measured on a live capture (recorded pause onsets sit near 1750 ms, spread under 100 ms). This suite does not add that bound. A relay smaller than that honest spread would remain inside the band.

Software can close this. The check is not added in this suite.

- **input-video alignment** (weak): injected delay 1000 ms; match_permille 990 chance_permille 0 offset_ms 1200 presses_tested 100. The ±3 s search keeps the match and records the delay as the offset; nothing rejects the bundle

### `relay_1_5s`

constant relay delay, video versus pad log — 1.5 s.

Rule v2 accepts a constant latency of 1500 ms (ceiling 2500 ms, and every success within ±300 ms of the median). The aligner reports the same delay as offset_ms and still scores a match; its search is ±3 s so a fixed pipeline delay is calibration, not a failure. Software can close a delay of this size by bounding accepted latency to the band measured on a live capture (recorded pause onsets sit near 1750 ms, spread under 100 ms). This suite does not add that bound. A relay smaller than that honest spread would remain inside the band.

Software can close this. The check is not added in this suite.

- **input-video alignment** (weak): injected delay 1500 ms; match_permille 990 chance_permille 0 offset_ms 1700 presses_tested 100. The ±3 s search keeps the match and records the delay as the offset; nothing rejects the bundle

### `relay_2_0s`

constant relay delay, video versus pad log — 2.0 s.

Rule v2 accepts a constant latency of 2000 ms (ceiling 2500 ms, and every success within ±300 ms of the median). The aligner reports the same delay as offset_ms and still scores a match; its search is ±3 s so a fixed pipeline delay is calibration, not a failure. Software can close a delay of this size by bounding accepted latency to the band measured on a live capture (recorded pause onsets sit near 1750 ms, spread under 100 ms). This suite does not add that bound. A relay smaller than that honest spread would remain inside the band.

Software can close this. The check is not added in this suite.

- **input-video alignment** (weak): injected delay 2000 ms; match_permille 990 chance_permille 167 offset_ms 2200 presses_tested 100. The ±3 s search keeps the match and records the delay as the offset; nothing rejects the bundle

### `relay_2_4s`

constant relay delay, video versus pad log — 2.4 s.

Rule v2 accepts a constant latency of 2400 ms (ceiling 2500 ms, and every success within ±300 ms of the median). The aligner reports the same delay as offset_ms and still scores a match; its search is ±3 s so a fixed pipeline delay is calibration, not a failure. Software can close a delay of this size by bounding accepted latency to the band measured on a live capture (recorded pause onsets sit near 1750 ms, spread under 100 ms). This suite does not add that bound. A relay smaller than that honest spread would remain inside the band.

Software can close this. The check is not added in this suite.

- **input-video alignment** (weak): injected delay 2400 ms; match_permille 990 chance_permille 167 offset_ms 2600 presses_tested 100. The ±3 s search keeps the match and records the delay as the offset; nothing rejects the bundle

### `virtual_capture_device`

fake or virtual capture device identity — OBS Virtual Camera written at seal time.

device and device_id are labels on the manifest line. The hash link is prev, seq, and the segment sha256, and verify-bundle does not read the device fields. An unmodified recorder can refuse a known virtual-camera name when it opens the device. A recorder willing to lie writes any name, and the bundle still verifies. Proving the frames entered through a physical HDMI capture device takes attestation of that device, or a hash of the HDMI stream taken before a PC can substitute a virtual camera.

Closing this takes hardware attestation or capture-before-PC.


### `splice_rehashed_resigned`

edited or spliced video segment — segment replaced, chain and receipt re-signed with the software key.

The sealed-segment row fails the hash chain. This row rebuilds the manifest, the checkpoint chain, and both receipt signatures over the edited bytes, so those checks agree with the new file. The official referee re-hashes the segments it samples and re-reads the scorebug on that sample; a splice outside the sample stays consistent with the new chain (spot-check miss probability is about (1-f)^16 plus the forced tail and challenge segments). A head fixed before the file can be edited is capture-before-PC. A co-signer who already holds the pre-edit head would reject the new signature; a first submit of the rebuilt bundle has no earlier head on record.

Closing this takes hardware attestation or capture-before-PC.

- **referee-only** (weak): A fresh submit is hashed against the manifest the submitter just built. The sampled scorebug re-read can miss a splice outside that sample. Weak, and it does not close the row.

## Caught rows, in short

- `replay_stale_no_fresh_nonce`: challenge rule v2: kickoff: no_pause_press; halftime: no_pause_press; end: no_pause_press
- `relay_3_0s`: challenge rule v2: kickoff: video_latency_too_high; halftime: video_latency_too_high; end: video_latency_too_high
- `splice_sealed_segment`: hash chain: segment 3 (video.mp4@288): SHA-256 mismatch (video bytes altered)
- `turbo_macro`: humanlike check: label bot-like patterns (macro/turbo/injection suspected), score_permille 50. Advisory: verify-bundle stays OK and input_proof status stays ok
- `second_pad`: input proof: controller corroboration FAIL: pad identity changed mid-match (serial:AAAA1111 -> serial:BBBB2222)
- `receipt_tamper_score`: signature: player signature: signature error: Verification equation was not satisfied
- `receipt_extra_field`: schema: /device_attest: additional property

## Control

`honest_control` is `pass`. A clean bundle with a 1750 ms pause latency, human-like presses, and an intact chain is accepted by every layer. That is the check that this suite did not invert a rule.

## Bugs

No fail-closed check accepted a sealed one-byte splice or an extra top-level receipt field. The missed and weak rows are checks the current code does not make. No verification rule was edited.
