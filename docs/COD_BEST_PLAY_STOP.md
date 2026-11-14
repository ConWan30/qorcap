# CoD practice auto stop: stop on BEST PLAY (practice lane only)

Practice-lane recording control. Not a final, not a score, not a receipt or bundle.json field.
REAL MATCH, receipt.v0, contracts/ and schema/ are unchanged.

## Why

Con asked (agent-relayed, Nov 13 2026, 5:12 PM CST): stop when the "Best Play of the Match" replay is seen.

On match-20261113-154154 the old rule (fixed 12 s tail after confirmation) did **not** stop before the
end board. From the video tail (box frames `/workspace/cod-1113b/154154/b/b_015..b_040.png`, 4 fps):

| video pts (s) | screen |
|---|---|
| ~604.0 | DEFEAT splash |
| ~606.5-612.9 | end board, Qortrola30 row 3 highlighted (K 29) |
| 608.4 | confirmed (splash+word_again), `cod_end_stop.json` |
| ~613-618.5 | black cut, Best Play intro |
| ~618.8 | BEST PLAY title |
| 621.7 | video ends (stop 12.1 s after confirmation) |

`owner_row_seen` was false because Qortrola30's row was found (tag matched) but `read_owner_row` stopped
at `deaths_not_read` on every board frame (`cod_signals.jsonl` owner_stage, t_ms 608025-613541). The
Domination board has no DEATHS column, and the personal-card DEATHS read failed. That is a reader
gap, not a timing gap. It is not fixed here. The `scoreboard` HUD region is `not_present` in every row
of both 154154 and 145809; it is the mid-match Tab board hook and does not track the end board.

Finding (not fixed here, UNCONFIRMED beyond one match): on 145809 the personal card below the board
showed the cursor-selected player (row 3), not Qortrola30 (row 5). `read_owner_row` returned deaths 18
from that card, which is that other player's number.

Follow-up (this build): card deaths are used only when the card's name matches Qortrola30. On
match-20261113-211713 the card was another player's (DEATHS 27); that card now gives `deaths` null with
`card_name_match` false. See `docs/GAME_SETS.md` for `deaths_status`. A row read with deaths
`UNKNOWN_NO_COLUMN` does not shorten the stop: for `owner_row_seen` (sustained bar, splash path) and
for the BEST PLAY board floor it counts as no row, so those frames wait for the same 16 frames / 6 s,
the same splash resight and the same 8 s floor as a frame without the row.

## Owner-row fields

Four files talk about Qortrola30's row. They mean different things. None is a receipt field.

| file | field | meaning |
|---|---|---|
| `cod_end_stop.json` | `confirmed.owner_row_seen` | The row was the read that confirmed the end screen (`splash+owner_row`, or a sustained run with the row). A row read after confirmation does not change it. A row with deaths `UNKNOWN_NO_COLUMN` does not count. |
| `cod_end_stop.json` | `owner_row_after_confirm_t_ms` | First frame after confirmation where the row passed the per-frame read with its deaths. One frame is enough; it is not an accepted row. A row with deaths `UNKNOWN_NO_COLUMN` does not set it. |
| `cod_reader_stats.json` | `owner_row_ever_read` | At least one frame passed the per-frame row read: tag, kills, and deaths, or deaths `UNKNOWN_NO_COLUMN` on a board with positive no-DEATHS-column evidence. Same name as before; it now also turns true on those UNKNOWN frames. It does not mean accepted. |
| `cod_reader_stats.json` | `owner_row_good_frames` | How many frames passed that per-frame read. |
| `cod_reader_stats.json` | `owner_row_accepted` | The row met the 3-frame / 3000 ms agreement and the owner-row consensus and was committed with the final. Only then does `cod_session.json` carry `owner_row`. |

