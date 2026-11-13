# Call of Duty owner-tag samples (practice lane, info-only)

`read_owner_row` finds Qortrola30's end-scoreboard row by template match on the gamertag letters
(value > 150, 16x66 on 1280x720). The gate `_OWNER_MIN` is **0.80** and is unchanged.

Since this change there are two samples of the same letters at the same size, matched at that same
gate; the higher score is kept:

| sample | source | where |
|---|---|---|
| `owner_mask` (npz) | match-20261105-173922, `end_173922_019` | unchanged |
| `owner_tags[0]` (text supplement `packs/cod_owner_row_digits.json`) | match-20261113-133025 end board, row 4 | thinner strokes |

match-20261113-133025 had its row on screen (K13 A3 D22) and the shipped sample scored 0.758-0.764 on all
13 board frames, so the row was never read.

## Split (by match)

- **Tuning** (sample cut, choice made): 20261105-173922, 20261113-133025, 20261101-092413.
- **Held-out** (scored once after the choice was frozen): 20261104-162225, 20261104-202724, 20261107-073204,
  20261111-183311, 20261111-194037 (end boards), 20261111-081216, 20261111-165658, 20261113-131754 (no end board in
  the available clip: gameplay, menus).

Frames are 2 per second from the recordings, or the staged still frames. Owner row = the highlighted row; each
staged board was looked at and the highlighted row is Qortrola30 in every match. Impostor rows = the 11 other row
slots on the same board frame (empty slots included). Non-board frames = every frame with no KILLS header.

## Held-out counts, gate 0.80

| set | board frames | owner rows accepted | false rejects | impostor rows | impostor accepts | non-board frames | non-board accepts |
|---|---|---|---|---|---|---|---|
| shipped sample only | 50 | 43 | 7 | 550 | 0 | 536 | 0 |
| both samples | 50 | 49 | 1 | 550 | 0 | 536 | 0 |

The 7 shipped-only rejects are 6 frames of 20261111-183311 where the board sits 7 px lower (0.770-0.783) and
one fade-in frame of 20261107-073204 (0.638). With both samples the 183311 frames score 0.897-0.968; the
fade-in frame stays rejected.

Held-out score ranges, both samples: owner rows 0.638-0.968 (median about 0.90 per match); impostor rows max 0.366;
non-board frames max 0.517.

**Rates:** five held-out matches with end boards, and frames within one match are strongly correlated. A
per-match false-accept or false-reject rate is **UNKNOWN** from this data. Frame counts above are raw.

## Not covered

- The live reader reads the 4 fps JPEG snapshot; these frames are decoded from the H.264 recording. Re-encoded at
  JPEG quality 90 and 40, the held-out tag decisions were the same (49/50 pass, 0 non-board passes).
- A 720p frame scaled to 1080p mostly fails the tag with either sample. No real 1080p practice board was available.
- Not changed here: the ASSISTS column header is missed on the TDM boards (20261113-133025, 20261104-162225), and
  the KILLS header margin is thin (0.714-0.96 against 0.70; at JPEG 90 it drops below on 20261104-162225).
  Deaths are not read on 20261111-183311.
