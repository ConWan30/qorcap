# Hand timeline (`qorcap-hand-timeline/v0`), practice lane only

A read-only controller input log clocked to the video. In the **practice lane it is on by default**. Switch it
off for one session with `--no-hand-timeline` (or `QORCAP_HAND_TIMELINE=0`). `--hand-timeline` (or a true env
word) is a no-op alias in practice (already on) and is refused outside the practice lane. It also needs a
bundle folder and the controller on. Outside the practice lane (REAL MATCH and the default lane) nothing is
opened or written and no checkpoint key is added, so those runs stay byte-identical. `receipt.v0`,
`contracts/`, `schema/`, `seal.rs` and `bundle.rs` are unchanged. Practice still has no referee submit, upload
or gold.

An off session is a normal session: the start and stop checkpoints record `hand_timeline: false` (extra
`qorcap` keys), no hand file is written, and `qorcap check-hand-timeline` and `verify-bundle` treat it as a
PASS, not as tampered or failed.

Fail-soft: with no pad or the wrong device the logger records `missing`, `rejected_transport`, or `open_denied`
(alignment UNMEASURED) and never blocks or voids capture. A failed `GENERIC_READ` open of a gated pad is
`open_denied`, not plain `missing`. A report whose `t_us` is before the previous report is recorded as
`clock_rollback` (first-class status), then capture continues.

Visible status (SCOPE-edge-logger §7.4): the live state (`connected` / `missing` / `unplugged` / `stalled` /
`rejected_transport` / `open_denied` / `clock_rollback` / `off`) is on `GET /status` as `hand` (practice only; the field is omitted outside
practice, so REAL MATCH `/status` is unchanged) and is written to the log as `HAND TIMELINE: …`. There is no
silent background run: the logger starts only from an explicit practice capture (`qorcap record --practice-lane`,
or `QORCAP_LANE=practice`). `qorcap status` against that recorder shows the same object.

Scope documents (QorArch, 2026-11-20): `SCOPE-edge-logger.md` (02144d1c…4cf0), `SCOPE-hand-timeline.md`
(f0046d44…69de), `ADDENDUM-hand-timeline-qorai-1.md` (46ee68cf…e71e1), `ADDENDUM-hand-network-con-answers-1.md`
(b8e94883…c9a8), `ADDENDUM-hand-network-con-answers-1-amend-1.md` (5084fe59…2d718).

## Files and `D:` routing (practice lane only)

