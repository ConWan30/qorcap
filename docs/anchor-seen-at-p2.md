P2 SCHEMA r2 CANDIDATE — NOT FROZEN — PENDING QorData acceptance then QorProof PASS/freeze — no model code

Private engineering schema candidate. Not a freeze, not schema PASS, not implementation acceptance, not permission to write model code. Feature `practice-anchor-transaction-model` is not added by this document and stays absent. No Rust, tests, or Cargo change belongs to this candidate.

# P2 schema r2 candidate — anchor seen-at offline model

This file is the sole allowlisted P2 schema document for the first-run schema gate. Digests, byte lengths, and vector counts are not stated here. Each required vector is named by a stable ID. digest: see p2-vectors.json. That file is not produced in this turn.

## 0. Provenance

`P2-SCHEMAS-r1-candidate.md` is a recipient-assembled chat-transfer copy, preserved separately from this candidate. It is not the missing writer-saved original, it is not authenticated by transfer markers, and it is an open proposal. Placeholders for vectors and pins in that copy stay unexpanded. Prospective "produced" or "created" wording in that copy is not execution evidence. This r2 candidate does not import any unsaved earlier worker proposal as schema.

Supplied pins below are copied from the architect handoff source table. They identify local bytes as supplied to this writer. They are not a fresh observation by this writer, not repository truth, not consent, and not a runtime result. This writer did not re-hash those files in this turn.

| Source (file name only) | SHA256 (supplied pin) | Bytes (supplied pin) |
|---|---|---|
| `P2-SCHEMAS-r1-candidate.md` | `21e9cc222d5a30ec6fa8fff1d1f0b69e29a121589560fc656a7963fc44159910` | 63841 |
| `P2-SCHEMAS-r1-qordata-review.md` | `ed189d630682ac72458374cfef34f296f0943f36159422b77aa148246b2575dd` | 27712 |
| `p2-schemas-r1-redteam.md` | `df2a60e8e28029ea4fcf8a9900ee383a0dc09e9acc6b7aa6b9ad57c03a9070df` | 28865 |
| `P2-BRIEF-QORARCH.md` | `13ed0a76d386abe683d8c2cf36b215dd62c946f92daa3e856e5e287f2fca3389` | 28276 |
| `P2-NOVELTY.md` | `57d5b42d551cf7a008e91859452109079701e0c41414f45e55372f2618f22bb1` | 17767 |
| `PHASE2-DESIGN-r2.md` | `d4bf5fd56ca1770c0bbe723014dd4c00a4b5972645592b91d3b1c33f00ca1fd4` | 68899 |
| `PHASE2-DESIGN-r3-addendum.md` | `d4a2a28396fa2d2c1ea729f093c9b4f10d792296b54803e7c519ecfc3d3535ad` | 10789 |
| `ACCEPTANCE-AND-BUDGETS.md` | `b6818017965c2ff9a21d246d4f7fb7764eaf1eab53d7ec498bda63762ddafdd3` | 30596 |
| `redteam-qorproof-r3.md` | `f3f021933a5f21bdce1ed67da21f3da5ab2577d905dc61365082245f4592f90f` | 12044 |
| `redteam-qorproof-r2.md` | `8ad7b50473ef3e511c3a7ab03bed55928eadf06f7cfa1db497566314e4446f33` | 19104 |
| Handoff `P2-GROK-HANDOFF.md` | `741d3aa11d849046e5620c1e9d13f2b5bbd2c94567e873418e869ea3e2d49ce6` | supplied in the task prompt; byte length not restated because this writer did not measure the file |

Repository context, supplied and not re-verified by this writer: ConWan30/qorcap; required base `4d119020cec8c3b6ae55c30c8bb17f5c6ab845ad` (operator-supplied pin for this job; the handoff itself reported no fresh git-object verification). Supplied tree pin `9fe2160a9dea08b9a501a55eb21612469be10398`. Supplied parent pin `f007728a3f2b83b47e4a49b73a99b1b2d285da9b`. Older feature head `9abea1e33c406b1ca412832ad4f2b7764b4247d1` is a different commit and must not substitute for the base. Branch name in the governing brief: `anchor/track1-phase2-p2-model-20261107`. If main moves, stop for an approved repin. No silent rebase.

Precedence used here: latest Data and Proof schema reviews correct r1; `PHASE2-DESIGN-r3-addendum.md` overrides `PHASE2-DESIGN-r2.md` on holder release and publication timing. Normal holder release is DISABLED. Terminal-positive stays BLOCKED. This candidate does not self-certify PASS or freeze.

## 1. Scope, non-goals, rails

In scope for the whole r2 candidate, of which this turn closes §2–§4 only: exact offline schema for a pure owned-memory model of auxiliary evidence. States are only `unknown`, `seen_at_late`, and `seen_at_retained`. There is no pass state. Match 201029 stays VOID. Practice does not submit to a referee, upload capture, or write practice gold. Controller output stays read-only: no HID, rumble, or lightbar writes. Grip stays off for practice. Rumble, if described at all, is information-only. `receipt.v0` and `contracts/` stay frozen. REAL MATCH stays v0.3.38 and unwired. P1 stays unchanged and default-off.

Non-goals: model or test code; feature wiring; network, keys, deploy, hosting; live finals; a current-policy service; collection; exporter; compact transparency-proof codec; normal holder unlock; publication-time freshness. The r3 rule stands: a future coordinator, which is not P2, qualifies as of admission time. Publication-time freshness is not asserted. Unbounded downstream pause is possible. If a later policy needs currentness at publication, that final or pilot stays BLOCKED.

Stable image means simulated model-durable bytes, not database, WAL, fsync, filesystem, or power-loss assurance. Synthetic auth, signer, and entropy roots are in-process fixture assumptions, not real security, key, or randomness claims. Decoders yield untrusted candidates. A public hash tag is not authentication. N1 and N2 are engineering priorities built from established mechanisms, not novelty, exclusivity, or patent conclusions.

Wording is skill-proof and fair-proof only. Operational timing, capacity, durability, and cost are UNMEASURED. Numbers that later appear as fixture inputs are not observations and not defaults. Unset applicable limits deny.

## 2. Encoding and closed type grammar

Carried forward from r1 §2, with the corrections in §2.1. Framing functions stay the r1 functions so E, J, F, and Raw are not redefined.

All names are proposed model names. Every top-level typed record `T` encodes as `E(T,x)={"payload":x,"schema":"proposed.aux.model.p2.r1."+T}`. The payload has exactly the fields declared for `T`, all required. Nested records omit `E` unless a field is explicitly typed bytes. `T?` means a required field whose value is either `T` or JSON null. It never means an absent field. No undeclared member, extensible map, free text, URL, or path exists. Union tags are closed ASCII enums. Inactive alternatives are null, not omitted. This is a new format. Existing v1 absent-option rules do not change.

Canonical `J` is RFC 8785 restricted to ASCII member names and ASCII string values, arrays, objects, and null. No JSON numbers or booleans. Sort object keys lexicographically. Minimal JSON escaping. UTF-8. No BOM, whitespace, or newline. Reject duplicate keys before typed decoding. Reject any input whose exact bytes differ from `J` of the decoded value. Reject unknown or missing fields, alternate escapes, malformed UTF-8, non-ASCII, invalid null, and wrong schema. Parser byte, depth, token, list, and string limits apply before allocation or hashing and again after canonicalization. No unbounded parse-then-check. Concrete numeric caps are §5 B3 and §6 B4, not this section. The r1 tiny fixture constants are not adopted as defaults.

Primitives:

- `U`: canonical unsigned decimal string, range `0` through `18446744073709551615`, no sign, no leading zero.
- `I`: canonical signed decimal covering the i64 minimum `-9223372036854775808`, no leading plus, no negative zero, no leading zeros. A parser that rejects that minimum is nonconformant. This sentence does not claim such a parser was executed.
- `Id`: exactly 32 lowercase hex characters.
- `H`: exactly 64 lowercase hex characters.
- `Bytes`: canonical unpadded base64url. Decode then re-encode must match. Both decoded and encoded lengths are bounded. Empty is allowed only where a field explicitly allows an empty buffer.
- `Z`: 64 zero hex characters. Reserved predecessor placeholder, not a proof and not a valid positive original id. Every other digest recomputes from its declared preimage.
- `List<T>`: ordered array, bounded by its declared cap. Set-valued lists are strictly sorted by `J(T)` and unique. An ordered chronology is never sorted in a way that hides reordering.
- `Purpose`: `opening` | `interval` | `close`.
- `State`: `unused` | `consumed` | `invalidated`.
- `HoldState`: `held` | `quarantined` | `terminal`.
- `Outcome`: `published` | `aborted`.
- `Boundary`: `unknown` | `terminal_known`. `terminal_known` does not mean earlier events were eligible.
- `Op`: `owner_grant` | `issue` | `consume` | `archive_read` | `snapshot` | `page` | `checkpoint` | `observe` | `hold` | `terminal` | `policy_update` | `recover`.
- `ClientOp`: `issue` | `consume` only. This is not `Op`. It is the only operation tag allowed inside `ClientExpectedCore`.

Framing, unchanged from r1:

- `F(d,b) = BE64(len(UTF8(d))) || UTF8(d) || BE64(len(b)) || b`.
- `D(T) = "QOR-AUX-P2-MODEL-R1/" + T`.
- `Hash(T,x) = SHA-256(F(D(T), J(E(T,x))))`, written as lowercase hex.
- `Raw(b) = SHA-256(b)`, lowercase hex. Typed `Hash` and `Raw` are different functions. Do not interchange them.
- Binary tree node and empty payloads are raw bytes only where §7 of r1 specified them. Those tree rules are not re-closed in this turn (§5–§9). Lengths are checked as `u64` before framing. No truncation or saturation.

`ModelSigned<T> = {alg:"synthetic_hash_only", payload:E(T,x), payload_id:H, role:RoleRef, tag:H}` with `payload_id = Hash(T,x)` and `tag = Hash(ModelTag, {alg, payload_id, role})`. The wrapper encodes as `E("ModelSigned", wrapper)`. The nested payload is already `E`. The tag is a public deterministic framing check under a trusted simulation. It is not a cryptographic signature and not authentication. No key material, host status flag, or normal-build signer exists in this schema.

