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

The receipt value is now chosen by `receipt_result`: a pass, or `ok` false with `reason` `pattern_mismatch`. `fill_challenge_results` still copies a non-null result and still leaves null alone. A stored failure is printed as `FAILED`, not `PASSED`.

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

The emitter stores a recheck in the receipt in two cases:

- `ok: true` — a pass, as before (`reverified_by: recorder`, no `rule` field).
- `ok: false` and `reason == "pattern_mismatch"` — a wrong pattern. Verify reports `challenge_failed`.

Every other recheck stays `result: null` (`no_pause_press`, `pause_not_seen_on_video`, `video_before_input`, `video_latency_too_high`, malformed timing). The verifier would score those as `challenge_failed` too if they were stored, because it only looks at `ok`. Leaving them null keeps a missed press as `challenge_unanswered`, which is the rule the pass tests already lock and the frozen `compat.json` sentence describes. The bug in #93 is the wrong answer, which was logged and then thrown away.

The jsonl line is still the full recheck for every challenge, pass or miss.