On Windows in the practice lane, hand and raw output defaults to `D:\QorCap\practice-hand\<session>\`
(where `<session>` is the bundle folder name, or `--match-id` if needed). Override with `--hand-dir <DIR>`
(CLI wins) or `QORCAP_HAND_DIR=<DIR>`. If `D:` (or the override) is missing or unwritable, the logger fails soft
and falls back to the session bundle folder (`hand_dir_source: "fallback_bundle"` with `hand_dir_fallback_reason`).

| File | What |
|---|---|
| `hand_timeline.v0.jsonl` | One JCS object per line: `header`, `status`, `in`, `frame`, `profile_swap_candidate`, `hb`, `raw_bind`, `chunk`. |
| `hand_raw.v0.bin` | ONE append-only raw file per session (`create_new`, `0600` on Unix, 0 B binary header). Fixed 80 B `v1` records: `u64_le qpc_ticks` (`0` = UNMEASURED) + `u64_le t_us` (engine `Instant`) + `64 B` USB input report `0x01`. |
| `hand_timeline_summary.v0.json` | At stop: file SHA-256, last head, chunk count, `raw_file`, `raw_bytes`, `raw_layout`, `raw_record_bytes`, `hand_dir`, `hand_dir_source`, `hand_dir_fallback_reason`, device models seen, fallback flag. |

`raw_layout: "v1"` (`raw_record_bytes: 80`) stores `t_us` next to `qpc_ticks` so the `Instant`-vs-QPC residual can
be measured later; the residual itself stays `UNMEASURED` until benchmarked.

Raw logging is **on by default** whenever the hand timeline runs. Switch it off for one session with
`--no-raw-usb` (or `QORCAP_RAW_USB=0`); `raw_file`, `raw_offset`, `raw_len`, and `raw_sha256` are then `null`
on `raw_bind` and `chunk` rows, and no `hand_raw.v0.bin` is created. Raw logging pauses (`paused: "disk_low"`)
if free space drops below `--min-free-gb`. Raw files stay on the local PC.

## Windows and the chain

Window `k` holds records with time in `[k * 1_000_000, (k + 1) * 1_000_000)` us on the engine clock (the same
`Instant` as `inputs.jsonl` `t_us` and `frame_clock.jsonl` `t_mono_us`; on Windows that is QPC). A window is sealed
250 ms after its end (frame arrivals can trail on the capture thread), or at stop (`partial: true`). A record for an
already sealed window goes into the oldest open window marked `late: true` and is counted in `hb.late_records`.

Each window ends with an `hb` row (health counters, including `frame_drops`) and a `raw_bind` row (`file`,
`offset`, `len`, `sha256`, `n_reports`, `bytes`, `record_bytes`, `layout`). Then `sha256 = SHA-256(JCS([records...]))`
and `head = SHA-256(JCS({prev, seq, sha256}))` from 64 x `0`, the `inputs.rs` rule. So each slice's byte range and
exact SHA-256 are inside the chain, and the `chunk` row repeats `raw_file`, `raw_offset`, `raw_len`, and `raw_sha256`.
A failed write cuts the file back to its last durable length and the head does not move.

Empty windows still include the trailing `hb` and `raw_bind` rows, so they hash `JCS([hb, raw_bind])` (`n = 2`),
not the SCOPE-hand-timeline §2.2 empty-window sentinel `SHA-256(JCS([]))`. That is deliberate: the health and
raw-bind rows stay in the chain even when no reports or frames arrived.

Frame arrivals travel from the capture thread over a bounded channel (`try_send`, cap 32 batches). Capture never
blocks on the logger. Drops are counted into the window's `hb.frame_drops`, the session total, the summary, and
`GET /status` as `hand.frame_drops`.

## Checkpoint binding (no seal.rs / bundle.rs change)

Checkpoints are written by `engine.rs` (`checkpoint_with`), and their `qorcap` object already allows extra keys
(`frames_hash = SHA-256(JCS(qorcap))`):

- every segment checkpoint: `hand_chunk_seq`, `hand_head` (newest durable window at the segment close; `null`
  before the first window is sealed);
- the start and stop checkpoints: `hand_timeline_dir` (bundle-relative pointer or off-bundle path),
  `hand_timeline_dir_actual`, `hand_dir_source`, `hand_dir_fallback_reason`, `hand_timeline_file`, `hand_raw_file`;
- the stop checkpoint also binds: `hand_timeline_sha256` (whole file), `hand_timeline_head`,
  `hand_timeline_chunks`, `raw_usb`, `hand_raw_bytes` (or `hand_timeline_error`);
- an off session (`--no-hand-timeline`): the start and stop checkpoints carry `hand_timeline: false` only. No
  file, no `hand_chunk_seq` / `hand_head` / sha keys.

`qorcap check-hand-timeline DIR` (and `check-response-bound DIR`) resolves `hand_timeline_dir` from `checkpoints.jsonl`
when `hand_timeline.v0.jsonl` is stored off-bundle (or uses `DIR` directly if present), enforces `raw_layout: "v1"`
(`80 B` records), checks non-decreasing `t_us` in `hand_raw.v0.bin`, enforces contiguous non-overlapping offsets and
no truncation, verifies each slice's SHA-256 and each `in` row's `raw_sha256`, and compares the checkpoint keys.
`verify-bundle` is unchanged.

## Devices

- `054c:0df2` DualSense Edge: primary. `FN_L/R`, `BACK_L/R` (`edge_bits`) and `profile_swap_candidate` are MEASURED.
- `054c:0ce6` DualSense: accepted and labelled `device_model: "dualsense"`, `fallback: true` in the header, status
  rows, every `in`, `hb` and `chunk` row. Edge-only fields are `UNMEASURED`. The FN/BACK bits are cleared out of
  `buttons` on this fallback and must be treated as UNMEASURED, not as measured-off. Read `edge_bits` (which is
  the string `"UNMEASURED"` on DualSense), not those bit positions in `buttons`. Never relabelled as an Edge.
- Anything else fails closed. Bluetooth (BTHENUM path or 78 B reports) is `rejected_transport`. A failed
  `GENERIC_READ` open of a gated pad is `open_denied`.

## Listen-only

- Enumeration: SetupAPI over the HID class GUID only, Sony paths only, query-only handles (access 0) for
  attributes, caps and the serial string.
- Capture: `CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING, FILE_FLAG_OVERLAPPED)`
  and overlapped `ReadFile` of input reports. No write access is requested, so the OS refuses any write on that
  handle. No output or feature report, no firmware or settings access, no audio API.
- Interface number note: on a wired DualSense / DualSense Edge the HID gamepad collection is USB interface 3, so its
  HID path contains `MI_03`; the audio functions are interfaces 0-2 and are not HID-class devices. The gate is the
  HID class GUID plus UsagePage 0x01 / Usage 0x05 / 64 B input reports, so audio is never enumerated or opened.
- `tests/hand_listen_only.rs`: denylist scan of `src/hand/` code (includes `LoadLibrary`, `GetProcAddress`, and
  `hid_write`), extern allowlist, the single `CreateFileW` call's access mask, a one-method `ReportSource`, and
  (Windows CI) an OS refusal of a write on a handle from `open_path`. That OS test opens a regular file, not a
  HID handle, and does not call `HidD_Set*`. There is no PE import-table check.


## Privacy (SCOPE-edge-logger §7.2)

`pad_id` (`serial:…` or a Windows device-instance path) is written into the local JSONL (header and status /
connected rows) so a single session can tell pads apart. It contains hardware serial numbers and local OS
paths. Before any share, upload to QorStream, public match page, or model-sharing export, `pad_id` must be
stripped or redacted, following SCOPE-edge-logger §7.2 (`qor_receipt/hands/sidecar.py::sanitize`). These files
stay on the local PC unless the operator copies them.

## What stays UNMEASURED / UNKNOWN

- Real-device report rate on MyTurn and raw disk use per hour: UNMEASURED (needs a separately approved device check).
- `Instant`-vs-QPC residual between `t_us` and `qpc_ticks` inside `hand_raw.v0.bin`: UNMEASURED until benchmarked.
- Adaptive-trigger resistance state: UNMEASURED (`trigger_fx_status` keeps bytes 42/43 raw, `status: UNMEASURED`).
- Edge profile, dead zones, curves, remap table, stick modules, patch firmware: UNKNOWN (feature reports, never read).
- Which profile a `profile_swap_candidate` selected: UNKNOWN (the candidate itself is ESTIMATED).
- Capture pipeline latency in `frame` rows: UNMEASURED. Without `--frame-clock` there are no `frame` rows and
  frame alignment is UNMEASURED.
- `seq_breaks` / `hw_stalls` are diagnostic only, not packet-loss evidence on their own.
- Who held the controller: not established by this log.
