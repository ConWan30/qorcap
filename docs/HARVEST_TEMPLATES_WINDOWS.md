# Harvest scorebug templates on Windows

Run this from the qorcap repo in PowerShell. The match folder is only read.
`--out` must be a different folder, not inside the match. Game 1 is
`match-20261024-102332`. Game 2 is `match-20261024-111640` (change the three
paths). Both ids are the only entries in `templates\template_sources.json`.
`templates\heldout.json` is refused even if an allowlist names one of those ids.

The footage for these two games is on the operator PC. It is not in this repo.
This document is the one-liner the harvest script also prints (`--windows-doc`)
and writes to `HARVEST_WINDOWS.txt` in the output folder.

One-liner (game 1):

```powershell
python tools\harvest_templates.py --match-dir "$env:USERPROFILE\Documents\QorCap Matches\match-20261024-102332" --labels "$env:USERPROFILE\Documents\QorCap Templates\labels-102332.json" --out "$env:USERPROFILE\Documents\QorCap Templates\out-102332"
```

Game 2 is the same line with `102332` replaced by `111640` in the match folder,
the labels file, and the output folder.

Labels file (a person picks every glyph; the script does not guess a digit):

```json
{
  "items": [
    {"role": "score", "label": "0", "roi": "score_left", "frame_index": 12, "t_ms": 400}
  ]
}
```

Roles: `score`, `clock`, `quarter`, `banner`, `end_menu`.
ROIs: `score_left`, `score_right`, `clock`, `quarter`, `banner`, `end_menu_away`, `end_menu_home`.

Each item needs `frame_index` (decoded frame, from 0). `t_ms` is optional and is
stored on the provenance row when you set it. The match folder must contain
`video.mp4` and either `timeline.jsonl` or `anchors.jsonl`. The folder name must
be `match-YYYYMMDD-HHMMSS`.

After both games:

1. Copy the `.npy` and `.png` files into the repo `templates\` directory, keeping the same relative paths the script wrote (`score\...`, `clock\...`, `end_menu\...`).
2. Merge the `templates` arrays from each output `provenance.json` into `templates\provenance.json`. Paths stay relative to that file.
3. Commit those files. Do not commit the match video.
4. CI rejects a template file with no provenance row, a sha256 that does not match the bytes, or a `source_match_id` other than `102332` or `111640`.

Until those files are committed, the scorebug reader abstains. It logs
`templates_missing: no allowlisted scorebug templates are loaded; abstaining; no score read`
and posts no score.
