# Challenge result: pattern mismatch is a failed result

Issue [#93](https://github.com/ConWan30/qorcap/issues/93). Match `match-20261024-135645`, QorCap v0.3.32, production submission `f26ac3935d038288`.

`challenge_results.jsonl` recorded the kickoff challenge as `ok: false`, `reason: pattern_mismatch` (prompt `OPTIONS,TRIANGLE,UP,DOWN,RIGHT`; entry `OPTIONS,UP,TRIANGLE,UP,DOWN,RIGHT`). The signed `receipt.json` stored `result: null`. Local verify then reported `home:challenge_unanswered:kickoff` (a partial) instead of `challenge_failed` (an error, FAIL-class). The production referee refused the submission. The two sides disagreed on severity.

`contracts/` is unchanged. `receipt.v0` already has a place for this failure. No sidecar and no new field.

## Where the receipt challenge entry is built

Two writers, in order.

The Rust engine drafts every challenge with `result: null`. It does not score the pattern. `draft_receipt` in `src/evidence.rs` copies the prompt fields and sets the result to JSON null:

```197:199:src/evidence.rs
    let ch: Vec<Value> = challenges.iter().map(|c| json!({
        "id": c["id"], "role": c["role"], "phase": c["phase"], "nonce": c["nonce"], "pattern": c["pattern"],
        "issued_ms": c["issued_ms"], "deadline_ms": c["deadline_ms"], "result": Value::Null })).collect();
```

`Engine` writes that draft, then Start Match and `finalize` both call `sign_and_report`, which calls `fill_challenge_results` before `receipt.json` is signed. The signed receipt is the Python recheck, not the Rust placeholder. `receipt.draft.json` can still show null. That file is the placeholder.

The recheck lives in `sidecar/qorcap_challenge.py` `challenge_results`. It runs QorTime `verify_response` (same reasons as the pin: `no_pause_press`, `pattern_mismatch`, `pause_not_seen_on_video`, `video_before_input`, `video_latency_too_high`, and `ok`). It always writes the full recheck to `challenge_results.jsonl` through `local`. Before this change the receipt result was kept only when `ok` was true:

```python
return {"result": res if res["ok"] else None, "local": local}
```

`fill_challenge_results` in `sidecar/qorcap_match.py` copies that object onto the challenge when it is not null, and otherwise leaves the draft's null in place. A pattern mismatch is `ok: false`, so the receipt kept null while the jsonl line kept `pattern_mismatch`. That is the whole bug. The console then said "not passed" for both a wrong pattern and a challenge nobody answered.

`receipt_result` returns a pass, or `ok` false with `reason` `pattern_mismatch`. `challenge_results` then drops that mismatch unless `mismatch_log_trustworthy` says the controller log can support it. `fill_challenge_results` copies a non-null result and leaves null alone. A stored failure is printed as `FAILED`. A withheld one is printed as not passed, and the jsonl line carries `withheld: controller_log`.

QorCap does not call QorTime `review.local.challenge_results` on this path. That function in the pin has the same drop (`result` only when `ok`). The live writer is `qorcap_challenge.challenge_results`.

## What `receipt.v0` already allows

`contracts/receipt.v0.json` (byte-identical to QorTime `schema/receipt.v0.json` at pin `0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2`). The challenge object is closed. Its `result` is null or an object whose required fields are `ok` and `reason`. There is no `failed` / `mismatch` enum. `ok` is a boolean, so `false` is in the schema. `reason` is a free string, so `pattern_mismatch` is in the schema.

```80:97:contracts/receipt.v0.json
    "challenges": {
      "type": "array",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["id", "role", "phase", "nonce", "pattern", "issued_ms", "deadline_ms", "result"],
        "properties": {
          "id": {"type": "string"}, "role": {"$ref": "#/$defs/role"}, "phase": {"enum": ["kickoff", "halftime", "end"]},
          "nonce": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
          "pattern": {"type": "array", "minItems": 2, "items": {"type": "string"}},
          "issued_ms": {"$ref": "#/$defs/ms"}, "deadline_ms": {"$ref": "#/$defs/ms"},
          "issuer": {"type": "object", "description": "Signed qortime-challenge/v1 token from the official referee (review receipts)."},
          "result": {"oneOf": [{"type": "null"}, {"type": "object", "required": ["ok", "reason"],
                     "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"},
                                    "pause_press_ms": {"type": ["integer", "null"]}, "done_ms": {"type": ["integer", "null"]},
                                    "latency_ms": {"type": ["integer", "null"]}, "hid_sha256": {"$ref": "#/$defs/hex64"},
                                    "rule": {"description": "Measurement rule. Absent or 1 keeps the 0..600 ms segment_ranges meaning. 2 is the fragment-PTS rule (0..2500 ms, ±300 ms consistency), stamped by the referee that measured it.", "enum": [1, 2]}}}]}
        }
      }
```

The result object does not set `additionalProperties` to false. Passes already add `reverified_by: "recorder"` inside that object (QorTime contract fixture `v0310_challenge_result_null` does the same). A failed result uses the same object shape with `ok: false` and `reason: "pattern_mismatch"`. `pause_press_ms` is the answering OPTIONS time; `done_ms` and `latency_ms` are null, which the schema allows. No sibling field is added on the challenge, because that object is closed.

`contracts/compat.json` `challenge_rules.recorder_result` still says anything other than an ok result stays null. That sentence is not updated here. `contracts/` stays frozen.

## How the pinned verifier classifies each result

Pin in `sidecar/qorcap_match.py` and `src/clip.rs`: `QORTIME_PIN = 0204e6d8082f8c40ea1fe5e09bf16402aaee3ff2`.

Local verify is `python -m qor_receipt.cli verify`, which calls `verify_receipt`. The public page is `verify/verify.js` in the same pin. They match.

Python `qor_receipt/receipt.py` lines 435–439:

```python
res = c.get("result")
if res is None:
    partial.append(f"{role}:challenge_unanswered:{ph}")
elif not res.get("ok"):
    errors.append(f"{role}:{ph}:challenge_failed:{res.get('reason')}")
```

Page `verify/verify.js` lines 439–440:

```javascript
if (c.result == null) partial.push(`${role}:challenge_unanswered:${ph}`);
else if (!c.result.ok) errors.push(`${role}:${ph}:challenge_failed:${c.result.reason}`);
```

The following branch (`ok` true) does not decide failed vs unanswered. It checks evidence (`hid_sha256`, timing, a live checkpoint) and may add partials such as `challenge_latency_out_of_range`. A failed object never enters that branch.

Status, same file, lines 470–477: any entry in `errors` forces `FAIL`. Partials alone do not.

```python
if errors:
    status = FAIL
elif not trusted_ref:
    status = SELF_ATTESTED
elif partial:
    status = PARTIAL
else:
    status = VERIFIED
```

| Stored `result` | Code | List | Status effect |
|---|---|---|---|
| `null` | `{role}:challenge_unanswered:{phase}` | `partial` | No error from this challenge. A local referee stays `SELF_ATTESTED`. |
| object, `ok: false`, `reason: pattern_mismatch` | `{role}:{phase}:challenge_failed:pattern_mismatch` | `errors` | `FAIL` |
| object, `ok: false`, any other reason | `{role}:{phase}:challenge_failed:{reason}` | `errors` | `FAIL` |
| object, `ok: true` | evidence partials, or nothing | `partial` | Not a challenge failure |

`pattern_not_from_nonce` and `pattern_too_short` are separate errors about the prompt, not about `result`. They are unchanged.

## Decision

Phase 2, in this change. The existing `result` object expresses the failure. No new contract field. No hashed sidecar.

A stored mismatch is a player failure. A recorder or controller glitch is not.

`receipt_result` returns a pass, and it returns `pattern_mismatch`. It does not return any other reason. `video_before_input` and `video_latency_too_high` stay null even though the verifier would put them in `errors` if they were stored. `no_pause_press`, `pause_not_seen_on_video`, and malformed timing stay null too.

## Final rule

Store `pattern_mismatch` (`ok` false, `reverified_by` `recorder`) only when the controller log can support it. Every non-empty line of `inputs.jsonl` must parse as a JSON object. A JSON value that is not an object, such as `42` or `[]`, is malformed, and so is a second `header` row. Chunk rows must cover `[issued_ms, deadline_ms]` with no gap larger than 3000 ms. That bound is the 2 s fMP4 fragment (`min_frag_duration`) plus 1 s of slack for the one-second GOP. A single chunk after the deadline does not cover the window, and neither does a crash gap followed by rows after the deadline. Wall time is the header `wall_anchor_ms` plus `t_us // 1000`. The pad's status must be `connected` at `issued_ms` and remain `connected` through `deadline_ms + 2500` ms. That 2500 ms is the stall-detection lag: a `stalled` row is written only after 2 s with no HID reports, on the next 100 ms read timeout. A `stalled`, `unplugged`, or `missing` row anywhere in that span withholds the result, including a drop that reconnects before the deadline. A status row whose `t_us` is not an integer, or whose `state` is not a string, withholds it too. OPTIONS alone, and OPTIONS inside the window with the rest of the pattern after the deadline, are `pattern_mismatch` from `verify_response` and are stored when the log passes this gate. Every other non-ok reason stays null. A withheld mismatch stays in `challenge_results.jsonl` as `reason: pattern_mismatch` with `withheld: "controller_log"`. That file is bundle metadata, not a contract file, and `withheld` is not in `receipt.v0`.

QorTime #80 should copy that paragraph into `compat.json` `recorder_result` and `review/local.py`. Do not re-vendor `compat.json`, `SHA256SUMS`, or `CONTRACTS_PIN` until that PR has merged. `Cargo.toml` stays `0.3.33`. The bump to `0.3.34` is the release PR.

A stored mismatch makes `qortime verify` status `FAIL`. `EXIT_CODES["FAIL"]` is 2, and `sign_and_report` returns that. Start Match offers the submit prompt only when `code != 2`, so a stored mismatch skips the prompt and `--submit`. Submit for Verification still works later: the **Submit for Verification** launcher, or `qorcap submit`.

A hung HID read writes no status row, so a silent gap can still be signed as a mismatch. That is [#99](https://github.com/ConWan30/qorcap/issues/99): commit a per-chunk report count in Rust, then the gate can require reports through the window.
