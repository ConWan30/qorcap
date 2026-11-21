# PWEC fixtures (info only)

`match-20261119-191816.*` is a reduced copy of one practice Call of Duty match's controller log and
frame clock (box copy, heldout-4 spare; already used in earlier dev work, so it is not a holdout).

- `*.inputs.jsonl.gz`: `type=in` rows only where the R2 or L2 held state (> 60) changes, with
  `t_us`, `r2`, `l2`; every `type=hb` row with `t_us` and the six health fields the hid_doubt rule
  reads; one final `status` row carrying the log's last `t_us`. The pad id, sticks, buttons, IMU
  and haptics are dropped.
- `*.frame_clock.delta.txt.gz`: every `video.mp4` frame row as `d_frame d_t_mono_us d_pts_90k`
  (deltas from the previous row, starting from 0 0 0). The test rebuilds `frame_clock.jsonl`.
- `*.p0-golden.json`: per-window expected values from the plan's offline count
  (`count_windows.py` sha256 f4d2ae7b..., `COUNTS-pwec.json` sha256 b204719b...). The `has_prior_cut_frame`
  field is box-only and not compared.

The reduction keeps every input the window builder reads, so the windows equal the full log's
windows. The full-log sha256 values are in the golden file. Gzip files are written with mtime 0.
