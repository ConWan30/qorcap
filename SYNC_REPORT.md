# QorCap <-> QorTime sync report (2026-10-16)

Audited: QorCap main `9909a00` (v0.3.9, PR #13), QorTime main `9d94877` (after #24 verified uploads and #25).
The canonical contract registry now lives in **qortime `schema/contracts/`** (`compat.json` plus JSON Schemas). QorCap
vendors a byte-identical copy in `contracts/` (`SHA256SUMS`, `CONTRACTS_PIN`). Tests on both sides fail on drift.

## Contracts: where each side defines them, and status

| Contract | QorTime | QorCap | Status |
|---|---|---|---|
| Bundle file names (meta/required) | `review/bundle.py` META_FILES/REQUIRED_FILES | `sidecar/qorcap_submit.py` META_FILES | Duplicated but identical. Now pinned to `compat.bundle_files` on both sides. |
| bundle.json schema `qorcap-bundle/v0` | read by `review/bundle.py` | written in `src/engine.rs` | No schema file existed. Added `qorcap-bundle.v0.json`; the real writer output is validated against it. |
| checkpoints.jsonl line | `checkpoints.py` (`body_of`, `make_checkpoint`) | `src/evidence.rs` CheckpointChain, plus a `qorcap` extras object | Same rule. Added `qorcap-checkpoint.v0.json` (trigger enum includes `stop`). |
| Receipt `qor-receipt/v0` | `schema/receipt.v0.json`, `receipt.py`, `verify/verify.js` | built through QorTime's `receipt.build` (sidecar) | One builder, no drift. QorCap now vendors the schema. |
| Hash chain / JCS / genesis | `jcs.py`, `review/bundle.link` | `evidence.rs` jcs/link | `jcs_vector.json` is identical on both sides. Rules are written down in compat.hash_chain, and the Rust test asserts them. |
| Signature / key_id | `keys.py` (Ed25519, 16-hex key_id) | sidecar uses QorTime FileKey | One implementation. |
| Trusted referees | `data/trusted_referees.json` = `verify/trusted_referees.json` (+ `.inc.js`) | `qorcap_submit.OFFICIAL_PUB` hard-coded | All equal (`qortime-v1`, `a5aa0c91…feea`, key_id `53eea1b5d23b1ff0`). Tests pin all copies to compat.official_referee. |
| Checkpoint timing (max gap 10 s, skew 3 s) | `checkpoints.py` and `verify.js` (duplicated) | no verifier; recorder stop/seal in `engine.rs` | Py and JS are equal and now test-pinned. See the root cause below. |
| Challenges (hid_sha256, timing, checkpoint bind) | `receipt.py`, `verify.js` challengeEvidenceErrors | `evidence.rs::pattern_for` (port) | Port matches. Covered by the existing JS and Py tests. Live challenges need a real match. |
| input_advisory / input proof | `receipt._input_proof_advisory`, `verify.js` inputProofAdvisory | `inputproof.rs` (`qorcap-input-proof/v0`), `inputs.rs` (`qorcap-inputs/v0`) | Schema strings are now registered and Rust-tested. |
| end_menu_read / final_confirmed / final_basis | `receipt.py` RESULT_METHODS, schema enums | sidecar draft | Equal. Pinned to compat (schema enums plus RESULT_METHODS). |
| Profiles | only `madden27_standard` known to QorTime | `madden27_standard` and `madden27_monday_night` (sidecar pack) | Free-form string in the schema, so no verifier conflict. Registered in compat. QorTime's vendored reader has no Monday Night pack, so the official referee can't re-read MNP finals (unsynced, see below). |
| CLI / verdict vocabulary / exit codes | `qortime verify` exits 0 for VERIFIED and SELF_ATTESTED, 1 PARTIAL, 2 FAIL | `sign_and_report` mapped only VERIFIED/PARTIAL/FAIL | **DRIFT, fixed** (see below). |
| Version compatibility | n/a | `QORTIME_PIN` = e9c5c95 | compat.recorder_versions: tail timing is sound from 0.3.3. 0.3.0 to 0.3.2 recordings have a known bad tail. |

## Drift found and fixed

1. **QorCap Start Match and Finalize crashed at the very end (`KeyError: 'SELF_ATTESTED'`, exit 6).** Since
   QorTime's trusted-referee allowlist, a local receipt verifies as SELF_ATTESTED. `sign_and_report` had no exit code
   for that, so every v0.3.9 run printed "PROBLEM: unexpected error" after writing receipt.json, and the
   "Submit for Verification?" prompt was **never reached**. QorCap's own two e2e tests were red on main for the same
   reason. Fixed with `EXIT_CODES` covering every verdict, and the tests were updated. **Needs a QorCap sidecar re-package (fold into v0.3.9 / v0.3.9.1).**
2. The checkpoint timing constants were duplicated in the Py and JS verifiers. They are now asserted equal to compat.
3. There were no JSON Schemas for bundle.json, checkpoint lines, or the allowlist. Added, and the real writer output is validated.

## Root cause: real matches rejected on their last checkpoints

* live-match-125430 = QorCap **0.3.1** and live-match-143212 = **0.3.2** (bundle.json `qorcap_version`). Per QorCap's
  README, match-20261016-170000's tail failures are also v0.3.2 recordings. In those builds the sidecar countersigned
  the tail only after QorCap exited (after the input-proof work), so the last checkpoints were stamped +28 s and +113 s
  after their `t_ms`. That produces `gap_too_long`+`clock_skew` on cp[n-2] and `clock_skew` on cp[n-1], exactly the
  reported pattern. cp[0] `gap_too_long` on 125430 is a slow genesis: the first referee stamp came 10.1 s after `started_ms`.
* **QorTime's verifiers are right.** A referee stamp taken half a minute late is not a live observation. These
  recordings cannot be repaired honestly.
* QorCap ≥ 0.3.3 already writes a sealed `stop` checkpoint and waits for `/countersigned`, and it writes the genesis
  checkpoint at start. On v0.3.9 test-pattern runs, stop skew was about 120 ms and the genesis gap about 200 ms.
  New golden tests on both sides cover this: `v032_late_stop_tail`, `v033_sealed_stop_tail`, `slow_genesis`,
  `finalize_offline`, plus a real-writer stop-timing test. No recorder code change was needed.

## Tests

* QorTime: `tests/test_contracts.py` (constants vs compat, schema validity, allowlist copies, 10 golden fixtures
  regenerated deterministically by `scripts/make_contract_fixtures.py`) and `verify/contract.test.mjs` (the JS
  verifier gives the same verdict and errors as Python on every fixture, added to CI).
* QorCap: `tests/contracts.rs` (Rust constants vs vendored compat) and `sidecar/test_contracts.py`. The latter
  checks the vendored copy is intact and equal to the qortime checkout, and the sidecar constants. Its E2E part runs
  the **real** Start Match test-pattern recording, then checks: good → both verifiers OK; tampered score/video,
  foreign/official-claimed key, missing checkpoint, and old-recorder late tail → FAIL; stop checkpoint meets timing.
  QorCap has no CI workflow (no workflow-scope token), so run these locally:
  `cargo build --release && QORTIME=../qortime python -m pytest -q sidecar`.

## Still unsynced / unverified

* Monday Night (`madden27_monday_night`) finals can only be re-read by QorCap's sidecar pack. QorTime's official
  re-read has no MNP pack.
* Live paths that only a real match exercises: official live challenges on a DualSense, card audio, the end-menu
  read on real frames, and Windows Ctrl+C/window-close stop timing over a full-length match.
* `QORTIME_PIN` in qorcap_match.py is bumped to qortime `a67279d` (contains `schema/contracts`) in QorCap PR #14.
