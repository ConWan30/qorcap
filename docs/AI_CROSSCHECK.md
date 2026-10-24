# Confirm-only cross-check (not wired)

Held until Con says go. This is the design. Nothing in the repo calls TypeSafe or OpenRouter for it, and `QORCAP_GLIMPSE_TD_VETO` stays off.

A locked checkpoint can be shown to TypeSafe Jev as a confirm-only second opinion. Jev sees the crop and the lock the reader already made. It answers yes, no, or no_scorebug.

Doubt — anything other than a clean yes — may flag that lock or lower its status. It must not raise a status, fill a blank, create a lock, or decide a final.

The note lives in a sidecar, `ai_crosscheck.jsonl`, tagged `ADVISORY_AI`. It is not a receipt field, not a checkpoint, and not an input to the checkpoint hash. `receipt.v0` stays as it is.

On the 170123 public highlight set, Jev's argmax-yes confirmed every correct reader proposal and doubted the synthetic wrong proposals. It also confirmed some trap clocks (a JPEG 1:03 read as 7:03, a 1:02 read as 7:08). A yes from Jev cannot mint a score or override an abstain. The reader leaves those traps blank. Jev does not get to fill them.
