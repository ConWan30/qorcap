# Clock rules, the running-clock re-anchor, and the lock vote

The standard-bar reader keeps a clock, a score, or a quarter only when the step is legal. Anything else is blank. These parameters are constructor arguments and are part of `reader_model_id`.

## Clock

Inside a quarter the clock holds or counts down. It does not go up. A higher quarter may set a new clock anywhere from 0:00 to 15:00. That cap is the regulation maximum. The configured quarter length is not known, so the reader does not guess a 5:00 or 6:00 reset. A clock above 15:00 is `clock_above_max` and is blank. A quarter that goes backwards is a cut: the baseline starts over. A negative clock is `clock_negative`.

An increase inside a quarter is `clock_increase`. It is blank and the baseline does not move. One held frame does not re-anchor. The same higher second held for the stoppage bar (4 reads spanning 2 s) blanks both, and the stoppage gate does not adopt it. Once two lines contradict, both stay blocked for the rest of the quarter, across the same slope band the running gate accepts (about 0.7x–1.3x). A higher clock that is not one of those lines returns only through the running-clock gate or a quarter change.

A drop larger than the wall time since the last accepted clock is not kept on that frame. Two gates can accept a later read:

| Gate | Parameters | What it accepts |
|---|---|---|
| Stoppage | `reanchor_reads` 4, `reanchor_span_ms` 2000 | The same lower second, held. While suspended, an equal second is also accepted. |
| Running clock | `run_reads` 4, `run_span_ms` 3000, `run_min_distinct` 3, `run_tol_s` 1 | A countdown at about 1 game-second per wall-second |

The stoppage gate cannot open on a clock that is actually running: the displayed second changes every wall second, so four reads of one value never span two seconds. That is why a Madden accelerated runoff left the clock blank until a two-second stop (FU1).

The running-clock gate is the one place a clock that #76 blanked may be kept. The value kept is the read itself. It has to pass every sample of the run, not only the endpoints.

Why these numbers:

* **4 reads.** One to three gated reads cannot finish the run. Same count as the stoppage gate.
* **3 seconds.** Over two seconds a perfect 1:1 clock drops one or two displayed seconds, and a doubled clock can still sit inside a ±1 second bin. Over three seconds a doubled clock is about three seconds off that bin.
* **3 distinct seconds.** A hold belongs to the stoppage gate. Three different displayed seconds is what three seconds of a 1:1 clock shows.
* **Tolerance of 1 second, at every sample.** Rates of about 0.7x–1.3x are accepted over the 3 s window (the values read are still the true display) and 2x is rejected. A displayed second is a one-second bin. A perfect 1:1 line `s0 - wall_seconds` stays within that bin at every sample. A jump in the middle fails even when the first and last samples would match.

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

A sample closer than 200 ms is not new evidence and does not reset the vote. One illegal decrease blanks that frame and does not flip `allows_final_fallback`. A pending new score inside the 2 s gap, including two frames that have not yet persisted, does withhold it. Blank frames do not age that pending read out; a later non-blank read can. The same disagreement, persisted across the vote, does. A gap longer than 2 s starts the count over. It does not clear a disagreement that already persisted. Disagreeing reads are kept across that window. A read of the locked score does not reset them when one of them is a new score rather than a decrease. Three consecutive reads of the locked score, with the same interval, span, and 2 s gap, clear a disagreement that has persisted, once those disagreeing reads have left the gap. While they are still inside it, the re-shows do not restore the stale lock. One matching read does not clear it, and a gap in that clear vote starts it over. An older lower score between reads of the lock is dismissed by those reads, so a forged decrease interleaved with the true lock does not abstain it.

A clock that has been accepted, and is then contradicted by another sustained 1:1 line at this same bar (higher or lower), blanks both values. Both of those running lines stay blocked until a quarter change, or until a line that is neither of them completes a gate. The block is the running gate's slope band (about 0.7x–1.3x from each line's origin), so a contradicted misread at a rate that gate accepts stays blocked too. The same higher second held for the stoppage bar blanks both as well. That held second stays blocked, and so does its later countdown, across that same slope band measured from the last read of the held second, for the rest of the quarter. Exact 1:1 is not the bound. While suspended, a stoppage re-anchor cannot be above the last clock published in that quarter, and an equal second is accepted. It also cannot adopt a second either contradicted line could already have shown, including after a projection has moved past that second. The low edge of that check is the top of the slope band, not 1.0x. After a held contradiction, both lines stay blocked on their trajectories. A hold that starts on the frame after the running gate accepted is that same block: the held countdown stays blank, and a later stoppage does not bring the accepted clock back. A read above 15:00 while suspended still moves a held line's anchor before the read is rejected. A higher clock that is neither blocked line returns only through the running-clock gate or a quarter change. A quarter change clears both lines. A read is emitted only when a re-anchor completes. A read with no quarter is not emitted and does not join the re-anchor. A lone runoff is still adopted: that is the running-clock gate.

A low misread accepted on a stoppage, then contradicted by a higher second held for the stoppage bar, stays blank through that higher second's whole countdown, not only while the second is held. The true clock is that countdown. The stoppage gate does not adopt the rejected misread again on a later stoppage. A new quarter accepts the clock still on screen. A 15:00 held against that clock is a contradicted held line, so the 15:00 countdown does not come back through the running gate.

An implausible jump still needs 9 samples over 2 s before the agreement window will lock it. A forged decrease can re-anchor inside the grammar and still leave the final abstaining, because the re-anchor stays unconfirmed until that window locks the same pair.

## Inferred quarter

When the quarter glyph is missing and this frame's clock was accepted as a continuation of the last observed quarter (no reset, no cut, no increase), the reader carries that quarter in the frame `detail`:

* `quarter_inferred`: true
* `inferred_quarter`: the last observed quarter

`quarter` stays null. The carried value is not posted on the timeline, not offered to the agreement window, and not passed to `detect_final`. The note is a line in `reanchor_notes.jsonl` (the existing sidecar, not a receipt field and not a checkpoint-hash input). `receipt.v0` is unchanged. A quarter that goes backwards is still a cut.
