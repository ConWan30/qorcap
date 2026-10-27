# Meta AI Developer Hackathon: this repo vs QorTime

> **The hackathon submission repo is [`ConWan30/qortime-ai`](https://github.com/ConWan30/qortime-ai)** (event work only).
> **This repo is pre-existing context and is not the submission.**

QorCap is the **Windows capture client** for [QorTime](https://github.com/ConWan30/qortime). It is pre-existing
supporting infrastructure for the QorTime entry on the **Indie Developer** track (solo build).

## Pre-existing (before Mon Oct 26 2026, 12:00 PM CT)
This entire repository is **pre-existing capture tooling** built before the hackathon kickoff. It records HDMI
capture-card video into a hash chain, logs DualSense USB input, runs pause challenges, and writes match bundles
that QorTime's referee can verify. Nothing in this repo calls Meta model APIs.

**Meta models are used in QorTime**, not here. See
[`ConWan30/qortime` `HACKATHON.md`](https://github.com/ConWan30/qortime/blob/main/HACKATHON.md) for the
pre-existing vs built-during split, Muse Spark / SAM 3.1 / Muse Voice Transcribe usage, and the demo framing
(anti-cheat and verified skill, not a coach).

## Pull requests merged on/after kickoff (non-Meta bugfixes / release)
These landed after Mon Oct 26 2026, 12:00 PM CT. They are recorder correctness and release packaging only — they
do not add Meta model calls.

| PR | One line |
|---|---|
| [#116](https://github.com/ConWan30/qorcap/pull/116) | Fix end-menu `home_glyph_count` on clipped logo fringe (online FINISH GAME) |
| [#117](https://github.com/ConWan30/qorcap/pull/117) | v0.3.38: end-menu clipped logo fringe release |
| [#118](https://github.com/ConWan30/qorcap/pull/118) | This file, README link, and re-vendored `contracts/compat.json` from qortime main (motion rule v4 contract drift) and its contract lock test; no recorder code change |

No Meta-model work was built in this repository during the hackathon window.
