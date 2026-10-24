# Template provenance

Glyph and digit templates used by the scorebug reader and the end-menu reader were
partly cut from held-out evaluation matches. A template cut from a held-out match
counts as training on it. Those templates are removed. A template whose source
match cannot be named is treated the same way.

The only matches that may supply a replacement are template-only games. They are
never test or evaluation data:

| match folder | id |
| --- | --- |
| `match-20261024-102332` | `102332` |
| `match-20261024-111640` | `111640` |

Their footage is on the operator's Windows PC and is not in this repo. This
change ships the harvest script, the manifest, and the CI check. It does not
ship harvested pixels. Until those pixels are committed, the readers abstain.
They do not guess a score.

`model_id` for the standard reader is a hash of each allowlisted template's
role, label, profile, and bytes (or an explicit missing marker) and the grammar
and clock parameters. Relabelling a glyph changes the id even when the bytes
do not. The reader abstains unless score digits 0-9 are all present. A partial
clock set is not installed, and the stock clock font is not used in its place.

| | id |
| --- | --- |
| previous (main, stock pack plus the 110028 and 084523 rows) | `local_scorebug_v1:madden27-standard-v1:ac836591d618adb8` |
| this change (no allowlisted templates loaded) | `local_scorebug_v1:madden27-standard-v1:322a3c85a62409de` |

The id will change again when harvested templates are added, because those bytes
enter the hash.

## Where the records live

| file | role |
| --- | --- |
| `templates/provenance.json` | one row per glyph. sha256, source match id and full folder, video sha256, measured `t_ms`, frame index, the labelled time or frame range, harvest script version, harvest time (UTC). Empty until a harvest is committed. |
| `templates/template_sources.json` | allowlist. Exactly `102332` and `111640`, each with the full folder name and the sha256 of `video.mp4`. An empty hash refuses the harvest. |
| `templates/heldout.json` | held-out ids. The harvest script refuses them. |
| `schema/template_provenance.v0.json` | JSON Schema for the provenance file. |

This provenance file is a QorCap-internal artifact. It is not a `receipt.v0`
field and it is not a vendored QorTime bundle field. `contracts/receipt.v0.json`
is unchanged.

The harvest script writes its own `provenance.json` into the output directory,
next to the template files. Paths in that file are relative to the file. Copy
that tree into `templates/` so the committed `templates/provenance.json` stays
beside the files it names.

## Inventory

Status is one of:

* **held-out-derived, removed** — at least one glyph was cut from 170123, 021003, 125430, 084523, or 193534
* **unknown, treated as held-out-derived, removed** — the file does not name a source match
* **known source, not an allowlisted template match, removed** — the match is named and is not held out, but `templates/template_sources.json` does not list it, so it cannot ship under the CI check

Method is how the vectors were built. Frame index is what the pack still recorded. "not kept" means the npz stored a mean and dropped the frame list.