`RoleRef = {issuer:Id, kind:RoleKind, generation:U, policy:H}`.

`RoleKind`, corrected in r2 because r1's set cannot name observation, hold, or recovery without overloading another kind: `challenge` | `ack` | `checkpoint` | `owner` | `terminal` | `snapshot` | `policy` | `observe` | `hold` | `recover`. Mapping of kind to record is §4. Reuse of an older kind for those three records is rejected, not optional.

Hash dependency rule carried forward: no record's preimage contains its own id, tag, response checksum, entry digest, tree root, image checksum, or page root. Manifest and page recursion stays forbidden as in r1 §7. The expected-id cycle is removed in §3, not patched with a placeholder.

### 2.1 Corrections relative to r1 §2–§3

- Delete the type `ClientExpected`. Delete the field `original_request_hash` everywhere. It is not a core field, not a request field, and not a hidden preimage.
- One time-field spelling: `enqueue_at`. The spelling `enqueue` is rejected, including as an alias.
- `ClientExpectedCore` is the only request-independent client declaration. Its domain is `Hash("ClientExpectedCore", core)` under the framing above. Fields are closed in §3.
- `RoleKind` gains `observe`, `hold`, and `recover` as distinct kinds. r1 kinds remain for the records that already had them.
- Schema string prefix and `D(T)` prefix stay the r1 strings. The candidate revision is r2; the framing label is not forked. A new type name is a new `T` under that same framing.

Records carried forward unchanged in name, except where a later closed section replaces a field, include `Scope`, `RegistryScope`, `Pins`, `ClockDomain`, `Mono`, `Wall`, `SourceBound`, `Range`, `CaptureRange`, `JournalRef`, `ParentBoundary`, and the request and record types named in §4. Their r1 field lists remain in force until a later section states a delta. Checkpoint identity (client commitment versus service receipt) is not closed here; it is §8 B6. Do not treat r1 `JournalRef.checkpoint` as already solved.

Clock-role pairs carried forward: `server_mono` with `model_counter` and `ns`; `client_mono` with `client_declared` and `ns`; `capture_declared` with `capture_declared` and `ns`; `wall_audit` with `wall_declared` and `unix_us`. Every `process`, `boot`, `clock_epoch`, and `leader` stays explicit. No cross-role subtraction. Same-role elapsed requires equality of every domain field and nondecreasing samples, checked arithmetic only. `Wall` is audit, not a timing authority.

`Range` rules carried forward: `first <= last`; checked `last+1`; checked `last-first+1 = count`; predecessor null if and only if `first = 0`, and then `predecessor_head = Z`, otherwise `predecessor+1 = first`. An endpoint equal to the `U` maximum rejects even if a one-row count could fit. Count equal to the `U` maximum is representable for `[0, MAX-1]`. That representation is not by itself a valid first close (close rule remains the r3 nonempty split: opening `[0, MAX-2]` then close `[MAX-1, MAX-1]`, `total_rows` MAX). Full close grammar stays with later sections; this sentence only stops the invalid reading of r1.

## 3. B1 — constructible binding and immutable original history

### 3.1 ClientExpectedCore

`ClientExpectedCore` is a synthetic client declaration, not a server fact. The server sees an opaque `expected_id`. A hash does not establish enqueue, arrival, durable resolution, or acquisition. The trusted client fixture checks core, id, operation, and link equality. The server cannot decode client times from the id.

```text
ClientExpectedCore={
  scope:Scope,
  stream:Id,
  exchange:Id,
  operation:ClientOp,
  purpose:Purpose,
  ack_ordinal:U,
  pins:Pins,
  enqueue_at:Mono,
  original_deadline:Mono
}
```

Closed. No other field. In particular, absent: `original_request_hash`, `expected_id`, any request or response byte or hash, any tag, any ledger or page root, any usage counter, `owner_generation`, `prior_ack`, `client_nonce`, idempotency, seal, challenge bytes.

`expected_id = Hash("ClientExpectedCore", core)`. The preimage is `J(E("ClientExpectedCore", core))` inside `F`, not a partial projection and not `Raw` of a request. No omitted-id form. No zero placeholder. `Z` is not a valid `expected_id`.

Both times are `ClientMono`: `domain.role = client_mono`, `domain.source = client_declared`, `domain.unit = ns`, and every other `ClockDomain` field of `enqueue_at` equals the same field of `original_deadline`. Checked `original_deadline.value >= enqueue_at.value`. Failure of domain equality or of that order is rejection `domain` before any attempt is stored. It is not classified `late`.

The core is owned by the client fixture before the first attempt and is immutable. A later sample, later deadline, later policy, or later budget cannot replace `enqueue_at`, `original_deadline`, `pins`, `purpose`, `ack_ordinal`, `exchange`, `operation`, `stream`, or `scope`.

### 3.2 Where owner generation, prior ack, and client nonce live

Data's core list and Proof's core list do not include `owner_generation`, `prior_ack`, or `client_nonce`. Those values stay request-body bindings. They are not folded into the core, which would still be non-recursive but would contradict the closed field list. They are also not dropped: each committed logical operation retains them by exact original request bytes, outside the core preimage.

- Issue: `owner_generation`, `prior_ack`, and `client_nonce` are the r1 `IssueRequest` fields. `IssueRequest.expected_id` is the issue core's `expected_id` and is inside the full canonical issue request.
- Consume: `owner_generation`, `prior_ack`, and `client_nonce` are the r1 `Commitment` fields inside `ConsumeRequest`. `Commitment.expected_id` is the consume core's `expected_id`, not the issue core's id. It is inside the full canonical consume request.
- The registry projection may store those three values only as a pure function of the retained request bytes. On restore, recompute them from those bytes. If the projection disagrees with the bytes, the client fixture is `unavailable` for that key. Do not repair the bytes from the projection, and do not repair the projection by inventing bytes.

This placement is the schema choice offered for exact review. It follows the review field lists rather than an unsaved alternate core.

### 3.3 Full request hash

For issue, request type is `IssueRequest`. `request_hash = Raw(J(E("IssueRequest", payload)))`. The payload includes `expected_id` and every r1 issue field. For consume, request type is `ConsumeRequest`. `request_hash = Raw(J(E("ConsumeRequest", payload)))`. The payload includes `commitment.expected_id` and every r1 consume field. Never hash a copy with `expected_id` removed, nulled, or replaced by `Z`.

Construction order, which is acyclic:

1. Build `ClientExpectedCore` with no request bytes.
2. `expected_id = Hash("ClientExpectedCore", core)`.
3. Build the full canonical request containing that `expected_id`.
4. `request_hash = Raw` of those exact request bytes.
5. Only then may a challenge or ack be derived. Challenge and ack preimages follow r1 and must not be written back into the core.

There is no direct or transitive edge from the core to a request hash, tag, response hash, or current root.

### 3.4 First-writer logical-operation registry

The registry is client-fixture state plus the service's retained original bytes. It is keyed by stable context, not by content hash alone.

```text
LogicalOpKey={scope:Scope, stream:Id, exchange:Id, operation:ClientOp}
```

`operation` is part of the key, so issue and consume are different keys even when scope, stream, and exchange match.

```text
LogicalOpRecord={
  key:LogicalOpKey,
  core:ClientExpectedCore,
  expected_id:H,
  request_type:IssueRequest|ConsumeRequest,
  request_bytes:Bytes,
  request_hash:H,
  owner_generation:U,
  prior_ack:H?,
  client_nonce:H
}
```

`request_type` must match `key.operation` (`IssueRequest` iff `issue`, `ConsumeRequest` iff `consume`). `expected_id` must equal `Hash("ClientExpectedCore", core)`. `request_hash` must equal `Raw(request_bytes)`, and those bytes must decode as the declared request type and carry the same `expected_id`, scope, stream, operation, and the projected owner fields. `core` fields `scope`, `stream`, `exchange`, and `operation` must equal `key`.

First committed writer for a key wins. The record is immutable. Registration happens before dispatch of that attempt and is durable across restore when the client fixture is present.

Collision and conflict, total:

| Situation | Outcome |
|---|---|
| No record for key, request matches its own core and hash | First writer. Record stored. |
| Record exists, request bytes identical, raw identical, core identical | Not a conflict. Retry oracle below. |
| Same key, different core or different `expected_id` | `conflict`. First record stands. The new id does not become the logical original. |
| Same key, same core and `expected_id`, different request bytes or different raw | `conflict`. Loser does not inherit the winner response. |
| `expected_id` presented with a key other than the key implied by its core | `binding`. Reject. |
| `expected_id` not equal to hash of the accompanying core | `binding`. Reject. |
| Two racers, different bytes, same key, either commit order | One winner. The other is `conflict` after the winner commits, including when the loser is retried after expiry or restart. |
| Two racers, identical bytes, same key | One commitment. The retry returns the winner's original bytes. No second advancement. |

Concurrent registration is decided by the same commit that retains the original, not by a side map that can diverge from `Original`.

### 3.5 Issue and consume are not aliases

An exchange group is `(scope, stream, exchange)`. It may contain at most one issue record and one consume record. Those records have different `ClientOp`, different cores, different `expected_id` values, and their own `enqueue_at` and `original_deadline`. The schema does not require the two ids to be equal. Presenting the issue `expected_id` inside `ConsumeRequest`, or the consume id inside `IssueRequest`, is `binding` even if every hash is well formed. A consume is not another issue attempt.

Issue linkage for a later consume is through retained issue request bytes, issue `request_hash`, and challenge bytes, as r1 already chains them. That link is not equality of `expected_id`.

A genuinely new exchange id is a new group under ordinary stream and owner rules. It does not delete or rewrite a prior group's late, missing, or broken history.

### 3.6 Retry oracle

Retained verbatim:

> identical retries of the winning committed canonical request return its exact original bytes; divergent losing requests remain conflicts and never inherit the winner's success

