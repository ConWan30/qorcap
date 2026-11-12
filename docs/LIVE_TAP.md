# Live tap with Live Weave (practice lane, default off)

QorCap needs the capture card to itself, so OBS cannot stream while QorCap records. The live tap lets
QorStream show the match live without a second reader on the card:

- QorCap stays the **only** reader of the capture card. The capture command is unchanged.
- The tap sends **the same MPEG-TS bytes that reached the recording** (no re-encode) as UDP datagrams to a
  **loopback** port, e.g. `udp://127.0.0.1:1937`, where QorStream listens.
- Every N recorded frames (default 15) the tap adds one **Live Weave cue**: a timed ID3 tag
  (TS `stream_type 0x15`, PID `0x1E0`, `private_stream_1` PES, PTS = that frame's PTS, ID3v2.4 `TXXX`
  with description `qorweave`). The cue is JSON, schema `qorweave/v0`:

| field | meaning |
|---|---|
| `n` | cue number in this run |
| `sid` | session id (the input logger's wall anchor, same value the session seal uses) |
| `file`, `f` | recording part file and frame index in it (same rule as `frame_clock.jsonl`) |
| `pts` | the frame's 90 kHz PTS |
| `t` | QorCap monotonic µs when the frame's first TS packet was read (same `Instant` as `inputs.jsonl t_us`) |
| `w` | press clock ms (`wall_anchor_ms + t/1000`), the clock `recent_presses[].t_ms` uses |
| `fps`, `every` | CFR rate and cue spacing |
| `seal`, `sh` | session seal status (`waiting`/`locked`/`failed`/`off`) and the first 12 hex of its hash |
| `pad` | controller logger state (`connected`, `missing`, `off`, ...) |
| `lp` | newest controller press `{s: seq, t: press-clock ms, b: button}` or null |
| `ph`, `h` | previous cue hash and this cue's hash: first 16 hex of sha256 of `qorweave/v0\|n\|sid\|file\|f\|pts\|t\|w\|seal\|lp.s\|ph` |

The cues are also written to `live_weave.jsonl` in the bundle so a page or a later check can compare what
went out live against what QorCap logged.

## End cue (`kind: "end"`)

Shape agreed with QorStream `docs/STREAM_END.md` (ConWan30/qorstream PR #21, merge commit `65a34e3`).
Once per run, just before the tap closes, QorCap sends an end cue so QorStream can end the stream at once
instead of waiting out its restart grace. When: on the stop path, after the recording is finalised (no media
slice can follow), the tap worker sends every queued slice, then the last partial media datagram, then the end
cue, then closes the socket. Nothing is sent on the tap after it.

Wire format (the same carriage as every cue):

- TS PID `0x1E0`, the timed-ID3 stream (`stream_type 0x15`) the rewritten PMT already announces.
- One `private_stream_1` PES (stream_id `0xBD`, PTS only) per copy. PTS = the PTS of the last frame the tap
  sent (`0` if it sent none).
- ID3v2.4 tag, one UTF-8 (`0x03`) `TXXX` frame, description **`qorweave`**, value = the JSON below.
- **Sent 3 times, about 50 ms apart.** The JSON is byte-identical in all 3 copies, so dedupe on `n` + `sid`
  (QorStream takes the first). Each copy is a fresh PES, so the ID3 PID's continuity counter keeps counting.
  Packed into 188-byte TS packets and 1316-byte datagrams, sent after the last media datagram.

JSON, keys in this order, nothing else:

| field | meaning |
|---|---|
| `v` | `"qorweave/v0"` |
| `kind` | `"end"` (the marker) |
| `n` | last cue `n` + 1 |
| `sid` | session id, same as the frame cues |
| `file`, `f`, `pts` | part file, frame index and PTS of the **last recorded frame the tap actually sent** (handed to the socket). Frames in dropped tap slices are not "sent". `null` when no frame was sent |
| `t` | QorCap monotonic µs when the stop path cut the cue (same `Instant` as cue `t` and `inputs.jsonl t_us`) |
| `w` | press clock ms at that moment, `wall_anchor_ms + t/1000` |
| `stop_reason` | QorCap's own first stop reason, unchanged and short ASCII: `post_final_tail`, `final`, `enter`, `shutdown`, `ctrl_c`, `ctrl_break`, `pipe_broken`, `disk_full`, `capture_error`, `source`, `duration`, `launcher_gone`, `console_close`; `null` if none |
| `ph` | `h` of the last cue |
| `h` | optional per STREAM_END.md: first 16 hex of sha256 of `qorweave/v0\|end\|n\|sid\|file\|f\|pts\|t\|w\|stop_reason\|ph` (a `null` value is the empty string) |

Example:

```json
{"v":"qorweave/v0","kind":"end","n":5,"sid":"1700000000000","file":"video.mp4","f":9,"pts":14400,"t":9000000,"w":1700000009000,"stop_reason":"post_final_tail","ph":"4cd9a7583f180702","h":"<16 hex>"}
```

It is **not a chain cue**. It does not advance the cue count and nothing links on from it, so cue counts
and the weave compare stay equal. A receiver that does not know `kind` would read it as a frame cue with a
bad hash. QorStream from `65a34e3` on detects it first (`server/stream_end.js` `detectEndCue`). Older
QorStream builds would count it as one bad hash, so update QorStream before you run a QorCap build with this cue.

`stop_reason` is the stop reason QorCap already has. The CoD practice auto stop shows as `post_final_tail`
(or `final`). Whether it was VICTORY or DEFEAT stays in `cod_end_stop.json` and the `stop note:` line in
qorcap.log, and is not on the tap. The cue is information only. It is not a score, a final or a result,
and it does not say who won.

Best effort: the stop path waits at most 300 ms (`END_WAIT_MAX`) for the cue to be queued and sent (3 copies)
and for the worker to confirm. A send failure or a stuck tap worker is logged (`live tap: end cue ...`), and the
stop, seal, countersign and bundle continue as before. `/status` `live_tap.end_cue` is `sent` (at least one full
copy) or `send_failed`, and `end_cue_copies` counts the copies. The final tap counters (skipped cue slots,
including slices dropped at the very end, plus queue drops and send errors) go to qorcap.log as
`live tap summary (end_of_session): ...`, not on the wire.

`live_weave.jsonl` gets one **non-chain end record** as its last line: the exact JSON that went out (written
once, not three times). A reader of that file that wants only chain cues skips lines with `type: "header"`
or `kind: "end"`.

## Turning it on

```
set QORCAP_LIVE_TAP=udp://127.0.0.1:1937
set QORCAP_LANE=practice
```

or `qorcap record ... --live-tap udp://127.0.0.1:1937 --practice-lane [--live-tap-every 15]`.

Refused (logged, tap stays off) when the lane is not practice, when the target is not `udp://` or not a
loopback address, or when it has no port. `/status` gains a `live_tap` object (frames, cues, datagrams,
send errors, queue drops, skipped frames and cue slots, dropped TS packets, `pkt_size`, `sndbuf_bytes`) only
while the tap is on.

## Counters in qorcap.log

Every 30 s while the tap is on, and once when the recording ends (`live tap summary (...)`), qorcap.log
gets one line: frames, cues, `skipped_cue_slots` (and their share of all cue slots), `skipped_frames`,
`queue_drops` (slices and TS packets), `send_errors` (datagrams and TS packets, and how many were
WouldBlock), datagrams sent, `pkt_size` and the UDP send buffer the OS reports. A 30 s line with new
skips or drops is a WARN with the deltas. These are tap counters only; dshow drops stay in the ffmpeg
lines.

## What it does not change

- The recording, `manifest.jsonl`, the checkpoint chain, `receipt.draft.json`, `receipt.v0` and `contracts/`
  are untouched. `verify-bundle` passes with the tap on (box end-to-end test).
- Sending is non-blocking UDP on loopback. If nothing listens, datagrams are dropped and counted.
  Datagrams are 1316 bytes (7 TS packets, `pkt_size=1316`); a partial tail waits up to 50 ms for the
  next slice. The tap asks for an 8 MB socket send buffer and logs what the OS grants.
- The capture thread never does tap work itself (since the v0.3.43 stall fix). It copies the recorded
  slice to a small queue (32 slices, about 3 MB at most; 8 before practice match-20261111-183311
  skipped 15.6% of cue slots) and a tap worker sends the UDP and writes `live_weave.jsonl`. If the
  worker falls behind, tap slices are dropped and counted in `queue_drops`; the recording is not affected
  and later cues keep the recording's frame index. The cue's controller/seal fields come from a snapshot
  the input log keeps, so the capture thread never waits on the input-log mutex.
- Nothing is sent to the controller. Nothing leaves the machine. The end cue goes only to the
  same loopback target, only when the tap is on.
- REAL MATCH builds are not changed by this file; the tap is not in v0.3.38.

Live Weave is timing line-up context (skill-proof / fair-proof). It is not a referee check and it does not
say who holds the controller.

## UNMEASURED

- Glass-to-glass delay with the real card and HLS on MyTurn.
- CPU cost of the tap at 1080p60 / 8 Mbps on MyTurn (the tap only copies bytes; ESTIMATED small).
- UDP loss on Windows loopback under load (counted in `send_errors` on the QorCap side only; receive-side
  loss shows as cue chain gaps on the page).
