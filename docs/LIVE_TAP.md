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
- Nothing is sent to the controller. Nothing leaves the machine.
- REAL MATCH builds are not changed by this file; the tap is not in v0.3.38.

Live Weave is timing line-up context (skill-proof / fair-proof). It is not a referee check and it does not
say who holds the controller.

## UNMEASURED

- Glass-to-glass delay with the real card and HLS on MyTurn.
- CPU cost of the tap at 1080p60 / 8 Mbps on MyTurn (the tap only copies bytes; ESTIMATED small).
- UDP loss on Windows loopback under load (counted in `send_errors` on the QorCap side only; receive-side
  loss shows as cue chain gaps on the page).