Apply before expiry, after expiry, and after restart, and apply both race orders. Require `Raw` equality and byte equality. Attempt number, later clock samples, and re-signing do not create a new original. A dedicated archive read may disclose the winner as historical bytes. It must not report that a divergent loser succeeded. Archive is not a second consume.

Same consumed challenge, same idempotency id, or same occupied ordinal, with a changed body, remains `conflict`.

### 3.7 Client originals, negatives, and restore

Persist and reconcile, across restore, the logical-op records, the exact request bytes, and every negative classification (`ClientAttempt` and `ClientNegative` as in r1, with `expected_id` pointing at a core id). Append-only. First late, missing, or broken classification stays sticky.

If the client fixture's original core, link, or classification history is missing, the state stays `unknown` / missing. Do not rebuild a client original from a genuine server `Original`. Do not assign a new core and favorable times because an old ack is readable. Readback of server bytes does not fill arrival, durable resolution, or clear late or broken.

Later `enqueue_at` or `original_deadline` values are a different core. Under the same logical key they conflict. They never replace the stored `original_deadline`.

Server requests disclose only the opaque `expected_id`, not the core's client times.

### 3.8 Required vectors

Outcomes are schema outcomes, not measured test runs. digest: see p2-vectors.json.

Positive twins:

| ID | Required outcome |
|---|---|
| `V-B1-DAG-01` | Issue core, then `expected_id`, then full `IssueRequest` including that id, then `Raw`, then challenge. No field of the core depends on the request, tag, or root. |
| `V-B1-DAG-02` | Consume core with `operation=consume`, distinct id, full `ConsumeRequest`, `Raw`, ack path. Issue id and consume id differ. Exchange group shared. |
| `V-B1-TWIN-RETRY` | Byte-identical winning request after expiry and after restart returns the same original response bytes. No new tag, time, or ack advance. |
| `V-B1-TWIN-NEWEX` | A new exchange id with its own core does not alter the previous key's failure classification. |

Attack traces from Proof §1, plus archive-after-loss:

| ID | Required outcome |
|---|---|
| `V-B1-ATK-01` | Core was late. A recomputed core with a later `enqueue_at` or `original_deadline` gets a new id and still conflicts on the same logical key. Hashes of the new core verify and still lose. |
| `V-B1-ATK-02` | Reuse of an issue `expected_id` as a consume `expected_id`, or a check that demands the two ids be equal, is `binding`. |
| `V-B1-ATK-03` | `expected_id` fixed, body changed (seal, owner generation, idempotency, challenge bytes, or other request field). `Raw` changes. Retry is `conflict`, not winner success. |
| `V-B1-ATK-04` | Client fixture dropped. A genuine old ack is readable. A new favorable core must not clear `unknown`. Server byte integrity is not client arrival. |
| `V-B1-ATK-05` | Archive after client loss may return the winner's historical bytes under current archive auth and must not mark a divergent loser successful and must not synthesize client timing. |

Field and nested-domain mutations. Each id is one mutation of an otherwise valid twin, and the valid twin is `V-B1-DAG-01` or `V-B1-DAG-02` as named. Expected rejection is `binding` if the id or registry link is stale, `conflict` if the same logical key is rebound, `domain` if clock domain or order fails. Not an unspecified decoder failure.

| ID | Mutation | Outcome |
|---|---|---|
| `V-B1-MUT-SCOPE-TENANT` | `scope.tenant` | new id; same key impossible (key includes scope) so this is a different key and must not replace the old key |
| `V-B1-MUT-SCOPE-PRINCIPAL` | `scope.principal` | same as tenant: different key, old record unchanged |
| `V-B1-MUT-SCOPE-SERVICE` | `scope.service` | different key, old unchanged |
| `V-B1-MUT-SCOPE-AUDIENCE` | `scope.audience` | different key, old unchanged |
| `V-B1-MUT-SCOPE-SESSION` | `scope.session` | different key, old unchanged |
| `V-B1-MUT-SCOPE-DEVICE` | `scope.device_epoch` | different key, old unchanged |
| `V-B1-MUT-SCOPE-CAPTURE` | `scope.capture_epoch` | different key, old unchanged |
| `V-B1-MUT-SCOPE-CHAIN` | `scope.chain` | different key, old unchanged |
| `V-B1-MUT-SCOPE-LINEAGE` | `scope.lineage` | different key, old unchanged |
| `V-B1-MUT-STREAM` | `stream` | different key, old unchanged |
| `V-B1-MUT-EXCHANGE` | `exchange` | different key; must not be treated as a retry of the old exchange |
| `V-B1-MUT-OP` | `operation` flipped issue/consume under the same exchange | different key; using the other operation's id on the wrong request type is `binding` (`V-B1-ATK-02`) |
| `V-B1-MUT-PURPOSE` | `purpose` | same logical key if scope, stream, exchange, operation unchanged: `conflict`; first core stands |
| `V-B1-MUT-ORD` | `ack_ordinal` | same key: `conflict` |
| `V-B1-MUT-PIN-PROTOCOL` | `pins.protocol` | same key: `conflict` |
| `V-B1-MUT-PIN-ELIG` | `pins.eligibility` | same key: `conflict` |
| `V-B1-MUT-PIN-TRUST` | `pins.trust` | same key: `conflict` |
| `V-B1-MUT-PIN-DATA` | `pins.data` | same key: `conflict` |
| `V-B1-MUT-PIN-CHECKER` | `pins.checker` | same key: `conflict` |
| `V-B1-MUT-ENQ-ROLE` | `enqueue_at.domain.role` not `client_mono` | `domain` |
| `V-B1-MUT-ENQ-SOURCE` | `enqueue_at.domain.source` not `client_declared` | `domain` |
| `V-B1-MUT-ENQ-UNIT` | `enqueue_at.domain.unit` not `ns` | `domain` |
| `V-B1-MUT-ENQ-PROCESS` | `enqueue_at.domain.process` differs from deadline | `domain` |
| `V-B1-MUT-ENQ-BOOT` | `enqueue_at.domain.boot` differs from deadline | `domain` |
| `V-B1-MUT-ENQ-EPOCH` | `enqueue_at.domain.clock_epoch` differs from deadline | `domain` |
| `V-B1-MUT-ENQ-LEADER` | `enqueue_at.domain.leader` differs from deadline | `domain` |
| `V-B1-MUT-ENQ-VALUE` | later `enqueue_at.value` on same key | `conflict`; stored deadline unchanged |
| `V-B1-MUT-DL-VALUE` | later `original_deadline.value` on same key | `conflict`; stored deadline unchanged |
| `V-B1-MUT-ORDER` | deadline value < enqueue value, same domain | `domain` |
| `V-B1-MUT-STALE-ID` | request carries an `expected_id` that is not the hash of the registered core | `binding` |
| `V-B1-MUT-OMIT-ID` | request hashed with `expected_id` omitted, null, or `Z`, then repaired | not a valid preimage; `binding` |
| `V-B1-RACE-AB` | order A commits, B divergent | A original wins; B `conflict` |
| `V-B1-RACE-BA` | order B commits, A divergent | B original wins; A `conflict` |
| `V-B1-RACE-AB-EXP` | same as `V-B1-RACE-AB` after expiry | oracle unchanged |
| `V-B1-RACE-BA-EXP` | same as `V-B1-RACE-BA` after expiry | oracle unchanged |
| `V-B1-RACE-AB-RST` | same as `V-B1-RACE-AB` after restart | oracle unchanged; missing client fixture stays `unknown` (`V-B1-ATK-04`) |
| `V-B1-RACE-BA-RST` | same as `V-B1-RACE-BA` after restart | oracle unchanged |

Scope, stream, and exchange mutations change the key, so they must not be implemented as in-place edits of the first record. Purpose, ordinal, pins, and times do not change the key and therefore cannot be "fixed" by publishing a new id.

## 4. B2 — total operation, response, role, auth, and event map

r1 request names are kept. No wrapper types are introduced around them. `policy_update` request bytes are `E(PolicyRecord)` as r1 stated, not a new request struct. `ReadOriginalRequest` is the r1 archive request and is not renamed.

r1 said every mutating operation is an `Original` plus a `GlobalEntry`. That blanket rule conflicts with `PolicyRecord` (registry scope, no lineage `Scope`, no idempotency id) and with internal `recover` (no caller request). The correction is an explicit class, not a fabricated scope or idempotency id.

Classes:

- `ledger`: one `Original` and one `GlobalEntry`. Caller request bytes exist. Idempotency is the request's `idempotency` field, scoped by `(scope, stream, operation)`.
- `side`: retained request and response bytes in the r1 snapshot side table (`SnapshotStored`), not a `GlobalEntry`, because a snapshot must not take a ledger self-reference. Idempotency is `SnapshotRequest.idempotency` when the request has that field.
- `read`: no new `Original`, no new idempotency id, no `GlobalEntry`. Response is previously retained bytes or a page derived from a stored manifest.
- `registry`: policy only. Not an `Original` and not a `GlobalEntry`. No synthesized idempotency id and no synthesized lineage. First-writer key is `(RegistryScope, generation)`.
- `internal`: no caller request bytes, no lineage, no idempotency id, no `Original`. Not a successful external response.

`GlobalEntry.scope` is a lineage `Scope`. `GlobalEntry.stream` is null only when the event's own record allows a null stream. None of the ledger ops below allow a null stream. Internal and registry events do not use `GlobalEntry` at all, so they do not invent a stream to satisfy that field.

Historical reads authorize against the current archive and historical policy (`PolicyRecord.archive` and `HistoricalRule`), not against the write authority that was current when the row was produced. Unknown historical cutoff is `policy_unknown`, never "all history". A retired role may be read only when current archive is `allow` and the matching historical decision is `allow`. `deny` and `unknown` do not qualify the read. Current write role is neither necessary nor sufficient for archive.

`RoleRef.policy` on a tag must equal the policy digest that authorized that commit. That digest must not be defined as a hash of a record that contains the digest or of a `RoleRef` that points at the same new policy. The exact policy preimage is §5 B3. Until that section is written, producers have no authority to fill `policy` by hashing `PolicyRecord` wholesale.

Holder release stays disabled. Rows that mention `terminal` define the negative and quarantine shape only. The positive terminal transition stays BLOCKED and is not a success path.

