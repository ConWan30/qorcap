# Harvest scorebug templates on Windows

Run this from a git checkout of qorcap. `tools\` is not in the release zip.
Start Match's venv has numpy and opencv. Bare `python` may not.
`Documents` may be redirected to OneDrive. `GetFolderPath("MyDocuments")` is
the same resolution Start Match uses.

The match folder is only read. `--out` must be a different folder, not inside
the match. Game 1 is `match-20261024-102332`. Game 2 is
`match-20261024-111640`. Both ids are the only entries in
`templates\template_sources.json`. `templates\heldout.json` is refused even if
an allowlist names one of those ids.

Pin `video.mp4` first. An empty `video_sha256` refuses the harvest, so a
renamed held-out folder cannot pass by name.

```powershell
cd "$env:USERPROFILE\src\qorcap"
$docs = [Environment]::GetFolderPath("MyDocuments")
Get-FileHash -Algorithm SHA256 "$docs\QorCap Matches\match-20261024-102332\video.mp4"
```

Put the hash on that match's `video_sha256` in `templates\template_sources.json`.
Do the same for `111640`.

One-liner (game 1). Add `--dry-run` to list the frames and per-glyph crops
without writing:

```powershell
$py   = "$env:LOCALAPPDATA\QorCap\venv\Scripts\python.exe"
New-Item -ItemType Directory -Force "$docs\QorCap Templates" | Out-Null
& $py tools\harvest_templates.py --match-dir "$docs\QorCap Matches\match-20261024-102332" --labels "$docs\QorCap Templates\labels-102332.json" --out "$docs\QorCap Templates\out-102332"
```

Game 2 is the same line with `102332` replaced by `111640` in the match folder,
the labels file, and the output folder.

Labels file. `label` is the slot text. After a clock colon is removed, its
length must equal the number of glyphs the reader segments. Each glyph is
saved on its own, already binarised the way the reader matches it. A score
label `14` becomes templates `1` and `4`, not one template named `14`.

```json
{
  "items": [
    {"role": "score", "label": "14", "roi": "score_left", "frame_index": 1200, "start_frame_index": 1200, "end_frame_index": 1260}
  ]
}
```

Roles: `score`, `clock`, `quarter`, `end_menu`.
ROIs: `score_left`, `score_right`, `clock`, `quarter`, `end_menu_away`, `end_menu_home`.

Score, clock, and end-menu labels are digits `0-9`. A clock label may contain
a colon (`5:23` is three digits). Quarter labels are `1st`, `2nd`, `3rd`,
`4th`, or `ot`. Banner is not harvested. Sunday, Monday, Thursday, and PSN
have no regions in this script, so those readers stay unavailable.

Each item needs `frame_index` and a range that contains that frame:
`start_frame_index` and `end_frame_index`, or `start_t_ms` and `end_t_ms`.
The script checks the range against the decoded frame. `harvest_date` is the
UTC time of the harvest, not the date in the folder name.

The match folder must contain `video.mp4` and either `timeline.jsonl` or
`anchors.jsonl`. The folder name must be `match-YYYYMMDD-HHMMSS` and must
match the allowlist entry.

After both games:

1. Copy the `.npy` and `.png` files into the repo `templates\` directory, keeping the same relative paths the script wrote (`score\...`, `clock\...`, `end_menu\...`).
2. Merge the `templates` arrays from each output `provenance.json` into `templates\provenance.json`. Paths stay relative to that file.
3. Commit those files on this branch. Do not commit the match video. Do not merge while `templates` is an empty list.
4. CI rejects a template file with no provenance row, a sha256 that does not match the bytes, a `source_match_id` other than `102332` or `111640`, or a provenance row that fails `schema/template_provenance.v0.json`.

Until those files are committed, the scorebug reader abstains. It logs
`templates_missing: no allowlisted scorebug templates are loaded; abstaining; no score read`
and posts no score.
