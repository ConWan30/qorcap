# `hand_raw.v0.bin` & `hand_timeline.v0.jsonl` Binary + Contract Spec

- **Status:** Frozen contract specification (`qorcap-hand-timeline/v0`, `raw_layout: "v1"`), matching current `origin/main` writer/checker without modifying runtime behavior.
- **Claim Ceiling:** `skill-proof` / `fair-proof` framing only. Records bind host-observed wired USB HID `0x01` input reports and MPEG-TS video PES arrivals onto one monotonic clock; never establishes who held the controller.

---

## 1. Binary Layout (`hand_raw.v0.bin`, `raw_layout: "v1"`)

| Property | Value |
|---|---|
| File name | `hand_raw.v0.bin` |
| File header | **`0` bytes** (`raw_file_header_bytes: 0`, headerless append-only file starting at byte offset `0`) |
| Record size | **`80` bytes** (`raw_record_bytes: 80`) |
| Endianness | Little-endian (`u64_le`) for numeric prefixes |

Each fixed `80`-byte record has the exact byte layout below:

| Byte Offset | Length | Field | Meaning |
|---:|---:|---|---|
| `0..8` | `8` | `qpc_ticks` (`u64_le`) | Raw Windows `QueryPerformanceCounter` tick count (`0` = `UNMEASURED` on non-Windows hosts or when unavailable) |
| `8..16` | `8` | `t_us` (`u64_le`) | Engine monotonic `Instant` timestamp in microseconds (`same` clock domain as `inputs.jsonl` `t_us` and `frame_clock.jsonl` `t_mono_us`) |
| `16..80` | `64` | `usb_report` (`[u8; 64]`) | Raw wired USB HID Input Report `0x01` (`usb_report[0] == 0x01`) |

- **Self-description rule:** `v1` binary files have zero magic bytes or version header. Readers and verifiers MUST take `raw_layout`, `raw_record_bytes`, and `raw_file_header_bytes` from the sealed `hand_timeline.v0.jsonl` `header` line and slice boundaries (`file`, `offset`, `len`, `sha256`) from each sealed `raw_bind` / `chunk` row.
- **Analog trigger travel:** Remains `hid.rs` bytes `5` (`L2`, `0..255`) and `6` (`R2`, `0..255`).
- **`TODO` on bytes `42–43` (`bytes_42_43_raw` / `trigger_fx_status`):** `src/hand/timeline.rs:413` currently names `raw[42]` as `l2_raw` and `raw[43]` as `r2_raw`, whereas `T-0006` (`match-20261121-104657`) saw byte `42` vary on R2 trigger pulls. L2-vs-R2 mapping of bytes `42–43` remains explicitly **`UNKNOWN`** until Con's listen-only Firing Range A-B-A bench settles polarity. No frozen fixture asserts which byte corresponds to L2 vs R2.

---

## 2. Crash-Tail & Slice Rules

1. **Unsealed trailing bytes in `hand_raw.v0.bin` (`len > expected_raw_offset`):** Treated as a **warning** (an `UNMEASURED` mid-record / unsealed crash tail that is never counted and never padded); `check-hand-timeline` still returns **PASS** if all sealed slices and JCS chunks verify cleanly.
2. **Shortened / truncated sealed slice (`offset + len > file_len`, slice SHA mismatch, offset gap/overlap, or modulus mismatch):** Treated as an **error** (`FAIL`).
3. **Unsealed trailing records in `hand_timeline.v0.jsonl`:** Records after the final `chunk` line emit a warning and are not counted as sealed.

---

## 3. Hardware Interface (`ERRATA-1-mi03.md`) & Practice-Off / LF Rules

1. **`MI_03` is the HID Gamepad Collection:** On wired DualSense (`054C:0CE6`) and DualSense Edge (`054C:0DF2`), USB interface `3` (`MI_03` in the HID device interface path) is the 64-byte HID gamepad top-level collection (`UsagePage = 0x01`, `Usage = 0x05`). Audio lives on interfaces `0–2` (USB Audio class). Enumeration gates on HID interface GUID + UsagePage/Usage/64 B input length, never rejects `MI_03`, and never opens audio endpoints.
2. **Practice-off / flag-off (`hand_timeline: false`):** Outside practice, or when `--no-hand-timeline` / `QORCAP_HAND_TIMELINE=0` is set, no `hand_timeline.v0.jsonl` or `hand_raw.v0.bin` is written and checkpoints MUST contain **zero** hand chain keys (`hand_chunk_seq`, `hand_head`, `hand_timeline_sha256`, `hand_timeline_head`, `hand_timeline_file`, `hand_raw_file`), carrying only `hand_timeline: false`.
3. **LF line endings (`\n`, no `\r`):** Every `hand_timeline.v0.jsonl` line MUST be RFC 8785 JCS canonical JSON terminated by a single `\n` (`0x0A`, zero `\r`). Windows CRLF checkouts or writes alter file SHA-256 and fail closed.

---

## 4. Verifier Behavior When Hand Chunks Are Absent (Documentation Only)

- **`qorcap check-hand-timeline` (`src/hand/check.rs::check_dir`):** When neither `hand_timeline.v0.jsonl` nor `checkpoints.jsonl` `"hand_timeline": false` is present, `check_dir` returns an explicit error (`hand_timeline.v0.jsonl: not found`); an off session (`"hand_timeline": false` with zero hand keys) returns `PASS` (`off: true`).
- **QorStream browser verifier (`web/public/chaincheck.mjs`):** Currently lists `"hand-chunks"` and `"hand-head"` in `OPTIONAL_UNKNOWN_IDS` (`chaincheck.mjs:426`), so absent or unverified hand chunks remain marked `UNKNOWN` without failing an otherwise-intact local video/frame chain. (Documentation note only; neither verifier is changed here.)