### 4.1 Caller operations

Auth columns: "requested" is the caller's presented `AuthBinding`. "required" is what the model checks now. `AuthBinding.stream` and `holder` and `owner_generation` follow the nullability in the row. A presented holder never substitutes for archive policy.

| Op | Request canonical | Result canonical | Requested auth | Required current auth | Stream | Owner gen | Holder | ModelSigned kind | Ref target | Original | Idempotency key | Event class | Counters touched (identity only; charge rules are §6) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `owner_grant` | `E(OwnerRequest)` | `ModelSigned<OwnerRecord>` | `Op=owner_grant`, scope match, stream set, holder set to the requested holder, owner gen = `expected_owner_generation` | current role kind `owner`, state `active`, same scope and stream; policy generation and digest match the retained minimum (minimum rules §5) | required | required; must match expected | required; bound by the synthetic issuer, not by principal id alone | `owner` on `OwnerRecord` | `Hash(OwnerRecord)` | yes | `OwnerRequest.idempotency` | ledger | `records`, `stored_bytes`, `store_calls`, `signer_calls`, denial and audit and fault counters on refusal |
| `issue` | `E(IssueRequest)` including `expected_id` | `ModelSigned<Challenge>` only after confirmed commit | `Op=issue`, scope, stream, owner gen, holder null | current owner role `active` for scope and stream; core registry agrees; caps set | required | required | null; non-null holder is `unauthorized` | `challenge` on `Challenge` | `Hash(Challenge)` | yes | `IssueRequest.idempotency` | ledger | `outstanding`, `inflight` or `concurrent` while prepared, `signer_calls`, `store_calls`, `records`, `stored_bytes`, retry counters if this is a retry, denial and audit and fault on refusal |
| `consume` | `E(ConsumeRequest)` including `commitment.expected_id` | `ModelSigned<AckCore>` only after confirmed commit | `Op=consume`, scope, stream, owner gen, holder null | current owner role `active`; challenge unused and unexpired at post-sign admission; fence and revision unchanged at commit; admission label only `post_sign_admission_only` | required | required | null | `ack` on `AckCore` | `Hash(AckCore)` | yes | `ConsumeRequest.idempotency` | ledger | same reservation family as issue, plus no second charge that resets retained retry cost (rule detail §6) |
| `archive_read` | `E(ReadOriginalRequest)` | stored `response_bytes` of the matching `Original`, not a new tag | `Op=archive_read`; `AuthBinding.scope` equals `ReadOriginalRequest.scope`; stream and owner gen and holder null | current `PolicyRecord.archive=allow` and historical decision `allow` for the target record's role through a resolvable cutoff; `request_hash` and `response_hash` and `operation` match one stored `Original`; ids alone do not authorize | null on the auth binding; the stored original's stream is whatever that original recorded | null on the auth binding | null; holder id is not read authority and not a substitute for archive | none minted; the historical object's existing kind is the one historical policy evaluates | the stored `Original.event` | no new original | none; do not invent one | read | `store_calls` or page counters only if a later accounting section assigns them; no stream advance; no consume |
| `snapshot` | `E(SnapshotRequest)` | `ModelSigned<Manifest>` after the side table commit | `Op=snapshot`, scope, stream null (the view covers the lineage, not one caller-chosen stream), holder null, owner gen null | current role kind `snapshot`, state `active`, for that scope; observer basis complete; archive rules do not turn the manifest into write authority | null | null | null | `snapshot` on `Manifest` | `Hash(Manifest)` | no `Original` row; bytes live in `SnapshotStored` | `SnapshotRequest.idempotency` | side | `pages`, `page_items`, `proof_hashes`, `signer_calls`, `store_calls`, `records`, `stored_bytes` |
| `page` | `E(PageRequest)` | `E(Page)` bytes already fixed by the manifest, no new tag | `Op=page`, scope equals the manifest scope, stream null, holder null, owner gen null | current archive `allow` for kind `snapshot` and the page's historical role; cursor and manifest id recompute; wrong scope is `unauthorized` without foreign bodies | null | null | null | none minted | none new; page id is `Hash(Page)` as an integrity id, not a new ledger `Ref` | no | none | read | `pages`, `page_items`, `proof_hashes`, `store_calls` |
| `checkpoint` | `E(CheckpointRequest)` | `ModelSigned<CheckpointRecord>` | `Op=checkpoint`, scope, stream, owner gen, holder null | current role kind `checkpoint`, `active` | required | required | null | `checkpoint` | `Hash(CheckpointRecord)` | yes | `CheckpointRequest.idempotency` | ledger | `records`, `stored_bytes`, `store_calls`, `signer_calls`, denial and audit and fault |
| `observe` | `E(Observation)` as the request (r1 name; the request is the `Observation` record) | `ModelSigned<ObservationRecord>` | `Op=observe`, scope, stream, owner gen, holder null | current role kind `observe`, `active`; not kind `checkpoint` | required | required | null | `observe` | `Hash(ObservationRecord)` | yes | `Observation.idempotency` | ledger | same counter family as checkpoint |
| `hold` | `E(HoldRequest)` | `ModelSigned<HoldRecord>` with `state=held` or, if uncertainty is committed, `quarantined` | `Op=hold`, scope, stream, owner gen, holder equal to `HoldRequest.holder` | current role kind `hold`, `active`, plus holder binding; principal match is not sufficient | required | required | required | `hold` | `Hash(HoldRecord)` | yes | `HoldRequest.idempotency` | ledger | `records`, `stored_bytes`, `store_calls`, `signer_calls`; outstanding hold is not a releasable reservation |
| `terminal` | `E(TerminalRequest)` | no success result in this candidate | `Op=terminal`, scope, stream, holder, owner gen | normal path: `retirement_disabled` before any CAS; holder, candidate, and revision checks still apply to the denial | required | required | required | kind `terminal` is reserved for a future reviewed oracle and is not produced by the normal path | none on the normal path | no success original; denial may append a bounded denial (§6) | `TerminalRequest.idempotency` is recorded only on a denial accounting row if §6 defines one; it is not a success key | internal denial, not a ledger success | denial and fault counters only; no release of `records` or holds |
| `policy_update` | `E(PolicyRecord)` | `ModelSigned<PolicyRecord>` | `Op=policy_update`; auth scope's registry fields must equal `PolicyRecord.registry`; lineage is not an input; stream null; holder null; owner gen null | current role kind `policy`, `active`, for that registry; new generation must be a checked successor of the current generation; same generation with any different digest is `fork` / `conflict` and not a writer | null | null | null | `policy` | `Hash` of the policy payload under the §5 preimage, not a self-hash | no | none | registry | `records`, `stored_bytes`, `signer_calls`, `store_calls` for the policy row only |
| `recover` | none | `RecoveryRecord` retained internally, optionally framed as `ModelSigned<RecoveryRecord>` with kind `recover` for image integrity only; not returned as a caller success | none; there is no caller | service continuity inside the model: new service epoch and strictly increased writer term; no grant while observer basis is unknown | none | none | none | `recover` if an internal tag is stored; the tag is not an external credential | `Hash(RecoveryRecord)` as an internal id | no | none | internal | `store_calls` for the image write; invalidate unused old-domain challenges without charging them as new issues |

`ReadOriginalRequest` shape, unchanged from r1 and normative for auth:

```text
ReadOriginalRequest={scope:Scope, operation:Op, request_hash:H, response_hash:H}
```

No idempotency field. No stream field. No holder field. Authorization uses current archive and historical policy as in the table row, plus equality with exactly one stored `Original`. A mismatch is `not_found` or `unauthorized` as follows: wrong current archive or historical decision is `unauthorized` and returns no body; unknown cutoff is `policy_unknown` and returns no body; hashes not present are `not_found` and return no body. Wrong `operation` with otherwise matching hashes is `not_found`, not a coercion into the stored operation. Foreign scope is `unauthorized` with no body.

`ObservationRecord`, `HoldRecord`, and `RecoveryRecord` role mapping is exactly the kind column above (`observe`, `hold`, `recover`). They are not `checkpoint`, `owner`, or `terminal`.

`PolicyRecord` keeps `RegistryScope` and does not gain a lineage `Scope` or an idempotency id. `Original` and `GlobalEntry` therefore do not apply to it. `GlobalEntry` is not given a fake scope so that policy can be forced into the ledger.

### 4.2 Internal transitions and read outcomes

These are not extra `Op` values and must not grow request bytes, lineage, or idempotency ids.

| Event | Request bytes | Result | Auth | Original / global | Notes |
|---|---|---|---|---|---|
| `issue_prepare` | the already registered issue request only | private challenge candidate | same as `issue` | not yet visible | no usable challenge bytes returned |
| `issue_commit` | none beyond the prepared issue | ledger issue result | re-check owner, fence, revision, term | becomes the issue `Original` | entropy failure yields no challenge |
| `receive` / `pre_sign` | the consume request already registered | private `AckCore` | same as `consume` | not published | not a consume |
| `post_sign` admission | none | `Admission` inside the consume candidate | re-check auth, policy, fence, owner; issuing domain; `issued <= post_sign < expires`; equality and later samples reject with no consume | `Admission` is non-null only on a committed consume | label remains admission-only; not freshness at commit |
| `stable_swap` | none | image replace or refuse | writer term and revision | no new original by itself | ambiguous swap withholds |
| `confirm` | none | confirmation or `commit_unknown` | none new | no fabricated success | loss of confirmation after a committed swap does not roll the image back |
| `reconcile` | none | one exact winning `Original` or `unavailable` | current archive if a winner is disclosed | no manufactured original | candidate tag, timeout, and absence are not success |
| unused-challenge invalidation | none | challenges in the old service or clock domain become `invalidated` | internal to `recover` | no new caller original | does not alter committed original bytes |
| snapshot materialize | the snapshot request | side-table manifest | as `snapshot` | not ledger | mixing pages across revisions is `fork` |
| page outcome | the page request | one page or refusal | as `page` | not ledger | omitted page is `incomplete`, not an empty success |
| archive outcome | the read request | historical bytes or refusal | as `archive_read` | not ledger | current archive policy, not the old writer role |
| policy outcome | `E(PolicyRecord)` | registry row or `fork` | as `policy_update` | not ledger | lower generation is `regression` |
| recover outcome | none | internal `RecoveryRecord` or withhold | no caller auth | not ledger | missing observer basis: no recovered grant |

