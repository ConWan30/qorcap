# RLR Stage A — offline digital R2 dwell and inter-press intervals

Host-observed description of the digital R2 bit in a completed `inputs.jsonl` copy.
Claim ceiling: descriptive host-observed input timing; not reaction time, not skill, not a measurement of the player.
Novelty UNASSESSED. Performance on real recordings is UNMEASURED. This tool does not count, rank, or upgrade a disposition.

## What it is

`sidecar/qorcap_rlr_stage_a.py` is a stdlib-only Python 3 CLI. The caller passes one explicit file (`--inputs`) or one explicit folder (`--session-dir`, which reads only `inputs.jsonl` and, if present, declared `receipt.json` fields `match.game` and `match.match_id`). It does not discover sessions, poll for new files, open a device, read video, or launch capture.

Output is a new JSON file at `--out`. The tool refuses an output inside the source directory, an output that is the input (including symlink and `..` aliases, compared with `realpath` and case-folding), and an output that already exists. It does not write temporary files next to the source. Source bytes are hashed before and after; a change is an error.

## Run

```
python sidecar/qorcap_rlr_stage_a.py --session-dir <completed session copy> --out <new file outside it> \
    --completion-evidence "<how the copy is known to be complete>" [--metadata meta.json]
python sidecar/qorcap_rlr_stage_a.py --inputs <copy of inputs.jsonl> --out <new file> --completion-evidence "..."
```

Exit codes: 0 analysed, 3 input rejected (an output with the reason is written, e.g. `input_missing`),
2 refused (nothing written: output collision, output inside the source folder, existing output).
The inputs header has no game field. Game comes from supplied metadata or `receipt.json match.game`, else
`UNKNOWN`. Practice/real and dispositions come only from supplied metadata, never from folder names or
`verify.txt`. Optional metadata keys: `session_id`, `game`, `mode`, `receipt_disposition`,
`verify_disposition`, `late_disposition`, `provenance`, `external_check_refs`.

There is no lower "continuity unknown" evidence tier: a session with no hb rows reports candidate counts
and exclusions with null medians.

## Source pin

- Pinned sources: `src/inputs.rs` (`record()`, status, `hb`, chunk) and `src/hid.rs` (`R2 = 1 << 7`) at `061fa1b8fa80e96a368305767edff436e959d87e` (v0.3.40).
- This checkout's current `src/inputs.rs` was read before use. That file also changed after the pin (commit `2ed127a`, #133). Row types and the field names below are the ones that file emits. Anything the health rule does not define is not interpreted.

Supported input schema: `qorcap-inputs/v0`.

| Row | Fields this tool uses |
|---|---|
| `header` | `schema` must be `qorcap-inputs/v0` or the file is rejected. Optional `transport` and version strings are echoed or marked UNKNOWN. |
| `in` | `t_us` (unsigned host-monotonic microseconds) and `b` (integer bitmask). Digital R2 is bit 128. Analog `r2` is not an edge and is not force. `c` and `hw` are not loss oracles. |
| `status` | A status row ends the current pairing stretch. It does not carry an epoch. `detail` and `pad_id` are never copied into the report. |
| `hb` | `t_us`, `hb_seq`, `epoch`, `rx_delta`, `last_rx_t_us`, `max_rx_gap_us`, `faults`, `short_reads`, `parse_faults`, `stale_over_50ms`. Other hb fields (`seq0`, `seq1`, `rx_bind`, `neutral`, `hw*`, and the rest) are not interpreted. |
| `chunk` | Declared `seq`, `seg`, `n`, `sha256`, `prev`, `head`. Structure is checked (contiguous `seq`, `n` equals records in the chunk, `sha256` is 64 lowercase hex). The digest is not recomputed. |
| `imu`, `haptic`, other types | Counted and skipped. They are not edges. |

Epoch comes only from `hb.epoch`. A later heartbeat does not assign an epoch backward. Row gaps and `c` jumps do not mean lost reports.

## Procedure `rlr-stage-a-procedure/v0`

Config hash is SHA-256 of the canonical config object and is written on every report (`tool.config_sha256`). The same value is on each moment.

1. Record input SHA-256, length, schema, and the supplied completion string. With no `--completion-evidence`, candidate counts are kept and qualified aggregates are withheld (`abstained`).
2. Stream lines. File line indexes are 1-based. Chunk-local record indexes are 0-based and count every committed record except the `chunk` line itself. Timestamps are not sorted. Raw integer microseconds are kept.
3. A rise is bit 128 going 0→1. A fall is 1→0. Dwell is fall minus rise. Interval is the next rise minus the rise. The first row in a stretch that is already held is left-censored, not a press. A held bit at end of file is right-censored. A fall with no rise in the stretch is not a dwell. Status rows, invalid `in` rows, a backward `t_us`, a duplicate same-time row, and a same-time R2 contradiction end the stretch. No pair crosses that boundary. Nothing missing is treated as zero.
4. Repeated held rows are not extra presses. Analog-only changes are not edges.