| asset | source match | method | status |
| --- | --- | --- | --- |
| `sidecar/packs/madden27_standard_score_084523.npz` (removed; added in `6f7fd79`, v0.3.23) | `084523` (`match-20261018-084523`) | mean of 111 real "4" glyphs, `tools/add_standard_four_084523.py`. Bands b0000–b0260 were eligible; the npz did not list which bands produced a glyph. | held-out-derived, removed |
| `sidecar/packs/madden27_sunday_night_v1.npz` (removed; added in `67853bf`, v0.3.11) | `002326` and `004308`, mixed in the same mean with `021003` for score 0, 1, 2, 3, 4, 6, 8 and clock 0–8 and quarter 3 and 4. Score 5 and 9 are `synthetic:clock_font_resized`. Quarter 1 and 2 and score 7 and clock 9 do not name `021003`. | per-label mean. Frame index not kept. A mean that includes a held-out frame cannot be split, so the whole pack is removed. Pack metadata names `021003`. | held-out-derived, removed |
| `sidecar/packs/madden27_end_menu_v1.npz` (removed; added in `0e4f6b8`, v0.3.4) | `125430` (95 frames, final 7–14) and `143212` (300 frames, final 0–20) for real 0, 1, 2, 4, 7. The npz source strings are `real:95`, `real:300`, and `real:600` (the 600 count is not explained by 95+300). Synthetic 3, 5, 6, 8, 9 from Hind SemiBold, Prompt SemiBold, and Ubuntu Bold via `tools/make_end_menu_pack.py`. | mean glyph, or a font mean. Frame index not kept. | held-out-derived, removed. `real:600` is unknown and treated as held-out-derived. |
| `sidecar/packs/madden27_end_menu_v2.npz` (removed; v1 plus rows from `9c316d8`, `b935fb2`, `61b958c`) | v1 rows, plus real 9 from `170000` (`real:21`), a 6 from `091651`, and 0/3/6 from `110028` (15 frames). `ot_vec` has no match id. | same tool, augment mode. Frame index not kept. The file still contains the `125430` rows. | held-out-derived, removed |
| `sidecar/packs/madden27_psn_banner_v1.npz` (removed; added in `41881ab`) | none recorded. Source tags are only `font` or `seen`. `tools/make_psn_pack.py` can exclude a prefix such as `021003`; the committed pack did not say whether that exclude was the one used. | font render, or a glyph marked seen. Frame index not kept. | unknown, treated as held-out-derived, removed |
| `sidecar/packs/madden27_standard_score_110028.npz` (removed; added in `61b958c`, v0.3.14) | `110028` (`match-20261017-110028`) | mean of 148 twos and 132 threes. Source string `real:match-20261017-110028`. Frame index not kept. Not held out and not in `human_labels.jsonl`. | known source, not an allowlisted template match, removed |
| `sidecar/packs/madden27_thursday_night_v1.npz` (removed; `67853bf` through `e4de196`) | `010703`, plus quarter rows that also name `091651`, `232726`, and `002316`. No held-out id in the source strings. | real glyphs, counts in the source string (`real:match-20261017-010703:131` and the later additions). Frame index not kept. | known source, not an allowlisted template match, removed |
| `sidecar/packs/madden27_monday_night_v1.npz` (removed; `9c316d8`, extra rows in `674cb20`) | `170000` for the original rows (`real:strip.mp4` in the npz; the match id is in the v0.3.5 commit message and `tools/make_monday_night_pack.py`, not in the source string). Score 5 is `synthetic:clock_font_resized`. Extra rows from `162055` (`tools/add_monday_night_162055.py`); the template id contains the frame, for example `162055:m12:clock:6:f_0751750:2`. | real crops, plus one synthetic digit. | known source, not an allowlisted template match, removed. `real:strip.mp4` does not name a match inside the npz; the commit message does. |
| `sidecar/packs/madden27_standard_clock_170123.npz` | `170123` | mean of stock-pack argmax labels on 90 clean highlight frames. Already deleted before this change (`sidecar/packs/template_manifest.json` on main). | held-out-derived, removed |
| `sidecar/packs/madden27_banner_fragments.npz` | `170123` (hl00 frame 175 white GOA, hl01 frame 174 dark TOUC) | fragment cut. Already absent on main. | held-out-derived, removed |
| QorTime `qor_receipt/_vendor/qoresence/vision/local_scorebug/packs/madden27_standard_v1.npz` | September 2026 HDMI clips `hdmi_clip_20260906_111605` through `hdmi_clip_20260912_015442`, plus `hdmi_live_bottom` and `live_frame_receipt5` (244 frames, built 2026-10-08). Not one of the five held-out matches. | one mean vector per label. Frame index not kept in the npz. | not in this repo. Vendored with QorTime. Not deleted here. The QorCap standard reader does not emit a score from it while allowlisted templates are missing. |

