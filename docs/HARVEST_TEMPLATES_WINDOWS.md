# Harvest scorebug templates on Windows

The commands are in [HARVEST_RUNBOOK.md](HARVEST_RUNBOOK.md). Each game is a
self-contained PowerShell block: it sets `$docs`, `$py`, and `QORTIME`, pins
`video.mp4` with `tools\pin_template_source.py` (lowercase sha256), previews a
frame, dry-runs, then harvests into an empty folder under Documents.

`Get-FileHash` is not the pin step. It prints uppercase hex. The pin tool
stores lowercase sha256 and refuses a match id that is not `102332` or `111640`.
