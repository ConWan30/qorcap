# Glimpse witness, burst, and attestation (cheap tier)

Observer-only. These three pieces sit beside watcher `glimpse-watch/0.1.3`. With every flag off, the watcher sends the same frames it sends today and `advisory.jsonl` bytes match a second flag-off run. Nothing here writes `receipt.json`, a score lock, a challenge, or a finalize result.

Design and the state machines: [PR #50](https://github.com/ConWan30/qorcap/pull/50) (`docs/GLIMPSE_TEARDOWN.md` on that branch). This file is the operator note for the cheap tier that was approved.

## Flags

The watcher and `qorcap.exe` treat every flag as off when it is unset. That is what the flag-off tests pin.
Start Match is the default for a double-clicked match: it sets the first three to `1`, sets
`QORCAP_GLIMPSE_FRAME=1` so `GET /frame/scorebug` is served, and starts the advisory watcher. It sets
`QORCAP_GLIMPSE_BURST_RING`, `QORCAP_GLIMPSE_CLIP_AUDIT`, and `QORCAP_GLIMPSE_TD_VETO` to `0`.

| Flag | Start Match | Piece |
|---|---|---|
| `QORCAP_GLIMPSE_WITNESS=1` | on | Unhinted read of frames the reader already locked. Own 45 s clock. Does not reset the ordinary 6 s clock. |
| `QORCAP_GLIMPSE_BURST=1` | on | On a disagreement, one extra unhinted crop (8 events per match). Reuses a witness or touchdown reply that was already unhinted. |
| `QORCAP_GLIMPSE_ATTEST=1` | on | After the match, sign `glimpse_attestation.json` with the observer key. |
| `QORCAP_GLIMPSE_BURST_RING=1` | off | Recorder keeps 8 frames / 16 s in memory and serves `GET /frame/burst-ring`. Leave this off until the capture A/B below passes. The cheap path uses frames the watcher already pulled. |
| `QORCAP_GLIMPSE_CLIP_AUDIT=1` | off | Optional. One unhinted frame per highlight clip, 15 clips, then the same attestation file. Off in the cheap running set. |
| `QORCAP_GLIMPSE_ATTEST_COPY=1` | off | Copy the attestation into the match folder. Default: leave the match folder alone. |

`QORCAP_GLIMPSE_TD_VETO` stays off. Start Match never turns it on. Witness, burst, and attest do not write `td_verdicts.jsonl`. The existing touchdown second opinion still does, and the reader still ignores that file unless the veto flag is on.

Opt out for one match: `Start Match.cmd --no-glimpse` turns the frame tap, the watcher, witness, burst, and attest off. `--no-glimpse-witness`, `--no-glimpse-burst`, and `--no-glimpse-attest` each turn one switch off.

Sidecars live under `%LOCALAPPDATA%\QorCapGlimpse\<match>\`:

- `advisory.jsonl` gains `kind: "witness"` (`feeds_lock: false`)
- `burst.jsonl` and `burst/` for a disagreement
- `highlights_audit.jsonl` when clip audit is on
- `glimpse_attestation.json` (`qorcap-glimpse-attestation/v0`)

`receipt.v0.json` is unchanged. The attestation is not a receipt and is not a score.

## Caps

`W` is `QORCAP_GLIMPSE_WEEKLY_LIMIT`, else `--weekly-limit` (default **$10**). `G` is `QORCAP_GLIMPSE_GAME_CAP`, else `--game-cap` (default **$0.50**). Stratum caps default to the cheap tier and must be greater than 0 and no larger than `G`:

| Stratum | Env | Cheap default |
|---|---|---|
| Witness | `QORCAP_GLIMPSE_WITNESS_CAP` | $0.08 (about 100 successful crops) |
| Burst | `QORCAP_GLIMPSE_BURST_CAP` | $0.02 |
| Clip audit | `QORCAP_GLIMPSE_CLIP_CAP` | $0.03 |

A missing, blank, non-numeric, or non-positive `W` or `G` refuses the call with `refused: cap_unconfigured` and does not open a socket. The same refusal is used when a stratum cap is missing or larger than `G`. A configured cap that `R` ($0.00135, the worst-case reservation) would pass is refused before the socket and booked only when a call was actually attempted. Failed calls book `R` on the local ledger (`ledger.jsonl`). Spend is one number across kinds.

The running default stays inside **$10/week** until you set `W`. Witness plus burst, if every match uses the whole stratum, is about **$0.082 per match** and about **$2.05 per week at 25 matches** (witness ~$0.076, burst ~$0.006, attest $0). That is the bill when the quotas fill. Healthy matches spend less. The $4–8 band in the teardown also included learning and menu frames, which are not in this build.

Start Match already sets witness, burst, and attest, and it does not set `W` or `G`, so the caps stay
**$10/week** and **$0.50/game**. The same switches by hand:

```bat
set QORCAP_GLIMPSE_FRAME=1
set QORCAP_GLIMPSE_WITNESS=1
set QORCAP_GLIMPSE_BURST=1
set QORCAP_GLIMPSE_ATTEST=1
rem leave QORCAP_GLIMPSE_BURST_RING, QORCAP_GLIMPSE_CLIP_AUDIT, and QORCAP_GLIMPSE_TD_VETO unset or 0
```

The watcher process is separate from capture. When witness or burst is on, those model calls run on one low-priority background lane and the extra call is dropped if that lane is busy. `offer` on the recorder still uses `try_lock` and never waits. If `OPENROUTER_API_KEY` is missing, Start Match logs one line (`%LOCALAPPDATA%\QorCapGlimpse\start-match.log` and the Start Match window) and the watcher sends nothing. Recording is not blocked or slowed.

## Observer key

```bat
python sidecar\qorcap_glimpse_attest.py --init-key
```

That writes `%LOCALAPPDATA%\QorCap\keys\glimpse-observer.key` (32-byte seed, mode 0600 on POSIX). Start Match calls this once when the file is missing and attest is on. A failure there is logged and recording continues. Attest does not create the key by itself. If the file is missing, the sidecar is `signed: false` with `reason: no_observer_key`. If the seed or the public key matches `device.key` or `referee.key`, it does not sign (`reason: key_collides_with_authority`).

Start Match runs the attestation after the receipt is signed (Finalize Last Match does too), with `QORCAP_GLIMPSE_ATTEST=1`.

The signature is Ed25519 over RFC 8785 JCS of the body, `key_kind: glimpse-observer`. `status: observed` is only set when that signature checks and `receipt_sha256` is the hash of the receipt bytes. A FAIL receipt can still be attested. The file does not say PASS and does not carry `away_score`, `home_score`, `winner`, `trust_tier`, or a receipt `signatures` object.

A fresh `.qorcap-run/recording.lock` (newer than 30 s) exits 3 and writes nothing. Run it after the match:

```bat
set QORCAP_GLIMPSE_ATTEST=1
python sidecar\qorcap_glimpse_attest.py --match-dir "C:\Users\you\Documents\QorCap Matches\match-..." --key-dir %LOCALAPPDATA%\QorCap\keys
```

## A/B

Start Match already runs witness, burst, and attest. The burst ring stays off until the ring measurement below has been run. Same card, same fps, flag off then flag on. One match does not promote the ring.

**Witness (live).** Zero new wrong score reads, no missed locks, no extra capture drops, no extra audio lag, no reader stall. The watcher is a separate process; confirm `GET /snapshot.jpg` and the reader loop stay on their usual cadence (the frame tap already skips a busy copy). Compare locks and `receipt.json` to the flag-off recording of the same session shape. A witness line is advisory. It must not move a lock.

**Burst ring (live, only if you intend to set `QORCAP_GLIMPSE_BURST_RING`).** The cheap tier leaves the ring off, so this gate is for the ring flag alone. PR #47 / `docs/FRAME_CLOCK_AB.md` is the measurement shape: same card, ring off versus ring on, zero new wrong reads, no extra drops, no extra lag. `GET /frame/burst-ring` must not change `GET /frame/scorebug` timing. A held ring mutex drops that sample and still updates the latest frame.

**Attest and clip audit (post-match).** The recorder is stopped and `recording.lock` is stale. `receipt.json` bytes, highlight clip bytes, and `highlights/manifest.json` bytes are identical before and after. `qortime verify` and `qorcap verify-bundle` return the same result. The match folder gains no file unless `QORCAP_GLIMPSE_ATTEST_COPY=1`, and that copy is the only new name (`glimpse_attestation.json`).

## Replay

Tests in `sidecar/test_qorcap_glimpse_witness.py` replay saved crops (`sidecar/fixtures/m27_mnp_q1.jpg`, `m27_mnp_q2.jpg`) with a fake model. The 13-8 versus 13-0 case uses a match folder named `match-20261017-021003` and those crops, because this repo has no scorebug JPEG from that match (only PSN name crops). The fast path is exercised directly: a 0.09-margin 13-0 → 13-8 look-alike does not post, and a clean +7 still posts while a `burst.jsonl` row says disagree and the veto stays unset.