Refusal of any row may append bounded denial, audit, or fault state only as §6 later specifies. Refusal never mints a positive `Ref`, never advances a stream, and never clears a prior `unknown`.

### 4.3 Required vectors

digest: see p2-vectors.json.

| ID | Required outcome |
|---|---|
| `V-B2-OP-OWNER` | Valid `owner_grant` twin commits one `Original`, one `GlobalEntry`, kind `owner`. |
| `V-B2-OP-ISSUE` | Valid issue twin; kind `challenge`; holder null. |
| `V-B2-OP-CONSUME` | Valid consume twin; kind `ack`; `Admission` present; label admission-only. |
| `V-B2-OP-ARCH` | `ReadOriginalRequest` returns winner bytes under current archive allow and a resolvable historical allow. |
| `V-B2-OP-ARCH-OLD` | Same bytes refused when current archive is deny or historical decision is not allow, even if the caller still has the old write role. No body. |
| `V-B2-OP-ARCH-CUT` | Unknown historical cutoff is `policy_unknown`, not full history. |
| `V-B2-OP-SNAP` | Snapshot twin is side-table only; no `GlobalEntry`; kind `snapshot`. |
| `V-B2-OP-PAGE` | Page twin; no new tag; no idempotency id. |
| `V-B2-OP-CKPT` | Checkpoint twin; kind `checkpoint`; ledger class. |
| `V-B2-OP-OBS` | Observation twin; kind `observe`, not `checkpoint`. |
| `V-B2-OP-HOLD` | Hold twin; kind `hold`; other principal's holder id refused. |
| `V-B2-OP-TERM` | Terminal request does not succeed; `retirement_disabled`; hold remains; positive path BLOCKED. |
| `V-B2-OP-POL` | Policy twin; registry class; no lineage field; no idempotency id; kind `policy`. |
| `V-B2-OP-POL-FORK` | Same generation, different digest, is `fork` and does not replace the row. |
| `V-B2-OP-RECOVER` | Recover has no request bytes and no `Original`; unused old-domain challenges invalidated; committed originals unchanged; missing observer basis grants nothing. |
| `V-B2-ROLE-OBS` | Tag kind `checkpoint` on an `ObservationRecord` rejected. |
| `V-B2-ROLE-HOLD` | Tag kind `owner` or `terminal` on a `HoldRecord` rejected. |
| `V-B2-ROLE-REC` | Treating `RecoveryRecord` as `Original` with a synthesized idempotency id rejected. |
| `V-B2-INT-01` | Each internal row in §4.2 leaves request-byte and idempotency fields absent. |
| `V-B2-NULL-HOLD` | Non-null holder on issue, consume, checkpoint, observe, snapshot, page, archive, or policy is `unauthorized`. |
| `V-B2-NULL-OWNER` | Null stream or null owner generation on owner, issue, consume, checkpoint, observe, hold, or terminal is `unauthorized` or `malformed` as a missing required field, not a global event. |

## 5. B3 — config, policy digests, minima, continuity

This section is the preimage for `RoleRef.policy` and for `StableCore.config_hash`. Neither digest is inside its own payload. No numeric cap below is a default. The r1 tiny-fixture integers are not adopted. Every bound is a mandatory field of the external config. If the external config fixture is absent, every op denies `limit_unset` and no grant is restored.

### 5.1 Policy core

r1 `RoleRule` embedded a `RoleRef`, and `RoleRef.policy` is an `H`. Putting that hash inside the same new policy that the hash names is a backedge. r2 deletes `RoleRule`. Roles inside a policy core have no policy hash.

```text
PolicyRole={issuer:Id, kind:RoleKind, generation:U, operation:Op,
            state:active|retired|revoked}
HistoricalRule={issuer:Id, kind:RoleKind, generation:U,
                records_through:H?, decision:allow|deny|unknown}
PolicyCore={registry:RegistryScope, generation:U, prior_digest:H?,
            roles:List<PolicyRole>, archive:allow|deny,
            historical:List<HistoricalRule>}
PolicyRecord={core:PolicyCore, digest:H}
```

`digest = Hash("PolicyCore", core)` under §2. The hashed bytes are `J(E("PolicyCore", core))` only. `PolicyRecord.digest` is outside that preimage. A `PolicyRecord` whose `digest` is not that hash is `schema` / `binding`. `PolicyCore` has no `digest` field.

`PolicyRole` and `HistoricalRule` must not contain a `policy` member. A `RoleRef.policy` on a `ModelSigned` tag points at an already computed `PolicyRecord.digest`. That tag is not a field of the `PolicyCore` being hashed. A historical rule may name an older digest only as `records_through` when §7 says that field is a checkpoint id, not as a nested `RoleRef.policy` of the new core. `records_through` admissibility is §7, not a second policy hash.

Refinement of §4: the `policy_update` canonical request is `J(E("PolicyCore", core))`, not `E(PolicyRecord)` and not a core with the digest filled in. The stored result is `PolicyRecord` plus `ModelSigned` over `PolicyCore` (payload id = the digest, kind `policy`). Same generation and a different core is `fork` and does not replace the first writer (§4). Lower generation than the external minimum is `regression`.

`prior_digest` null is not history and not a grant. It is admissible only on the single synthetic floor row that the external basis itself names. An image that arrives with `prior_digest` null and no external floor is `policy_unknown`.

### 5.2 Config core

Caps and charge identity live outside the image.

```text
LimitSet={
  request_input_bytes:U, canonical_bytes:U, decoded_bytes:U, parse_bytes:U,
  depth:U, tokens:U, string_bytes:U, list_items:U,
  output_bytes:U, workspace_bytes:U, recovery_image_bytes:U,
  records:U, stored_bytes:U, outstanding:U, inflight:U, concurrent:U,
  retry_attempts:U, pages:U, page_items:U, proof_hashes:U,
  signer_calls:U, store_calls:U, issue_lifetime_ns:U, operation_age_ns:U,
  denial_records:U, denial_bytes:U, fault_records:U, audit_records:U,
  calls:U, call_bytes:U, minima_entries:U, accounting_basis_bytes:U
}
ServerDomainPin={role:server_mono, source:model_counter, unit:ns,
                 process:Id, boot:Id, clock_epoch:Id, leader:Id}
ConfigCore={model_instance:Id, tenant:Id, limits:LimitSet,
            charge_profile:"b4-r2", server_domain:ServerDomainPin}
ConfigRecord={core:ConfigCore, digest:H}
```

`digest = Hash("ConfigCore", core)`. `ConfigRecord.digest` is not in the preimage. `charge_profile` is the closed literal `b4-r2`, meaning the charge and release rules in §6 and no other schedule. A different literal does not match this candidate. `ServerDomainPin.role`, `source`, and `unit` are fixed literals; the four ids are fixture inputs. `LimitSet` fields are required `U`, never `U?` and never JSON null. There is no "unlimited" null. A missing external `ConfigRecord` is unset, which denies. Values are not filled from `StableCore`.

`request_input_bytes` counts only the caller request buffer. `recovery_image_bytes` counts only a stable-image byte length. They are not aliases. `output_bytes`, `workspace_bytes`, and `parse_bytes` are the other separate ceilings. Supplied invalid example, not a measurement and not a cap adopted here: a recovery image of 61295 bytes was charged against a request-input ceiling of 16384 bytes. That pairing is nonconformant. This candidate does not raise `request_input_bytes` so that 61295 would pass, and it does not install either integer as a default. Vector `V-B3-IMG-SPLIT`: image length is compared only to `recovery_image_bytes`; the request-input ceiling stays an independent field.

`StableCore.config_hash` must equal the external `ConfigRecord.digest` before any grant. Recomputing the image self-hash after editing caps, charges, or `charge_profile` does not authorize the image. Self-hash detects corruption under the simulated trust root only.

### 5.3 Minima and accounting basis, outside the image

```text
MinimumKey={registry:RegistryScope, kind:RoleKind, scope_digest:H}
Minimum={key:MinimumKey, generation:U, digest:H, basis:Id}
ExternalBasis={config:ConfigRecord, minima:List<Minimum>,
               continuity:Continuity, accounting:Usage}
Continuity={service_epoch:Id, writer_term:U, server_domain:ServerDomainPin,
            config_digest:H}
```

`Usage` is the r1 counter record (all required `U`, extended in §6 with `calls` and `call_bytes`). `scope_digest` is `Hash("Scope", scope)` or `Hash("RegistryScope", registry)` as named on the key; it is not a hash of the image. `basis` is an id in the external fixture, not a row of the image being authorized.

Bounds, from the external config, applied to the basis itself:

- `minima` length ≤ `limits.minima_entries`. Over that length the basis is unusable (`unavailable`), not truncated.
- Canonical byte length of `J(E("ExternalBasis", basis))` ≤ `limits.accounting_basis_bytes`. The image's `recovery_image_bytes` cap does not authorize an oversized basis, and the basis cap does not authorize an oversized image.

The image is never a source of minima. Copying `StableCore.policies`, `config_hash`, or `observer_hash` into `Minimum.generation` or `Minimum.digest` is forbidden. A filtered or shortened minima list is missing basis, not a successful subset. Observer loss remains no recovered grant (§4 recover).

### 5.4 Restore checks

Order is fixed. First failure stops grants. Committed original bytes, consumed namespaces, `first_break`, and unresolved hold fences stay as they were; a failure here does not clear them.

