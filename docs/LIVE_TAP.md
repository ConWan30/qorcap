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

## End-of-session cue (`end_of_session`)

Once per run, just before the tap closes, QorCap sends one more timed ID3 cue so a receiver can end the
stream at once instead of waiting out an idle grace. When: on the stop path, after the recording is
finalised (no media slice can follow), the tap worker sends every queued slice, then the last partial media
datagram, then this cue, then closes the socket. Nothing is sent on the tap after it. It is sent at most once.

Wire format (same carriage as the frame cues):

- TS PID `0x1E0`, the same timed-ID3 stream (`stream_type 0x15`) the rewritten PMT already announces; the
  continuity counter continues from the frame cues.
- One `private_stream_1` PES (stream_id `0xBD`, PTS only). PTS = the PTS of the last frame the tap sent
  (`0` if it sent none).
- ID3v2.4 tag with one UTF-8 (`0x03`) `TXXX` frame. **Description `qorweave_end`** (not `qorweave`), value =
  one JSON object.
- Packed into 188-byte TS packets and 1316-byte datagrams (a short last datagram is possible); the cue's
  datagrams are sent after the last media datagram, never mixed into it.

Old receivers that only read `TXXX` description `qorweave` skip this cue, so their chain checks and frame
line-up are unchanged (checked against QorStream's `server/live_weave.js` `WeaveReader`: chain-clean, the
end cue is not counted).

JSON fields:

| field | meaning |
|---|---|
| `type` | `"end_of_session"` |
| `v` | `"qorweave/v0"` |
| `n` | next cue number (last frame cue `n` + 1) |
| `sid` | session id, same as the frame cues |
| `file`, `f`, `pts` | part file, frame index and PTS of the **last recorded frame the tap actually sent** (handed to the socket; a refused datagram is in `send_errors`). Frames in dropped slices are not "sent". `null` when no frame was sent |
| `t` | QorCap monotonic µs when the stop path asked for the cue (same `Instant` as cue `t` and `inputs.jsonl t_us`) |
| `w` | press clock ms, `wall_anchor_ms + t/1000` |
| `reason` | stop-reason enum, below |
| `stop_reason` | QorCap's own first stop reason, unchanged (`enter`, `post_final_tail`, `final`, `pipe_broken`, ...), or `null` |
| `counts` | final tap counters just before the cue: `frames`, `cues` (frame cues), `skipped_cue_slots`, `skipped_frames`, `queue_drops`, `queue_dropped_packets`, `send_errors`, `send_dropped_packets`, `send_would_block`, `datagrams`. Slices dropped at the very end are counted here too. The cue's own datagrams are not in `counts` |
| `info` | fixed text: information only, not a score, final or result |
| `ph`, `h` | last frame cue's `h`, and this cue's hash: first 16 hex of sha256 of `qorweave/v0\|end_of_session\|n\|sid\|file\|f\|pts\|t\|w\|reason\|stop_reason\|ph` (a `null` value is the empty string). `counts` and `info` are not in the hash |

Example:

```json
{"type":"end_of_session","v":"qorweave/v0","n":5,"sid":"1700000000000","file":"video.mp4","f":9,"pts":14400,"t":9000000,"w":1700000009000,"reason":"user_stop","stop_reason":"enter","counts":{"frames":10,"cues":5,"skipped_cue_slots":0,"skipped_frames":0,"queue_drops":0,"queue_dropped_packets":0,"send_errors":0,"send_dropped_packets":0,"send_would_block":0,"datagrams":3},"info":"end of the practice live tap; information only, not a score, final or result","ph":"4cd9a7583f180702","h":"2df63db0c5bbb481"}
```

`reason` is mapped only from values QorCap already has. Anything it cannot tell apart is `unknown`:

| `reason` | when |
|---|---|
| `auto_stop_victory` / `auto_stop_defeat` | stop reason `final` or `post_final_tail` **and** the practice CoD end-stop watcher's `/shutdown` note for that same stop reads `practice autoclose; cod end screen VICTORY` / `DEFEAT` |
| `user_stop` | `enter` (Enter/Q in Start Match), `shutdown` (`/shutdown` with no reason), `ctrl_c`, `ctrl_break` |
| `error` | `pipe_broken`, `disk_full`, `capture_error`, `source` (recorder left on its own) |
| `unknown` | everything else: a `final` / `post_final_tail` without the CoD word (e.g. a Madden end-menu stop), `duration`, `launcher_gone`, `console_close` (also logoff / OS shutdown), no reason, any new value |

The VICTORY/DEFEAT word is kept in memory for this cue only. It is not written to `bundle.json`,
`receipt.draft.json` or the receipt. The cue does not say who won and is not a final.

Best effort: the stop path waits at most 300 ms (`END_WAIT_MAX`) for the cue to be queued, sent and
acknowledged. A send failure or a stuck tap worker is logged (`live tap: end_of_session cue ...`) and the stop,
seal, countersign and bundle continue as before. `/status` `live_tap.end_cue` is `sent` or `send_failed`.
`qorcap.log` gets a final `live tap summary (end_of_session): ...` line.

`live_weave.jsonl` gets the same JSON as its last line (the same string that went out), so a compare of
the log against the stream matches cue for cue. A reader of that file that expects only frame cues should
skip lines whose `type` is `header` or `end_of_session`.

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
- Nothing is sent to the controller. Nothing leaves the machine. The end-of-session cue goes only to the
  same loopback target, only when the tap is on.
- REAL MATCH builds are not changed by this file; the tap is not in v0.3.38.

Live Weave is timing line-up context (skill-proof / fair-proof). It is not a referee check and it does not
say who holds the controller.

## UNMEASURED

- Glass-to-glass delay with the real card and HLS on MyTurn.
- CPU cost of the tap at 1080p60 / 8 Mbps on MyTurn (the tap only copies bytes; ESTIMATED small).
- UDP loss on Windows loopback under load (counted in `send_errors` on the QorCap side only; receive-side
  loss shows as cue chain gaps on the page).
