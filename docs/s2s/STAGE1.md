# Sight-to-shot Stage S1: `qorcap s2s-counted`

Offline and read-only. It reads one bundle's `inputs.jsonl` and writes one JSON report to `--out`, which must be **outside** the bundle folder (seal integrity; the tool refuses a path inside it). No video, frame clock, screen reader or detector is read. Plan: sight-to-shot PLAN-r9 (sha256 `b3e3c603…8caa`) with amendment r10 (PLAN-r10, sha256 `1104de94…f684`).

```
qorcap s2s-counted <bundle-or-inputs.jsonl> --out <report.json> --session-id <id> \
    [--config docs/s2s/threshold_A.standby.json] [--seal-status rehashed_match|locked_not_rehashed|failed|missing] \
    [--disposition ok|UNKNOWN|VOID|FAIL] [--in-calibration-set] [--holdout-reused]
```

**What COUNTED means here.** Host-observed USB rows that passed the preregistered gates on this capture setup. It is a private descriptive tier only: not a rating, not a measure of the person, not checked by a referee, never gold. It does not show who held the controller or that the input came from a person. The physical thumb onset is UNMEASURED.

## Rule (inputs.jsonl rows only)
- **S4 anchor.** R2 edge = an R2 analog jump of more than `edge_delta` (60) between consecutive `in` rows, exactly `trigger_edges()` in `sidecar/qorcap_cod.py`. A slow squeeze makes no edge. The R2 digital bit is recorded as a diagnostic (`t_us_bit_diagnostic`) and never makes an edge. Edges `<= burst_gap_us` apart merge into one burst; the burst's first edge is the anchor.
- **Window.** S1-blind `[R2 - W, R2]`. Nothing screen-side is read, so no S1 value can move the selection (fixture: `s1_perturbation_invariance`).
- **S2, onset rule (ii).** Deflection `d = max(|rx-128|, |ry-128|)` clipped 127. An onset is the first `in` row with `d >= onset_d` after a span of at least `quiet_us` with `d <= release_d` throughout, inside the window (hysteresis: after an onset the stick must fall to `<= release_d` before another). Every qualifying onset is kept in `s2_candidates[]`; more than one sets `multi_onset`; the first is selected.
- **Throttle.** `in` rows are change-only and analog-only changes are logged at most every 4 ms (`ANALOG_MIN_GAP_US`, `src/inputs.rs:56`, applied at `:484-485`; re-read at main `4888a63`). S2 is the logged row time; `prev_row_gap_us` and `t_us_bracket = [previous in row, onset row]` carry the uncertainty.
- **Outcome per anchor (exactly one).** `onset`, `no_onset`, `left_censored_stick` (stick deflected at window start and no qualifying onset), `onset_reused` (the selected onset already went to an earlier anchor; not counted twice), or `masked:<reason>`.
- **Span gates.** Span = `[baseline start, R2]` for an onset, `[R2 - W, R2]` otherwise. Masks: `transport_not_usb` (header or any row in the span not `usb-hid`), `transport_unknown` (no header transport), `controller_status_change_in_window`, `no_hb`, `epoch_change_in_window`, `hb_gap_in_window` (non-contiguous `hb_seq`, faults, short reads, parse faults, `stale_over_50ms`, `max_rx_gap_us > 50,000`), `report_seq_break`, `window_before_first_row`. hb rows without `seq_breaks` (older builds) pass with flag `seq_breaks_unrecorded`.

## Tiers and the headline
- `COUNTED` only when the config `status` is `frozen`, the caller states `--seal-status rehashed_match`, and the session is not excluded, calibration or held-out-reused.
- `INFO_ONLY` otherwise, with `info_only_reasons` (`stick_onset_threshold_provisional`, `seal_not_rehashed`, `seal_failed`, `seal_missing`). The headline is `NOT_COUNTED`; values appear only under `descriptive`.
- `DESCRIPTIVE_ONLY_HOLDOUT_REUSED` (`--holdout-reused`, PLAN-r10 R10-3): never COUNTED, excluded from the headline and every COUNTED table.
- `--in-calibration-set`: no per-burst rows and no timing values (R1); only the threshold-free gate-passing burst count (M4).
- Excluded sessions (config `excluded_sessions`: 073204 per Con, 201029 and 153536 VOID, 112342 FAIL) and `--disposition VOID|FAIL`: `status: excluded`, nothing else.
- **Headline:** label "R2 press bursts". The S2 -> S4 distribution (`n`, `median_us`, `p25_us`, `p75_us`, `iqr_us`, `min_us`, `max_us`) over every gate-passing burst with outcome `onset`, always next to the full `outcome_table`. Fewer than `min_bursts_for_headline` (30) -> `status: UNMEASURED`, no value. No screen-side filter (post-death, killcam, HUD) touches it.

## Report fields
`schema` `qorcap-s2s-counted/v0`; `session{session_id, inputs_sha256, seal_status, disposition, in_calibration_set, holdout_reused}`; `threshold{threshold_id, provenance, params, code_pins}`; `counts`; `tier`; `info_only_reasons`; `outcome_table{outcomes, multi_onset, gate_passing_r2_bursts, r2_bursts}`; `headline`; `descriptive` (non-COUNTED only); `anchors[]` with `anchor_id = sha256([session_id, epoch, r2_edge_t_us, edge_rule_id])`, `epoch`, `tier`, `outcome`, `flags`, `s4_r2`, `burst`, `window_us`, `s2_candidates[]{onset_id = sha256([session_id, epoch, onset_t_us, threshold_id]), t_us, axis, d, prev_row_gap_us, t_us_bracket, baseline_start_us, quiet_us}`, `s2_selected`, `s2_s4_us`, `hb_span`, `threshold_id`, `threshold_provenance` (on every row); `gold: false`, `referee_submit: false`, `upload: false`. The report is not in the bundle, the hash chain, `receipt.v0` or `contracts/`.

## threshold_id
`sha256(JCS({schema, source, params, tuning_sessions[{session_id, seal_status, inputs_sha256}], heldout_sessions, code_pins{main_sha, refs, drift}}))`. Freeze status and the exclude list are not part of it. `docs/s2s/threshold_A.standby.json` is the standby (A) rule (`status: standby_unfrozen`); a test keeps it identical to `Config::standby_a()`, and another fails if any pinned code line drifts. Freezing a real threshold, the ledger entries, seal re-hash checks and any run on real sessions are outside this tool and still gated (PLAN R7-3).
