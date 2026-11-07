# Opening seal, dual-bind

The Call of Duty opening echo is TRIANGLE, SQUARE, CIRCLE, CROSS, then OPTIONS
held at least 1.5 s. That hold floor and the 20 s opening window are
PROVISIONAL. The window is measured from capture clock `t_us` 0.

The watch lives in the HID receive loop. When OPTIONS releases, the window is
hashed and written to `session_seal.json` and `pad_session.v0.json` beside
`inputs.jsonl`. Both files carry the same `seal_hash`. The next checkpoint
copies `qorcap.session_seal.hash`. The opening gate passes only when the
status is `locked` and `opening_gate` is true and the three hashes match.

A late complete pattern is `failed` with reason `late` and a real hash. The
opening gate stays false. A solo OPTIONS hold, a wrong order, a short hold, or
no pattern is `failed` with a null hash. A hash is not invented. Still holding
OPTIONS at stop does not invent a release.

The console line is the only confirmation. Nothing is written to the controller.

If the pause-menu exit is missing, or measured lag is unavailable, the seal
exclusion window stays unresolved. It does not shrink, and it does not release
later events into counts.
