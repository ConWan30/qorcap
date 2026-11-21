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

Held-out score ranges, both samples: owner rows 0.638-0.968 (per-match medians 0.89-0.94); impostor rows max 0.366;
non-board frames max 0.517.

### Per match (board frames)

| held-out match | board frames | shipped only accepted | both samples accepted | other-row slots | non-board frames |
|---|---|---|---|---|---|
| 20261104-162225 | 5 | 5 | 5 | 55 | 5 |
| 20261104-202724 | 5 | 5 | 5 | 55 | 5 |
| 20261107-073204 (excluded session, see below) | 14 | 13 | 13 | 154 | 1 |
| 20261111-183311 | 13 | 7 | 13 | 143 | 67 |
| 20261111-194037 | 13 | 13 | 13 | 143 | 67 |
| 20261111-081216 (no board) | 0 | - | - | 0 | 160 |
| 20261111-165658 (no board) | 0 | - | - | 0 | 80 |
| 20261113-131754 (no board) | 0 | - | - | 0 | 151 |

No other-row slot and no non-board frame reached 0.80 in any match.

### With and without 20261107-073204

Same measurements as above, only regrouped. No frame was re-scored.

| set | held-out | board frames | accepted | false rejects | other-row slots (accepts) | non-board frames (accepts) |
|---|---|---|---|---|---|---|
| shipped only | with 073204 | 50 | 43 | 7 | 550 (0) | 536 (0) |
| shipped only | without 073204 | 36 | 30 | 6 | 396 (0) | 535 (0) |
| both samples | with 073204 | 50 | 49 | 1 | 550 (0) | 536 (0) |
| both samples | without 073204 | 36 | 36 | 0 | 396 (0) | 535 (0) |

### 20261107-073204 standing exclusion

- 20261107-073204 is a v0.3.40 practice session. A standing rule from Con keeps it **EXCLUDED**. For the S2S work it
  is never a tuning, Counted, or held-out session, and `docs/s2s/threshold_A.standby.json` still lists it under
  `excluded_sessions`. This change does not touch that file.
- It is **not a template source.** Neither tag sample was cut from it, and no threshold here was set on it.
- It appears above only as an extra held-out illustration for this practice-lane tag reader. Its one fade-in frame
  (0.638) is the **only false reject** with both samples. Without it the held-out set has 0 false rejects on 36 board
  frames.
- The end-to-end read on this session (K9 A2 D21) is an illustration only, under the same exclusion.

**Rates:** five held-out matches with end boards, and frames within one match are strongly correlated. A
per-match false-accept or false-reject rate is **UNKNOWN** from this data. Frame counts above are raw.

## Not covered

- The live reader reads the 4 fps JPEG snapshot; these frames are decoded from the H.264 recording. Re-encoded at
  JPEG quality 90 and 40, the held-out tag decisions were the same (49/50 pass, 0 non-board passes).
- A 720p frame scaled to 1080p mostly fails the tag with either sample. No real 1080p practice board was available.
- Not changed here: the ASSISTS column header is missed on the TDM boards (20261113-133025, 20261104-162225), and
  the KILLS header margin is thin (0.714-0.96 against 0.70; at JPEG 90 it drops below on 20261104-162225).
  Deaths are not read on 20261111-183311.

## Row highlight as a cue (evaluated, not shipped)

The console fills the local player's row with a dark yellow band. The fill only marks whoever is signed in on
the console. It does not bind that row to Qortrola30, so the tag stays the identity check.

**Cue measured.** Share of pixels with hue 15-40, saturation >= 80 and value >= 25 across the stat cells of each
row slot. The slot grid is shifted by the KILLS header offset.

**Thresholds.** Set on the tuning matches only:
- The highlight locates a row when its fill is >= 0.65 and beats every other slot by >= 0.30.
- Highlight plus tag: the tag must reach 0.70 on the highlighted row. With no highlight, the tag-only path at 0.80
  applies.
- On tuning, owner fill was 0.726-0.960. Other slots reached 0.555 and non-board frames 0.435.

**Held-out, same matches and frames as above:**

| mode | board frames | correct row | false reject | wrong row | other rows tag >= gate | non-board frames | non-board accepts |
|---|---|---|---|---|---|---|---|
| highlight only (locator, no identity) | 50 | 45 | 5 | 0 | not an identity check | 536 | 1 (located) |
| tag only, shipped sample | 50 | 43 | 7 | 0 | 0 / 550 at 0.80 | 536 | 0 |
| tag only, both samples (this change) | 50 | 49 | 1 | 0 | 0 / 550 at 0.80 | 536 | 0 |
| highlight + tag, shipped sample | 50 | 49 | 1 | 0 | 0 / 550 at 0.70 | 536 | 0 |
| highlight + tag, both samples | 50 | 49 | 1 | 0 | 0 / 550 at 0.70 | 536 | 0 |

**Where highlight-only fails.**
- It misses 4 frames of 20261111-183311, where the neighbouring rows are also yellow and the margin falls under 0.30.
- It misses the 20261107-073204 fade-in frame.
- It locates a row on one 20261111-194037 frame, where the board is fading out and no KILLS header is found.
- It cannot reject another local player. Highlight alone must never post an owner read.

**Decision.** Highlight plus tag gives the same held-out count as tag-only with both samples (49/50) and needs a
lower tag gate on the highlighted row, so it is not added here. It is a candidate if a future render falls under
0.80 on both samples.

**Does the highlight explain the thin tag?** UNCONFIRMED.
- Every owner-row tag in this data is on a highlighted row. No non-highlighted Qortrola30 row exists in the data,
  and the personal card uses a different text size.
- On 20261113-133025 the fill is the darkest and most saturated (value about 38, saturation about 221). The tag has
  the thinnest strokes (mean ink run 1.76 px against 1.86-2.02 elsewhere).
- But 20261104-162225 has a similar dark fill (value about 45) and still scores 0.94 on the shipped sample.
- Text hue (about 30) and peak value (225-233) are the same in every match.
- Sub-pixel placement from the capture scaler is another possible cause. It was not measured.
