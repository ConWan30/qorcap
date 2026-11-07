# Pad-link continuity

Each `hb` row in `inputs.jsonl` is already in the input chunk hash chain.
The row now also commits, for that second:

- `rx_delta`, how many reports the host received
- `seq0` / `seq1` / `seq_steps` / `seq_breaks`, the report counter copied from the HID input report
- `hw0` / `hw1` / `hw_advance` / `hw_stalls`, the sensor timestamp copied from that same report
- `rx_bind`, SHA-256 of those counter and timestamp bytes in receive order

Axes stay raw 0..255, center 128, no deadzone. The counter-step rule is PROVISIONAL.

A quiet stretch stays unknown unless every covering heartbeat passes the interval gates already on the row: one epoch, no faults, no short reads, no parse faults, no stale gap, `max_rx_gap_us` at most 50 ms, and a fresh first and last report. The span also has to be neutral (buttons up, sticks at 128) and carry one trusted anchor. `rx_bind` is a local digest of bytes this process read. It is not that anchor.

A report count can add support only after those gates. A missing `hb_seq` or a zero `rx_delta` stays unknown. Unknown is not idle. The capture does not mint a trusted anchor, so a live gap stays unknown.

`pad_link.v0.json` is a reading of those rows. Its SHA-256 is on the stop checkpoint. Editing an `hb` row breaks the chunk hash.
