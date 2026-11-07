# Opening seal, dual-bind

The Call of Duty opening echo is TRIANGLE, SQUARE, CIRCLE, CROSS, then OPTIONS
held at least 1.5 s. That hold floor and the 20 s opening window are
PROVISIONAL. The window is measured from capture clock `t_us` 0.

The watch lives in the HID receive loop. The hash is SHA-256 of the session, the epoch, and every timed echo report from the pattern through the OPTIONS release. The preimage stays on `session_seal.json`. Selected edges alone are not the hash. A report with no bytes does not invent a hash.

A reconnect or a read fault fails the pending attempt. A later OPTIONS release on the new epoch does not finish a hold that was not observed. A retryable failure is not a checkpoint binding. A finalized revision is acknowledged only after the checkpoint line is durable.

Both files carry the same `seal_hash`. The next checkpoint copies `qorcap.session_seal`. The opening gate also needs an anchor acknowledgment that is not the seal hash, on the session file, the pad file, and the checkpoint. A local lock does not mint that anchor, so dual-bind does not pass on the hash alone.

A late complete pattern is `failed` with reason `late` and a real hash. The opening gate stays false. A solo OPTIONS hold, a wrong order, a short hold, or no pattern is `failed` with a null hash. A hash is not invented. Still holding OPTIONS at stop does not invent a release.

The console line is the only confirmation. Nothing is written to the controller.

If the pause-menu exit is missing, or measured lag is unavailable, the seal
exclusion window stays unresolved. It does not shrink, and it does not release
later events into counts.