1. External `ConfigRecord`, `ExternalBasis`, and observer basis are present and inside their own caps. Else `unavailable` / `policy_unknown`. No seed from the image.
2. `Continuity.config_digest` equals `ConfigRecord.digest` and `ServerDomainPin` equals `ConfigCore.server_domain`. Else `unavailable`.
3. Image `config_hash` equals that same digest. Else deny changed caps or charges (`conflict` / `fork`). Do not adopt the image's limits.
4. For every policy row the image still carries, a `Minimum` with the same registry, kind, and scope exists. Image generation lower than that minimum: `regression`. Same generation and different digest: `fork`. Missing minimum: `policy_unknown`.
5. Historical decision `unknown`, or a cutoff that §7 calls unknown: `policy_unknown`. Not all-history.
6. Image `writer_term` must equal `Continuity.writer_term`. Do not take the greater of the two. Disagreement: `commit_unknown`, no grant.
7. On an accepted restore the model then uses a fixture-supplied new `service_epoch` and checked `writer_term+1`. The new term is not read from the image. Unused challenges in the old service epoch or in a server domain that is not the current pin become `invalidated` with `invalidated_by` equal to the new service-epoch `Id` (§7). Consumed challenges and committed originals are unchanged. Client deadlines are not rewritten.
8. Image `usage` lower than external `accounting` on any cumulative field is undercharge: latch `unavailable`. Disposable reservations in the image (`inflight`, `concurrent`, `workspace` mirror) are dropped, not converted into grants (§6).

### 5.5 Deny table

| Condition | Outcome | Vector |
|---|---|---|
| External config absent | `limit_unset`, no grant | `V-B3-CFG-MISS` |
| Minima absent or shorter than the retained basis | `policy_unknown`, no grant | `V-B3-MIN-MISS` |
| Basis filtered (one minimum removed, image otherwise intact) | `policy_unknown` | `V-B3-MIN-FILT` |
| Image generation < minimum generation | `regression` | `V-B3-GEN-LOW` |
| Same generation, different policy digest | `fork` | `V-B3-GEN-FORK` |
| Image caps, weights, or `charge_profile` differ from external config | deny; self-hash irrelevant | `V-B3-CAP-CHG` |
| Unknown historical cutoff | `policy_unknown`, not all-history | `V-B3-CUT` |
| Image seeds its own minimum | `policy_unknown` | `V-B3-SEED` |
| `writer_term` or server domain disagrees with continuity | `commit_unknown`, no grant | `V-B3-TERM` |
| Accepted restore | unused old-domain challenges invalidated; originals kept | `V-B3-EPOCH` |
| Recovery image checked as `request_input_bytes` | nonconformant (the supplied 61295-versus-16384 pairing) | `V-B3-IMG-SPLIT` |

digest: see p2-vectors.json. No digest is written in this section.

## 6. B4 — units, scopes, charging, crash reconciliation

All figures that a later fixture puts in `LimitSet` are synthetic inputs. They are UNMEASURED. They are not live defaults. Unset means the external config is absent or a required `LimitSet` field is not a canonical `U`: deny `limit_unset`. Exhausted cumulative quota or uncertain accounting latches `status=unavailable` and `unknown`. That latch does not evict originals, does not reuse a consumed namespace, does not clear `first_break`, and does not unlock a hold. There is no admin, time, or quota release.

### 6.1 Two stocks

Cumulative retained charges commit only inside the atomic image swap and survive crash, expiry, and retry. They are: `records`, `stored_bytes`, `retry_attempts`, `signer_calls`, `store_calls`, `denial_records`, `denial_bytes`, `fault_records`, `audit_records`, `calls`, `call_bytes`, and `outstanding` while a challenge is still `unused`.

Disposable reservations never become history. They are: `inflight`, `concurrent`, `workspace_bytes`, and the attempt-local parse scratch counted against `parse_bytes`. Release them when the attempt ends, including refuse, and on any crash before the swap commits. Do not release them by deleting a committed row. A crash after the swap leaves cumulative charges in place and releases only reservations that were not converted.

`retry_attempts` increments once per retry class call and is never set back to zero at the start of an attempt. Winning-byte retries still pay this retained cost. They do not add a second `records` charge and do not advance the stream (§3 oracle).

`audit_records` and `fault_records` are counter-only. They are not a retained trace, log, or export. `denial_records` is the r1 `Denial` list, bounded by `denial_records` and `denial_bytes`. Append only. If another denial would exceed either cap, do not drop the earliest denial and do not drop `first_break`; latch `unavailable`.

`operation_age_ns` is a server-domain comparison from receive to the phase being checked. Client `enqueue_at` is not the start. `issue_lifetime_ns` is `expires_at - issued_at` in the issuing server domain, checked, and it is not refreshed on retry or restore.

### 6.2 Separate ceilings

| Ceiling | What it measures | What it must not measure |
|---|---|---|
| `request_input_bytes` | Caller request buffer length at receive | Recovery image, response, workspace |
| `canonical_bytes` | `J(E(...))` length of that request | Image |
| `decoded_bytes` | Decoded payload size | Image |
| `parse_bytes` | Parser scratch for this attempt | Retained store |
| `output_bytes` | Response bytes at delivery | Request input |
| `workspace_bytes` | Attempt scratch reservation | Committed originals |
| `recovery_image_bytes` | Stable-image length at recover | Caller request input |
| `stored_bytes` | Retained model bytes after commit | Per-attempt scratch |

The supplied invalid example (61295-byte image versus 16384-byte request-input ceiling) fails closed if it is run as a request-input charge. A conformant fixture gives the image its own `recovery_image_bytes` check and leaves `request_input_bytes` unchanged. This document does not choose a passing integer for either field.

Per-request, per-principal, per-tenant, and per-model stocks are all enforced. A principal must not be able to spend a tenant cap alone: each stock has its own counter. The tightest exhausted scope denies. `calls` and `call_bytes` exist because r1's tenant and model totals did not name the per-principal call and byte ceilings from the governing resource contract.

### 6.3 Phases and witnesses

Phases: receive, parse, auth, sign, prepare, atomic swap, confirmation, delivery. Cumulative charges appear in the image only at swap. Confirmation loss after a committed swap does not undo them. Delivery loss does not undo them. Ambiguity across swap and confirmation is `commit_unknown`: no provisional positive bytes, and if the accounting basis cannot be matched to exactly one image, latch `unavailable`.

```text
CommitWitness={before:H, after:H?, writer_term:U, revision:U,
               phase:prepare|swap|confirm|deliver,
               outcome:committed|refused|unknown}
```

`before` and `after` are image digests when an image exists (`after` null if no new image committed). The witness is external fixture state. It is not an idempotency id, not a caller request, and not a lineage. Restore must find a witness that agrees with the image bytes and the external term. Missing or contradictory witness: `commit_unknown`, no new grant. Crash before swap: image unchanged, reservations released, cumulative stocks unchanged, witness `refused` or absent. Crash after swap: image is the committed one, cumulative stocks stand, reservations are not still held, witness `committed`.

### 6.4 Metric × call rows

Call names: the twelve ops, plus `malformed`, `auth_denied`, `read`, `page_call`, and `retry`. `read` is the historical body return of `archive_read` (same stocks; listed so the read path cannot be omitted). `page_call` is the fetch path of `page`. `retry` is an additional stock on top of the retried op, not a thirteenth success path. `recover` has no caller request. `terminal` has no success commit (§4); only denial stocks apply.

Shared restore cell `EXT`: read the ceiling from the external config; cumulative stocks must be ≥ the external accounting basis; image-undercharge latches `unavailable`; do not reset retained counters; drop disposable reservations. `NA` means this call does not take that stock. The external ceiling must still exist, or the call is `limit_unset` rather than unlimited.