Fixture images under `sidecar/fixtures/` are not templates. The reader does not load them as glyph packs. They were not deleted. Tests must not harvest templates from held-out matches (`170123`, `021003`, `125430`, `084523`, `193534`) or from the template-source games (`102332`, `111640`). Tests may grade the reader on held-out matches. That is what those matches are for. The evaluation modules skip with `templates_missing` until an allowlisted harvest is installed, and those skip names are listed in `tools/assert_pytest_skips.py` so the skip stays visible. They run again once score templates load.

`sidecar/packs/held_out_sha256.json` is a blocklist of public **frame and clip** hashes for `170123`, `021003`, and `125430` (paths under `site/matches/.../events/` and `site/matches/.../clips/`). It does not contain the sha256 of a full match `video.mp4`. A renamed held-out match is refused because the allowlist pins the folder name and the sha256 of that allowlisted match's own `video.mp4`, not because this table knows the held-out match video. The harvest script still refuses a file whose sha256 is in the table. `084523` and `193534` have no source video in this repo; those ids are refused by path.

The Windows zip copies `templates/` next to `sidecar/`. The three JSON files are required install files because the readers open them. With `templates` an empty list, the installed reader abstains the same way the repo does.

## What the reader does with no templates

`standard_reader`, the Thursday / Sunday / Monday night readers, the end-menu reader, and the PSN banner reader load a pack only when `templates/provenance.json` lists that file with a matching sha256 and an allowlisted match id. With an empty list they return `templates_missing`, with no score digits and no guessed name.

The log line, also stored on the in-memory timeline and posted once as an observation, is:

`templates_missing: no allowlisted scorebug templates are loaded; abstaining; no score read`

That observation is not a score event. It has no `home` or `away`.

## Harvest the replacements

On the operator PC, from a git checkout of this repo, follow `docs/HARVEST_RUNBOOK.md`. The script is `tools/harvest_templates.py` version `3`. It calls the reader's own glyph segmentation and saves one binarised vector per glyph. The file name carries the match id, the glyph, and the frame (`score/102332__1__score_left__f000012__g0.npy`). QorTime is resolved the same way Start Match resolves it (`--qortime`, `$QORTIME`, then the installed `qortime-<pin>`), and that commit is stored on every provenance row, before the video is hashed.

Sunday, Monday, Thursday, and PSN readers have no harvest regions. After this harvest only the standard bar and the end menu can come back.

The operator commands, including the pin, the preview, the dry-run, and the merge, are the two PowerShell blocks in `docs/HARVEST_RUNBOOK.md`. A skeleton labels file is `docs/templates/labels.example.json`.

A labels item is `role`, `label`, `roi`, `frame_index`, and a range (`start_frame_index`/`end_frame_index`, or `start_t_ms`/`end_t_ms`). `label` is the slot text. A score label `14` is kept only when the reader segments two glyphs, and the saved labels are `1` and `4`. Two different labels for the same slot and frame are refused. Clock labels are digits, with an optional colon. Quarter labels are `1st`, `2nd`, `3rd`, `4th`, or `ot`. The person running the script chooses the label. The script does not invent one. `harvest_date` is the UTC time of the run. The script refuses a match id outside the allowlist, any held-out id, a video whose sha256 is missing from the allowlist or is in the blocklist, a label the reader would not segment, a non-empty `--out`, and an `--out` path inside the match folder. It does not write into the match folder. Commit the merged templates on this branch. Do not commit the match video. Do not tag or release from this work. Do not merge while `templates` is empty.

## Tracked debt: numbers fitted on held-out matches

These are not templates. This change does not edit them.

| constant | where | fitted on |
| --- | --- | --- |
| `ANCHOR_SEARCH_PX = 8` and the full-resolution clock | `sidecar/qorcap_standard.py` | the 170123 highlight kit |
| `GOA_MIN = 0.66`, `TOUC_MIN = 0.72` | `sidecar/qorcap_banner.py` | the same clips |
| the 021003 touchdown look-alike safeguard (`TD_MIN_MARGIN` and the 0/8, 0/6, 0/9, 3/9 rules) | `sidecar/qorcap_reader.py` | match 021003 |