On match-20261113-211713 the row passed on one frame (t_ms 752650), 1531 ms after the stop had
confirmed on `splash+word_again` at 751119. So `owner_row_seen` was false, `owner_row_ever_read` was
true, and no row was accepted: the frames after it stopped at `kills_not_read` and each one clears
the end candidate. On match-20261113-210612 the row passed on 5 frames in a row and was accepted
(kills 14, deaths 17). Replayed through this build, those reads would give `owner_row_good_frames` 1 / `owner_row_accepted`
false and 5 / true. `end_eval.owner_stage_detail` in `cod_signals.jsonl` names the digit check that
failed (see `docs/GAME_SETS.md`). No gate, threshold, or agreement rule changes.

## Rule

After confirmation the watcher keeps reading Qortrola30's row (until read) and the BEST PLAY title
(`sidecar/qorcap_cod_bestplay.py`, mask `sidecar/packs/cod_best_play.json`). It stops at the first of:

- the title on 2 frames in a row within 1.5 s, once Qortrola30's row was read or 8 s
  (`BOARD_FLOOR_S`) have passed since confirmation;
- 25 s after confirmation (`QORCAP_AUTOCLOSE_BEST_PLAY_MAX_S`, clamped 15..45), title or not.

The bound comes from `stop_due`, which the main loop polls on its own clock, so a missed title cannot
keep the recording going past it. `QORCAP_AUTOCLOSE_BEST_PLAY=0` restores the 12 s tail.

Timing observed on five practice tails (splash and title from contact sheets, 1 s / 0.25 s steps,
ESTIMATED to about 0.5 s):

| match | splash -> board | board on | splash -> title | confirm -> title |
|---|---|---|---|---|
| 20261113-154154 | ~2.5 s | ~6.4 s | ~14.8 s | 10.4 s |
| 20261113-145809 | ~3 s | ~6.5 s | ~15.5 s | 12.7 s |
| 20261113-133025 | ~3 s | ~7 s | ~13.8 s | UNKNOWN (clip start ESTIMATED) |
| 20261111-183311 | ~3 s | ~7 s | ~15.3 s | no cod_end_stop.json |
| 20261111-194037 | ~2 s | ~7 s | ~15.5 s | no cod_end_stop.json |

- 8 s floor: the board ended 4.5-6.6 s after confirmation on 154154 and 145809. The floor only
  delays a stop; the title itself comes after the board in all five.
- 25 s bound: the latest title was 15.5 s after the splash, and confirmation is never before the
  splash, so 25 s is that plus about 9.5 s (over 60 %) margin. Past it the recording is inside the
  Best Play replay or the lobby; only extra seconds are recorded.

## Detector and held-out counts

HSV value > 200 ink in a fixed box at the bottom right (720p x 600..1230, y 490..660), halved,
`TM_CCOEFF_NORMED` against a 42x259 mask. Gate 0.60, PROVISIONAL.

Match-level split. Tuning (mask and gate): 145809, 154154. Held-out: 133025, 183311, 194037 (title
present) and 081216 (2 tails), 165658, 131754 (no title: gameplay, lobby and menu screens). 073204 is
excluded and was not opened. Labels are one rater (QorArch) from contact sheets; a second rater is UNMEASURED.

4 fps frames of the last 40 s (75 s for 131754); slide-in and fade frames are not scored:

| split | title frames read / total | other frames read / total | max score on other frames |
|---|---|---|---|
| tuning | 15 / 15 | 0 / 307 | 0.21 |
| held-out | 21 / 22 | 0 / 1197 | 0.26 |

The one held-out miss is 133025 at a fade (score 0.51). End splash and board frames score at most
0.04. All 3 held-out title matches fire; none of the 6 held-out tails fires on a non-title frame.

Replays: `sidecar/fixtures/cod/end_timelines_best_play.json` (reader outputs only, no pixels; built
by `tools/cod_best_play_timelines.py`). In all five the stop is after the last board frame and on the
second title read, 10.5-13.1 s after confirmation. With the title read removed, all five stop at
exactly 25 s.

UNKNOWN: other modes and maps, other end-of-match flows (no Best Play, party leaves, error screens),
other resolutions than 720p and 1080p. Five matches is a small sample.
