# Game sets

Start Match asks which game to load, then which side you are on. Each set loads its own profile. A direct `qorcap_match.py` run with no `--game` stays on Madden, so older recordings keep the scorebug path.

`Start Match.cmd --game madden` or `--game cod` skips the prompt. A one-shot `next-game.txt` in `%LOCALAPPDATA%\QorCap` does too (`madden` or `cod`, also `mw4` and `call of duty`). The file is renamed after use and ignored when it is older than 4 hours or not one of those words.

The choice is written to `game_set.json` beside the match. That file is not a receipt field. The receipt still uses `match.game` and `match.scorebug_profile`.

## Madden

- Set id `madden`. Receipt game `madden27`. Profile starts at `madden27_standard` and can still move to a night profile when that reader agrees.
- Loads home and away, the scorebug, and the end-of-game menu.
- The match ends when that menu is read. Stopping the recorder is not that read.
- Glimpse stays the Madden scorebug advisory. It does not feed a Call of Duty hook or the receipt.

## Call of Duty (MW4 / modern)

- Set id `cod`. Receipt game `cod_mw4`. Profile `cod_mw4_hud`.
- The match window ends when the end screen agrees (3 strictly later timestamps across 3 seconds), the same bar Madden uses for a final screen. Home and away have to both be read before that count starts. A frame that is not the end screen clears it. A repeated timestamp or a gap longer than 3 seconds starts it over. The result and the score pair have to stay the same. A scoreboard number is not that pair. Stopping QorCap does not read that screen. The receipt quarter is 4 because this end screen has no game clock. It is not a Madden quarter.
- `final_confirmed` is written only when a calibrated end-screen hook sets confirm and the runner posts a final. That is end-screen agreement. It is not a verification verdict. DEFEAT can agree on the header. Home and away stay unassigned on these frames, so the 3-frame, 3-second bar is never reached and no final is posted today.

### Controller and video clock

The shared clock is the opt-in frame clock (`--frame-clock` or `QORCAP_FRAME_CLOCK=1`, default off). A stamp has to sit within 500 ms of both the screen time and the input time. That is the same window as `MEASURED_MAX_GAP_US` in `src/frameclock.rs`. The manifest fit is not a stamp. With the frame clock off, a hook is not Counted.

`cod_session.json` records `clock_sync` as agreed, flagged a mismatch, or not Counted. The same status is copied onto `bundle.json`. Neither file is a receipt field, and the status is not a verification verdict.

### HUD chain

These are agreement signals for how the match was played.

1. Hitmarker, against an R2 rising edge (more than 60 counts, the same step the haptic detector already uses).
2. Kill popup, after a hitmarker that already agreed.
3. Killfeed, after that popup.
4. Pause menu, against OPTIONS (bit 9, the same bit the Madden path uses).
5. Recoil and view motion, from a haptic row the existing detector already labeled high. A `grip_jolt` or `hand_motion` row stays low there, so it is not Counted.

A hook that is not calibrated is not Counted. A calibrated hook whose prior step did not agree is flagged a mismatch. Crosshair is a crop, not a counted event. Hitmarker, the XP plate (presence only; a 25 or a 50 is not an assist), and killfeed weapon-icon presence are calibrated from Con's private practice frames. Pause and the crosshair still return `not_read`. They post no score and no final.

### Scoreboard

Kills and assists from an offline count can be compared with the end scoreboard. Both sides need both numbers. The calibrated hook reads K and A when the SCORE / KILLS / ASSISTS / DEATHS row is visible. A missing number is not Counted. Different numbers are flagged a mismatch. There is no offline count on these frames, so the comparison stays not Counted. This hook does not post a final and does not change the receipt by itself.

### INFO-ONLY timing

`stick_turn_vs_camera`, `rumble_vs_hit`, and `power_aware_timing` are labeled INFO-ONLY in the set config. Each status is not Counted. They stay that way until a held-out match promotes them. Battery versus plugged is not read, so no timing band is applied.

### Session tags

`cod_session.json` sits beside the match:

- `power` is `unknown`.
- `controller_path` is `usb` only when the input log header already says `usb-hid` (the path QorCap read). Otherwise it is `unknown`.

These tags are not written onto input rows. Input rows stay in the checkpoint hash, and this set does not change them.

### Receipt

Agreement across the signals above is a checkable record of the match. Disagreement is flagged a mismatch, or the signal is not Counted. The recorder does not stamp a hosted verification on its own. A local signature stays on the path Start Match already uses.

QorCap/QorTime’s CoD path is built so a player can leave a correlated match record (controller + video + HUD + scoreboard hooks) that others can check: agreed, Counted, a receipt, a mismatch, or not Counted.

Official challenges stay closed on this set. No Call of Duty pause moment is wired for them. Anchors can still run.

## Fail closed

- An unreadable `game_set.json` does not run the Madden scorebug. The receipt note says no score was Counted.
- A missing `game_set.json` on an older bundle stays Madden, unless the bundle already names `cod_mw4`.
- The Call of Duty offline pass does not decode the Madden scorebug.

## Still to do

Calibrated from Con's private practice frames (Kill Confirmed match-20261105-173922, plus earlier scoreboard and HUD crops). This is not a public verification.

- VICTORY on the splash and on the match-complete header. A mid-match frame does not confirm. A blurred word does not confirm. The sharpness floor stays 0.26. The ratio is the 99th percentile of the gradient over the contrast from the 5th to the 98th percentile of the word box. The previous ratio (95th percentile over the 10–90 contrast) scored the large splash near 0.33, so a one-pixel blur (near 0.25) and a 720p frame scaled up to 1080p (near 0.23) missed while the letters still matched. Those two agree again. A sigma-2 blur of the splash or the header stays near 0.20 and does not confirm. The letter score is not that check. A brightness shift of about 30% and a 540p header can still miss when the white mask drops the letters.
- DEFEAT on the top-right header when all six letters agree and the field around the word is red. Checked on private practice headers for Domination (match-20261101-092413), Team Deathmatch (match-20261104-162225), and Kill Confirmed (match-20261104-202724). The Kill Confirmed letters are a thinner cut, so pieces of one letter are joined before that word is read. The glitched splash does not agree. A blurred word does not agree. DEFEAT uses the same sharpness ratio and the same 0.26 floor, so a clean header still agrees and a sigma-2 blur still does not. A mid-match frame does not agree. A VICTORY frame does not confirm as DEFEAT, and a DEFEAT frame does not confirm as VICTORY. This check is still not a public verification.
- Hitmarker strokes, the red kill marker (presence only, not a kill count), the XP plate (presence only; a 25 or a 50 is not an assist), and killfeed weapon-icon presence.
- Scoreboard K and A when that digit row is visible. Missing digits stay not Counted.

Still open:

- Home and away are not assigned from the team totals on these frames. The screen does not label those sides, and the receipt side is the side chosen at the start. No final score is posted until both are read. Re-read the end screen during finalize once that read exists.
- The scoreboard comparison stays not Counted until an offline kills and assists count exists. It does not post a final.
- Pause and the crosshair are still not read.
- Stick turn against camera turn, rumble against an on-screen hit, and power stay INFO-ONLY and not Counted.
- Read battery versus plugged only after a report map exists. Do not guess bytes. Power stays `unknown`.
- Record the console link (wired or wireless) only when the log actually has it. `usb` today means the laptop read path.
