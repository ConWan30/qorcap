# Harvest runbook (Windows PowerShell)

One git checkout of `cursor/template-provenance-129c`. `tools\` is not in the
release zip. Start Match's venv has numpy and opencv. `Documents` may be
OneDrive-redirected; `GetFolderPath` is the same path Start Match uses. Do not
commit `video.mp4`. Do not tag or release. Do not merge while
`templates/provenance.json` has an empty `templates` list.

Each block below is a fresh window: it checks out the branch and defines
`$docs`, `$py`, and `QORTIME`. Dry-run first. It lists the crops and writes
nothing. `--out` must be empty and must sit outside the match folder. A rerun
needs a new `out-<id>` folder, or the old one deleted. A nonempty `--out` is
refused.

Two labels for the same slot and frame are refused. The standard reader
abstains until score digits 0-9 and clock digits 0-9 are all present. The
end-menu reader abstains until end-menu digits 0-9 are all present. Do not
stop once both games' final screens are labelled. Sunday, Monday, Thursday,
and PSN have no regions, so this harvest does not bring those readers back.

Labels are UTF-8 JSON. A BOM is accepted. Each game block copies
`docs\templates\labels.example.json` into place. Edit that copy before the
dry-run. `label` is the slot text. `14` is kept only when two glyphs segment,
and the saved labels are `1` and `4`. One label per slot per frame. Find
`frame_index` with `preview_frame.py` (same decode order as the harvest).
Roles: `score`, `clock`, `quarter`, `end_menu`.

The clock row in `labels.example.json` uses `start_t_ms` 40000 and `end_t_ms`
42000 around frame 1200. That window assumes 30 fps (frame 1200 is 40000 ms).
`preview_frame.py` prints `fps`. When that print is not 30, set `t_ms` from
the printed fps: `frame_index / fps * 1000`.

`sidecar\packs\held_out_sha256.json` holds public frame and clip hashes, not
the hash of a full match video. The guard is the allowlisted folder plus the
pinned sha256 of that match's `video.mp4`. The pin tool requires the video's
folder to be the allowlisted match folder (`match-20261024-102332` or
`match-20261024-111640`). It refuses a path that contains a held-out id
(`170123`, `021003`, `125430`, `084523`, `193534`) and a sha256 listed in
that table. `pin_template_source.py` and `merge_template_provenance.py` write
JSON with LF line endings on Windows.

If the QorTime checkout is not commit
`0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2`, the harvest prints a warning and
continues. The recorded commit is the checkout that was used.

## Game 1, match-20261024-102332

```powershell
cd "$env:USERPROFILE\src\qorcap"
git checkout cursor/template-provenance-129c
$docs = [Environment]::GetFolderPath("MyDocuments")
$py   = "$env:LOCALAPPDATA\QorCap\venv\Scripts\python.exe"
$env:QORTIME = "$env:LOCALAPPDATA\QorCap\qortime-0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2"
New-Item -ItemType Directory -Force "$docs\QorCap Templates" | Out-Null
Copy-Item -Force docs\templates\labels.example.json "$docs\QorCap Templates\labels-102332.json"
& $py tools\pin_template_source.py --match-id 102332 --video "$docs\QorCap Matches\match-20261024-102332\video.mp4"
& $py tools\preview_frame.py --video "$docs\QorCap Matches\match-20261024-102332\video.mp4" --frame 0 --out "$docs\QorCap Templates\preview-102332.png"
& $py tools\harvest_templates.py --dry-run --match-dir "$docs\QorCap Matches\match-20261024-102332" --labels "$docs\QorCap Templates\labels-102332.json" --out "$docs\QorCap Templates\out-102332"
& $py tools\harvest_templates.py --match-dir "$docs\QorCap Matches\match-20261024-102332" --labels "$docs\QorCap Templates\labels-102332.json" --out "$docs\QorCap Templates\out-102332"
```

## Game 2, match-20261024-111640

```powershell
cd "$env:USERPROFILE\src\qorcap"
git checkout cursor/template-provenance-129c
$docs = [Environment]::GetFolderPath("MyDocuments")
$py   = "$env:LOCALAPPDATA\QorCap\venv\Scripts\python.exe"
$env:QORTIME = "$env:LOCALAPPDATA\QorCap\qortime-0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2"
New-Item -ItemType Directory -Force "$docs\QorCap Templates" | Out-Null
Copy-Item -Force docs\templates\labels.example.json "$docs\QorCap Templates\labels-111640.json"
& $py tools\pin_template_source.py --match-id 111640 --video "$docs\QorCap Matches\match-20261024-111640\video.mp4"
& $py tools\preview_frame.py --video "$docs\QorCap Matches\match-20261024-111640\video.mp4" --frame 0 --out "$docs\QorCap Templates\preview-111640.png"
& $py tools\harvest_templates.py --dry-run --match-dir "$docs\QorCap Matches\match-20261024-111640" --labels "$docs\QorCap Templates\labels-111640.json" --out "$docs\QorCap Templates\out-111640"
& $py tools\harvest_templates.py --match-dir "$docs\QorCap Matches\match-20261024-111640" --labels "$docs\QorCap Templates\labels-111640.json" --out "$docs\QorCap Templates\out-111640"
```

## Install into the repo

From the same checkout, after both games, including end-menu digits 0-9:

```powershell
cd "$env:USERPROFILE\src\qorcap"
git checkout cursor/template-provenance-129c
$docs = [Environment]::GetFolderPath("MyDocuments")
$py   = "$env:LOCALAPPDATA\QorCap\venv\Scripts\python.exe"
& $py tools\merge_template_provenance.py --dest templates --from "$docs\QorCap Templates\out-102332" "$docs\QorCap Templates\out-111640"
git add templates
git commit -m "Harvest score, clock, and end-menu templates from 102332 and 111640."
```

That stops if a path is already present. The commit is the operator's. Do not
commit the match video.
