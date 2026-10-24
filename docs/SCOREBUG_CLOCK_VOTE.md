# Clock rules, the running-clock re-anchor, and the lock vote

The standard-bar reader keeps a clock, a score, or a quarter only when the step is legal. Anything else is blank. These parameters are constructor arguments and are part of `reader_model_id`.

## Clock

Inside a quarter the clock holds or counts down. It does not go up. A higher quarter may set a new clock anywhere from 0:00 to 15:00. That cap is the regulation maximum. The configured quarter length is not known, so the reader does not guess a 5:00 or 6:00 reset. A clock above 15:00 is `clock_above_max` and is blank. A quarter that goes backwards is a cut: the baseline starts over. A negative clock is `clock_negative`.

An increase inside a quarter is `clock_increase`. It is blank, the baseline does not move, and holding the higher second does not re-anchor.

A drop larger than the wall time since the last accepted clock is not kept on that frame. Two gates can accept a later read:

| Gate | Parameters | What it accepts |
|---|---|---|
| Stoppage | `reanchor_reads` 4, `reanchor_span_ms` 2000 | The same lower second, held |
| Running clock | `run_reads` 4, `run_span_ms` 3000, `run_min_distinct` 3, `run_tol_s` 1 | A countdown at about 1 game-second per wall-second |

The stoppage gate cannot open on a clock that is actually running: the displayed second changes every wall second, so four reads of one value never span two seconds. That is why a Madden accelerated runoff left the clock blank until a two-second stop (FU1).

The running-clock gate is the one place a clock that #76 blanked may be kept. The value kept is the read itself. It has to pass every sample of the run, not only the endpoints.

Why these numbers:

* **4 reads.** One to three gated reads cannot finish the run. Same count as the stoppage gate.
* **3 seconds.** Over two seconds a perfect 1:1 clock drops one or two displayed seconds, and a doubled clock can still sit inside a ±1 second bin. Over three seconds a doubled clock is about three seconds off that bin.
* **3 distinct seconds.** A hold belongs to the stoppage gate. Three different displayed seconds is what three seconds of a 1:1 clock shows.
* **Tolerance of 1 second, at every sample.** A displayed second is a one-second bin. A perfect 1:1 line `s0 - wall_seconds` stays within that bin. The tolerance is the bin, not slack for a faster rate. A jump in the middle fails even when the first and last samples would match.

## Vote

A score, a quarter, and a clock lock through the existing agreement window. The numbers are now explicit, and they are the disagreement vote as well:

| Parameter | Value | Agreement window field |
|---|---|---|
| `vote_reads` | 3 | `need` |
| `vote_interval_ms` | 200 | `min_interval_ns` |
| `vote_span_ms` | 400 | `min_span_ns` |
| `vote_gap_ms` | 2000 | `max_gap_ns` |
| `suspicious_reads` | 9 | `need_suspicious` |
| `suspicious_span_ms` | 2000 | `min_span_suspicious_ns` |
| `jump_memory_ms` | 30000 | `jump_memory_ns` |

A sample closer than 200 ms is not new evidence and does not reset the vote. One illegal frame blanks that frame and does not flip `allows_final_fallback`. The same disagreement, persisted across the vote, does. A gap longer than 2 s starts the count over. It does not clear a disagreement that already persisted. A legal read of a score the grammar accepts does.

An implausible jump still needs 9 samples over 2 s before the agreement window will lock it. A forged decrease can re-anchor inside the grammar and still leave the final abstaining, because the re-anchor stays unconfirmed until that window locks the same pair.

## Inferred quarter

When the quarter glyph is missing and this frame's clock was accepted as a continuation of the last observed quarter (no reset, no cut, no increase), the reader carries that quarter in the frame `detail`:

* `quarter_inferred`: true
* `inferred_quarter`: the last observed quarter

`quarter` stays null. The carried value is not posted on the timeline, not offered to the agreement window, and not passed to `detect_final`. The note is a line in `reanchor_notes.jsonl` (the existing sidecar, not a receipt field and not a checkpoint-hash input). `receipt.v0` is unchanged. A quarter that goes backwards is still a cut.
