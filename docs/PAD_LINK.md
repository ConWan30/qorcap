# Pad-link continuity

Each `hb` row in `inputs.jsonl` is already in the input chunk hash chain.
The row now also commits, for that second:

- `rx_delta`, how many reports the host received
- `seq0` / `seq1` / `seq_steps` / `seq_breaks`, the report counter copied from the HID input report
- `hw0` / `hw1` / `hw_advance` / `hw_stalls`, the sensor timestamp copied from that same report
- `rx_bind`, SHA-256 of those counter and timestamp bytes in receive order

Axes stay raw 0..255, center 128, no deadzone. The counter-step rule is PROVISIONAL.

A quiet stretch is classified from those sealed rows:

- a heartbeat that keeps ticking through the stretch can support idle
- a zero `rx_delta`, or a missing `hb_seq`, is a drop
- no covering heartbeat, a missing `rx_bind`, a sequence break, or a stalled sensor timestamp stays unknown

Unknown is not idle. An older heartbeat without `rx_bind` stays unknown.
`pad_link.v0.json` is a reading of those rows. Its SHA-256 is on the stop checkpoint. Editing an `hb` row breaks the chunk hash.