Quantiles: sort ascending, linear interpolation at index `(n-1)*p` for p = 1/4, 1/2, 3/4. `n=0` yields null and status `UNMEASURED`, never the number 0. `n=1` uses that single value for Q1, median, and Q3 (IQR 0) and is labeled `single_value`. Milliseconds are microseconds / 1000 with no extra rounding. Epochs are not merged into one series. A session-level quantile is emitted only when every qualified value for that kind sits in one epoch; otherwise the session aggregate is `not_pooled` and `per_epoch` keeps each denominator.

## Health rule `rlr-health/v0-proposed`

PROPOSED review default. Not an approved counting policy. Not a physical timing bound. Not tuned on outcomes.

A pair is health-qualified only when the hb rows from the first hb with `t_us >= start` through the first hb with `t_us >= end` share one integer epoch, have `hb_seq` increasing by 1, strictly increasing `t_us`, `rx_delta > 0`, `faults`, `short_reads`, `parse_faults`, and `stale_over_50ms` all 0, `max_rx_gap_us <= 50000`, and a non-null `last_rx_t_us`. If the span starts at or before the first hb, that hb must have `hb_seq` 0 or the span is left-uncovered. Time after the last hb is not covered and is not extrapolated. No hb rows at all: continuity `UNKNOWN`, candidate counts kept, qualified medians null.

Host hb fields are not device timing.

## Output schema `qorcap-rlr-stage-a/v0`

External derived JSON. It is not a receipt and it does not change `receipt.v0` or `contracts/`.

- Tool name, version, procedure, config (and its SHA-256), source pin, analysis id.
- Optional lineage: `supersedes`, `supersede_reason`, `depends_on`. A revision is a new file. Earlier outputs are not rewritten.
- Input basename, SHA-256, and length. No directory path.
- `created_at_wall` is a wall-clock label and is not used in the math.
- Metadata states are `declared`, `unknown`, or `mismatch`. `checked_by_this_tool` is false. The word VERIFIED in a supplied file stays declared. It does not make derived events or counting eligibility.
- `evidence_tier` is `private_descriptive`. `formal_counting` is `not_eligible` or `not_evaluated`. Gold, referee submit, and upload are false. Practice stays off those flags.
- Match id `201029` (session id `201029` or ending in `-201029`) stays ineligible. A supplied VOID disposition is echoed, not upgraded.
- Clock domain: actor `host`, units `us`, origin host-monotonic `t_us`. No wall, video, or device mapping.
- Denominators for rises, falls, complete observed pairs, qualified pairs, exclusions, interruptions, and censoring.
- Per-epoch coverage via qualified or excluded reasons.
- Each moment: kind, session id, input SHA-256, endpoint line, chunk declared hash and seq, chunk-local index, seg, epoch, raw `t_us`, `duration_us`, health status/reason, trace flag, tool version, config hash. Endpoint chunks may differ. Unsealed-tail endpoints are not trace-qualified.
- `chunk_binding.state` is `declared_only`. `binding_verified` is false.

## Read-only invariants

No edits under `src/`, `contracts/`, `receipt.v0`, HUD readers, the referee or checker, upload, controller output, or match launchers. No network, device, video, or subprocess import. Practice is never gold, referee submit, or upload. REAL MATCH baseline files are not modified.

## Unsupported or rejected

- Schema other than `qorcap-inputs/v0`, missing header, empty file.
- Truncated or malformed JSONL (no repair, no sort).
- Non-integer `t_us` or `b`.
- Chunk `seq` / `n` / hash-shape failures abstain from qualified aggregates. The recorded hash is still not called checked.
- Health fields absent, non-increasing hb time, `hb_seq` gaps, epoch changes, zero receive, fault/short/parse/stale flags, `max_rx_gap_us` above 50000, uncovered first span, uncovered tail.
- Live or incomplete capture is not inferred. Without an explicit completion string, qualified aggregates are withheld.

## UNMEASURED

- Novelty (UNASSESSED, not a measurement).
- Performance on real recordings.
- Physical bit-edge latency and firmware threshold.
- `hw` units.
- Device-clock, wall-clock, and video mapping.
- Agreement with an independent annotator on real recordings.
- Chunk digest binding (declared only; not recomputed).
- Counting eligibility as policy (not evaluated into a counted result).