| Metric | Unit | Call | Scope | Reserve point | Charge / release | Reset | Restore |
|---|---|---|---|---|---|---|---|
| `request_input_bytes` | byte | `owner_grant`, `issue`, `consume`, `archive_read`, `snapshot`, `page`, `checkpoint`, `observe`, `hold`, `terminal`, `policy_update`, `malformed`, `auth_denied`, `read`, `page_call`, `retry` | per-request ceiling, also the same ceiling at principal, tenant, and model | receive, before parse | compare length; no cumulative charge; oversize denies with no commit | none per attempt; ceiling is config lifetime | EXT; never apply this ceiling to an image |
| `request_input_bytes` | byte | `recover` | none | none | forbidden. Image length is not request input | none | EXT |
| `canonical_bytes` | byte | every call except `recover` | per-request at principal, tenant, model | after canonical `J` | compare; no cumulative charge | none | EXT |
| `canonical_bytes` | byte | `recover` | none | none | NA | none | EXT |
| `decoded_bytes` | byte | every call except `recover` and `malformed` | per-request at principal, tenant, model | after decode | compare; malformed never decodes | none | EXT |
| `decoded_bytes` | byte | `malformed`, `recover` | none | none | NA | none | EXT |
| `parse_bytes` | byte | every call except `recover` | per-request disposable | parse | reserve scratch; release at attempt end or crash before swap; no retained charge | every attempt | EXT; drop reservation |
| `parse_bytes` | byte | `recover` | per-model disposable | image parse | reserve against `parse_bytes` separately from `recovery_image_bytes`; release at end | every attempt | EXT |
| `depth` | count | every call except `recover` | per-request | parse | compare nesting; exceed denies before hash | none | EXT |
| `depth` | count | `recover` | per-model | image parse | compare; do not use the request-input path | none | EXT |
| `tokens` | count | every call except `recover` | per-request | parse | compare | none | EXT |
| `tokens` | count | `recover` | per-model | image parse | compare | none | EXT |
| `string_bytes` | byte | every call except `recover` | per-request | parse | compare | none | EXT |
| `string_bytes` | byte | `recover` | per-model | image parse | compare | none | EXT |
| `list_items` | count | every call except `recover` | per-request | parse | compare | none | EXT |
| `list_items` | count | `recover` | per-model | image parse | compare | none | EXT |
| `output_bytes` | byte | `owner_grant`, `issue`, `consume`, `snapshot`, `checkpoint`, `observe`, `hold`, `policy_update`, `archive_read`, `read`, `page`, `page_call` | per-request at principal, tenant, model | delivery | compare response length; oversize withholds the body and does not roll back a committed image; denial is `oversize` | none | EXT |
| `output_bytes` | byte | `terminal`, `malformed`, `auth_denied`, `recover` | per-request | none | no success body. Local error enum is not an original response | none | EXT |
| `output_bytes` | byte | `retry` | per-request at principal, tenant, model | delivery | the retried winner's existing response is compared again; no second original | none | EXT |
| `workspace_bytes` | byte | every caller call including `malformed` after the buffer exists, `auth_denied`, `read`, `page_call`, `retry` | per-request disposable at principal, tenant, model | prepare, or parse if prepare is not reached | reserve; release on refuse, delivery, or crash before swap | every attempt | drop; do not keep scratch in the image |
| `workspace_bytes` | byte | `recover` | per-model disposable | prepare of restore | reserve; release when restore returns | every attempt | drop |
| `recovery_image_bytes` | byte | `recover` | per-model | receive of the image | compare image length to this field only | config lifetime | EXT |
| `recovery_image_bytes` | byte | every call except `recover` | none | none | NA. Must not be used to enlarge `request_input_bytes` | none | EXT |
| `records` | count | `owner_grant`, `issue`, `consume`, `snapshot`, `checkpoint`, `observe`, `hold`, `policy_update` | per-principal, per-tenant, per-model | prepare | +1 retained at swap only; release the reservation if swap does not commit; exact retry does not add another | never | EXT; no eviction to free a slot |
| `records` | count | `archive_read`, `page`, `read`, `page_call`, `malformed`, `auth_denied`, `terminal`, `recover`, `retry` | those scopes | none | no new record. `retry` keeps the winner's single record | never | EXT |
| `stored_bytes` | byte | same commit set as `records` | per-principal, per-tenant, per-model | prepare | add canonical retained size at swap; release reservation if no commit | never | EXT; undercharge latches `unavailable` |
| `stored_bytes` | byte | non-commit calls listed on the `records` NA row | those scopes | none | no add | never | EXT |
| `outstanding` | count | `issue` | per-principal, per-tenant, per-model | issue prepare | +1 at swap while `unused`; release when that challenge becomes `consumed` or `invalidated` | not per attempt; release is that state change only | EXT; do not clear by eviction |
| `outstanding` | count | every call except `issue` | those scopes | none | NA | none | EXT |
| `inflight` | count | every caller call except `malformed` | per-principal, per-tenant, per-model | auth | +1 disposable; release at attempt end and on crash before or after swap (not a retained stock) | every attempt | drop |
| `inflight` | count | `malformed` | those scopes | none | no reservation; parse failed first | every attempt | drop |
| `inflight` | count | `recover` | per-model | prepare | +1 disposable; release at end | every attempt | drop |
| `concurrent` | count | same calls as `inflight` | same as `inflight` | same point as `inflight` | same disposable rule, separate counter | every attempt | drop |
| `retry_attempts` | count | `retry` | per-principal, per-tenant, per-model | receive | +1 retained immediately in the basis even if the attempt later conflicts; never give the winner's bytes a free retry | never, including not at attempt start | EXT |
| `retry_attempts` | count | the twelve ops, `malformed`, `auth_denied`, `read`, `page_call` | those scopes | none | unchanged unless the call is also class `retry` | never | EXT |
| `pages` | count | `snapshot` | per-request at principal, tenant, model | prepare | compare page count; charge +count retained at swap for the side table | never for the retained count | EXT |
| `pages` | count | `page`, `page_call` | per-request at principal, tenant, model | receive | +1 disposable attempt count against the cap; release does not delete stored pages | attempt counter resets; stored pages do not | EXT |
| `pages` | count | every other call | none | none | NA | none | EXT |
| `page_items` | count | `snapshot`, `page`, `page_call` | per-request | prepare or receive | compare items on the page; no stream advance | none | EXT |
| `page_items` | count | every other call | none | none | NA | none | EXT |
| `proof_hashes` | count | `snapshot`, `page`, `page_call` | per-request | prepare or receive | compare witness length; oversize denies the view | none | EXT |
| `proof_hashes` | count | every other call | none | none | NA | none | EXT |
| `signer_calls` | count | `owner_grant`, `issue`, `consume`, `snapshot`, `checkpoint`, `observe`, `hold`, `policy_update` | per-principal, per-tenant, per-model | sign | +1 retained at swap if a tag was produced; if sign fails, +1 still retained and no positive record | never | EXT |
| `signer_calls` | count | `recover` | per-model | sign of internal tag | +1 only if the optional internal tag is stored; else 0 | never | EXT |
| `signer_calls` | count | `archive_read`, `read`, `page`, `page_call`, `terminal`, `malformed`, `auth_denied`, `retry` | those scopes | none | no new tag. `retry` must not re-sign | never | EXT |
| `store_calls` | count | every call except `malformed` | per-principal, per-tenant, per-model; `recover` is per-model | prepare or the read | +1 retained at swap or at the completed read; crash before store: no +1 | never | EXT |
| `store_calls` | count | `malformed` | none | none | NA | never | EXT |
| `issue_lifetime_ns` | ns, server domain | `issue` | per-request | sign | compare `expires_at - issued_at`; exceed denies issuance; retry does not extend | none | do not rewrite `issued_at` or `expires_at` |
| `issue_lifetime_ns` | ns | every call except `issue` | none | none | NA | none | unchanged |
| `operation_age_ns` | ns, server domain | every caller call except `malformed` | per-request | each phase after receive | compare now − receive; exceed denies that phase; client enqueue is not now | none; not a stored stock | do not invent age |
| `operation_age_ns` | ns | `malformed` | none | none | no start time | none | none |
| `operation_age_ns` | ns | `recover` | per-model | each restore phase | compare in the current server pin only | none | old challenges are invalidated by epoch, not by stretching age |
| `denial_records` | count of `Denial` | any refusing call, including `malformed`, `auth_denied`, `terminal`, `retry` conflict | per-model list, also principal and tenant ceilings | refuse | append one `Denial` at swap of the denial image, or latch if the append cannot fit | never; do not drop the earliest | EXT; overflow latches `unavailable` |
| `denial_records` | count | a call that commits a positive original | none | none | no denial row | never | EXT |
| `denial_bytes` | byte | same refusing calls | same | refuse | add canonical denial size with the same overflow latch | never | EXT |
| `denial_bytes` | byte | positive commit | none | none | NA | never | EXT |
| `fault_records` | count, counter-only | a call whose witness phase is a crash or fault | per-model, principal, tenant | witness | +1 retained counter; no trace body | never | EXT; uncertain fault count latches `unavailable` |
| `fault_records` | count | a call with no fault | none | none | NA | never | EXT |
| `audit_records` | count, counter-only | every completed call including deny and `retry` | per-model, principal, tenant | attempt end | +1 retained counter; not a log line | never | EXT |
| `calls` | count | every caller call including `malformed`, `auth_denied`, `read`, `page_call`, `retry` | per-principal, per-tenant, and per-model, separately | receive | +1 retained even if the outcome is deny or conflict | never | EXT |
| `calls` | count | `recover` | per-model only | start of restore | +1 retained. No principal (there is no caller) | never | EXT |
| `call_bytes` | byte | every caller call except `malformed` | per-principal, per-tenant, per-model | parse | add request length, and at delivery add response length, both retained | never | EXT |
| `call_bytes` | byte | `malformed` | those scopes | receive | add the received prefix that was accepted into the parser, retained, even though decode failed | never | EXT |
| `call_bytes` | byte | `recover` | per-model | image receive | add image length to the model `call_bytes` stock and also compare it to `recovery_image_bytes`. Do not add it to `request_input_bytes` | never | EXT |

Positive commit of `terminal` is not a row: the normal path is refusal, so the denial and fault rows cover it. A fixture that needs a success `records` charge for terminal is BLOCKED.

Required vectors, digest: see p2-vectors.json. `V-B4-UNSET` absent config denies. `V-B4-SPLIT` the supplied 61295-versus-16384 pairing is not a request-input success. `V-B4-RETRY` second identical attempt keeps `retry_attempts` and does not zero it. `V-B4-CRASH-PRE` reservations gone, cumulative unchanged, no positive bytes. `V-B4-CRASH-POST` cumulative remains, witness `committed`. `V-B4-AMBIG` missing witness latches `unavailable`. `V-B4-EXHAUST` denial overflow or exhausted `calls` latches `unavailable` and keeps `first_break`, originals, consumed namespaces, and hold fences. `V-B4-NOEVICT` filling `records` does not delete a hold or an original.

## 7. B5 — conditional states

Null means JSON null on a required `T?` field. Absent keys are `malformed`. `Z` is not a stand-in for null. A fabricated timestamp is not a stand-in for null. 770 syntactic null masks are not these twins and are not executed coverage. Each twin is a semantic case. digest: see p2-vectors.json.

Restore recomputes every condition below. A stored discriminator that disagrees with the recomputation is `unavailable`, not a repair. Unknown or missing basis does not grant.

### 7.1 ChallengeRow

`invalidated_by` is a service-epoch `Id`, not an event `H`. It matches `Continuity.service_epoch` or the epoch on the `RecoveryRecord` that performed the invalidation.

| `state` | Winning request `H` | Winning response `H` | `invalidated_by` | Restore | Valid twin | Invalid |
|---|---|---|---|---|---|---|
| `unused` | null | null | null | both hashes absent; epoch id absent | `V-B5-CH-UNUSED` | `V-B5-CH-UNUSED-WIN` either hash set; `V-B5-CH-UNUSED-EPOCH` epoch set |
| `consumed` | required | required | null | both equal the one `Original` for this challenge; that original's op is `consume` | `V-B5-CH-CONSUMED` | `V-B5-CH-CONSUMED-ONE` only one hash; `V-B5-CH-CONSUMED-BAD` hashes do not match that original; `V-B5-CH-CONSUMED-EPOCH` |
| `invalidated` | null | null | required epoch `Id` | no invented winner; epoch resolves; committed originals elsewhere untouched | `V-B5-CH-INVAL` | `V-B5-CH-INVAL-WIN` a winner hash set; `V-B5-CH-INVAL-NULL` null epoch; `V-B5-CH-INVAL-H` an event hash used as the id |

### 7.2 HoldRow

Normal transition into `terminal` stays BLOCKED (§4). The row shape is still closed so a stray terminal body cannot release a hold.

