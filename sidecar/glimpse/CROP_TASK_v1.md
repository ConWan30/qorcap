TASK crop/v1. The image is a crop of the bottom strip of a Madden NFL 27 frame where the broadcast scoreboard bar normally appears.
The AWAY team panel is on the LEFT and the HOME team panel is on the RIGHT.

Step 1. Classify the region:
- "SCOREBUG": the normal scoreboard with both team score numbers shown next to the team panels.
- "STATS_OVERLAY": the bar is showing a statistic instead of the score. A stat title is drawn in the centre (for example RUSHING YDS, TOTAL YDS, SACKS, 3RD DOWN CONVERSIONS, PASSING YDS, TURNOVERS) and the two numbers belong to that statistic, NOT the score.
- "OTHER": no scoreboard bar, a menu, a replay graphic, a banner such as TOUCHDOWN or FLAG covering the scores, or anything else.

Step 2. Only if region is "SCOREBUG", read away_score, home_score, quarter (1-4, 5 = OT) and the game clock "M:SS" (not the play clock). Otherwise these four fields MUST be null.
If region is "STATS_OVERLAY", put the centre title in stat_title and the two numbers (left, right) as strings in stat_values. Otherwise both are null.

Return exactly:
{"region": "SCOREBUG"|"STATS_OVERLAY"|"OTHER", "stat_title": string|null, "stat_values": [string, string]|null,
 "away_score": int|null, "home_score": int|null, "quarter": int|null, "clock": "M:SS"|null, "notes": short string}