| `state` | `terminal_bytes` | Eligible to release | Valid twin | Invalid |
|---|---|---|---|---|
| `held` | null | no | `V-B5-HOLD-HELD` | `V-B5-HOLD-HELD-BYTES` bytes present |
| `quarantined` | null | no | `V-B5-HOLD-QUAR` | `V-B5-HOLD-QUAR-BYTES` |
| `terminal` | required, and must equal the exact prior hold reference and outcome bytes | no under this candidate. Shape twin is not a success and not an unlock | `V-B5-HOLD-TERM-SPEC` BLOCKED | `V-B5-HOLD-TERM-NONE` null bytes; `V-B5-HOLD-TERM-MIS` bytes do not match the hold |

### 7.3 ParentBoundary

| `boundary` | `checkpoint` | Meaning | Valid twin | Invalid |
|---|---|---|---|---|
| `unknown` | null | parent is not complete and not repaired | `V-B5-PAR-UNK` | `V-B5-PAR-UNK-CKPT` hash present |
| `terminal_known` | required `H` | hash resolves to a checkpoint or terminal record. It does not make earlier rows eligible and does not clear a child `unknown` | `V-B5-PAR-KNOWN` | `V-B5-PAR-KNOWN-NULL`; `V-B5-PAR-KNOWN-MISS` hash does not resolve |

### 7.4 Historical cutoff

| `decision` | `records_through` | Effect | Valid twin | Invalid |
|---|---|---|---|---|
| `unknown` | null | cutoff unknown. Deny qualification. Not all-history | `V-B5-HIST-UNK` | `V-B5-HIST-UNK-AS-ALLOW` treating this pair as allow |
| `unknown` | present `H` | still unknown. The hash does not upgrade the decision | `V-B5-HIST-UNK-HASH` | `V-B5-HIST-UNK-UP` using the hash as allow |
| `allow` | required resolvable checkpoint `H` | allow only through that resolved checkpoint | `V-B5-HIST-ALLOW` | `V-B5-HIST-ALLOW-NULL` null cutoff |
| `deny` | required resolvable checkpoint `H` | deny through that checkpoint | `V-B5-HIST-DENY` | `V-B5-HIST-DENY-NULL` |

### 7.5 KnownCheckpoint

`head` is always required `H`. `ledger` is `LedgerHead?`.

| `kind` | `stream` | `generation` | `ledger` | `head` resolves to | Valid twin | Invalid |
|---|---|---|---|---|---|---|
| `global` | null | null | required | that ledger's root, and the root matches the head | `V-B5-KC-GLOBAL` | `V-B5-KC-GLOBAL-STREAM` stream set; `V-B5-KC-GLOBAL-LEDGER` ledger null |
| `owner` | required | required owner generation | null | that owner record | `V-B5-KC-OWNER` | `V-B5-KC-OWNER-GEN` generation null |
| `journal` | required | null | null | that journal head | `V-B5-KC-JOURNAL` | `V-B5-KC-JOURNAL-LEDGER` ledger set |
| `policy` | null | required policy generation | null | that policy digest | `V-B5-KC-POLICY` | `V-B5-KC-POLICY-STREAM` stream set |
| `terminal` | required | required | null | that hold or terminal record. Does not release the hold | `V-B5-KC-TERM` | `V-B5-KC-TERM-NULL` stream null |

Empty observer list is not a kind. It is missing basis (`V-B3-MIN-MISS`), not an empty `global` checkpoint.

### 7.6 ScopedHead and LedgerHead

ScopedHead `event_count = 0` requires `last_event` null, `ack_count = 0`, `last_ack` null, `source_bound` null, `journal` null, `owner_generation` null, `closed = open`, `first_break` null, `unresolved_holds` empty. Twin `V-B5-SH-EMPTY`. Invalid `V-B5-SH-EMPTY-LAST` (count 0 with any of those fields filled) and `V-B5-SH-EMPTY-CLOSED`.

ScopedHead `event_count > 0` requires `last_event` set. `ack_count = 0` requires `last_ack` null. `ack_count > 0` requires `last_ack` set. `closed = closed` is admissible only when a committed nonempty close exists; the close rule itself stays the r3 split and is not reopened here. Twin `V-B5-SH-NONEMPTY`. Invalid `V-B5-SH-ACK` (ack count and `last_ack` disagree) and `V-B5-SH-CLOSED-EMPTY` (`closed` with `event_count = 0`).

LedgerHead `count = 0` requires `last_seq` null, `last_digest = Z`, and `root` equal to the empty-tree digest defined by the r1 MTH rule. Twin `V-B5-LH-EMPTY`. Invalid `V-B5-LH-EMPTY-SEQ` (`last_seq` set) and `V-B5-LH-EMPTY-NZ` (`last_digest` not `Z`). `count > 0` requires `last_seq = first_seq + count - 1` checked, `last_digest` not `Z`, and `root` matching the leaves. Twin `V-B5-LH-NONEMPTY`. Invalid `V-B5-LH-SEQ`.

### 7.7 Opening seal nulls

| Case | Seal | Also required | Valid twin | Invalid |
|---|---|---|---|---|
| Opening issue | null | `ack_ordinal = 0`, `prior_ack` null | `V-B5-SEAL-ISSUE` | `V-B5-SEAL-ISSUE-SET` seal present |
| Opening consume | required `H` | same ordinal and prior-ack nulls; seal is the later commitment, not the issue seal | `V-B5-SEAL-CONSUME` | `V-B5-SEAL-CONSUME-NULL` |
| Interval or close issue or consume | required `H` | matches the accepted opening seal | `V-B5-SEAL-LATER` | `V-B5-SEAL-LATER-NULL`; `V-B5-SEAL-LATER-MIS` different seal |

### 7.8 Internal and global scope

| Case | Stream | Scope | Valid twin | Invalid |
|---|---|---|---|---|
| Ledger `GlobalEntry` for any §4 ledger op | required | lineage `Scope` | `V-B5-GL-STREAM` | `V-B5-GL-NULL` stream null |
| `policy_update` | none | `RegistryScope` only, no lineage field | `V-B5-GL-POL` | `V-B5-GL-POL-LIN` a lineage scope added so it can enter `GlobalEntry` |
| `recover` | none | no caller scope | `V-B5-GL-REC` | `V-B5-GL-REC-ENTRY` a `GlobalEntry` or `Original` manufactured for it |
| Registry versus lineage | policy rows do not satisfy a stream-scoped auth | as §4 | `V-B5-GL-REG` | `V-B5-GL-REG-STREAM` stream required on policy |

### 7.9 ClientAttempt

Fields stay the r1 set: `expected_id`, `attempt`, `dispatch`, `arrival`, `raw_response_hash`, `durable_resolution`, `result`. There is no `resolved_at`. Server samples are not client milestones and cannot fill nulls.

Domain and order, checked before any late or missing label. All present monos are `ClientMono` in the core's client domain (§3). If `dispatch` and `arrival` are both present, checked `arrival >= dispatch`. If `arrival` and `durable_resolution` are both present, checked `durable_resolution >= arrival`. If `dispatch` and `durable_resolution` are both present and `arrival` is null, checked `durable_resolution >= dispatch`. Failure is `invalid`. A null milestone is skipped, not compared, and not replaced by `Z` or by the deadline.

One precedence, first match wins:

1. Domain or order failure, as above → `invalid`.
2. Stored `result = retained` while `durable_resolution` is null or `raw_response_hash` is null → inadmissible. `retained` is never eligible in that shape. Restore does not coerce it to `retained`. Twin of the bad shape: `V-B5-ATT-RET-NODUR`.
3. Classification pass is restore or readback, and `arrival` is null, and `durable_resolution` is null → `missing`. The deadline is not consulted. A missing time is not a milestone and is not `>= deadline`, so this state is reachable. Twin `V-B5-ATT-MISSING`. Invalid mutation `V-B5-ATT-MISS-LATE` labels this shape `late`.
4. Any present client milestone among `arrival` and `durable_resolution` is `>= original_deadline` → `late`. `dispatch` alone does not select `late`. Twin `V-B5-ATT-LATE`. Invalid `V-B5-ATT-LATE-EMPTY` (late with both of those milestones null).
5. `durable_resolution` present, `raw_response_hash` present, hash equals the retained response, and every present time milestone is `< original_deadline` → `retained`. Twin `V-B5-ATT-RETAINED`. This is the only eligible `retained`.
6. Pass is not restore or readback, `arrival` is null, and `durable_resolution` is null → `pending`. Twin `V-B5-ATT-PENDING`. The same bytes on a later readback are step 3 `missing`, not `pending` (`V-B5-ATT-PEND-READ`).
7. Accounting, witness, or continuity uncertainty for this attempt → `unavailable`. Twin `V-B5-ATT-UNAVAIL`.
8. Otherwise → `invalid` (`V-B5-ATT-INVALID`), including a response hash that does not match retained bytes.

No step uses "any milestone `>= deadline`" on a null. That reading is rejected because it makes step 3 unreachable. `missing` is not `late`. `retained` with a missing `durable_resolution` is not eligible.

| `result` | `dispatch` | `arrival` | `raw_response_hash` | `durable_resolution` | Eligible observation |
|---|---|---|---|---|---|
| `pending` | optional | null | null | null | no |
| `missing` | optional | null | null | null | no |
| `invalid` | any | any | any | any | no |
| `late` | optional | at least one of arrival, durable present and past the deadline | optional | see step 4 | no |
| `retained` | optional | must not be past the deadline if present | required | required, not past the deadline | yes, historical client classification only; not a new server grant |
| `unavailable` | any | any | any | any | no |

`pending` and `missing` share a null shape and are split only by step 3 versus step 6. Restore of an open-looking attempt with no arrival and no durable resolution is `missing`, not a fabricated on-time `retained`.

## 8. B6

TODO (next turn)

## 9. B7

TODO (next turn)

## 10. Open choices

TODO (next turn)

## 11. Vectors

TODO (next turn)

## 12. Pins

TODO (next turn)

## 13. C1 scan status

TODO (next turn)

## 14. UNMEASURED

TODO (next turn)

## 15. Review gate

TODO (next turn)
