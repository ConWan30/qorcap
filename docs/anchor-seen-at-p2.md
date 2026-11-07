P2 SCHEMA r3 CANDIDATE — NOT FROZEN — PENDING QorData acceptance then QorProof PASS/freeze — no model code

Private engineering schema candidate. Not a freeze, not schema PASS, not implementation acceptance, not permission to write model code. Feature `practice-anchor-transaction-model` is not added by this document and stays absent. No Rust, tests, or Cargo change belongs to this candidate.

# P2 schema r3 candidate — anchor seen-at offline model

This file is the sole allowlisted P2 schema document for the first-run schema gate. Digests are not hand-typed here. They live in `p2-vectors.json`, written by `gen_vectors.py`. Each required vector is named by a stable ID. digest: see p2-vectors.json. Computed rows are encoding facts. Model outcomes are NOT EXECUTED.

## 0. Provenance

`P2-SCHEMAS-r1-candidate.md` is a recipient-assembled chat-transfer copy, preserved separately from this candidate. It is not the missing writer-saved original, it is not authenticated by transfer markers, and it is an open proposal. Placeholders for vectors and pins in that copy stay unexpanded. Prospective "produced" or "created" wording in that copy is not execution evidence. This r3 candidate does not import any unsaved earlier worker proposal as schema.

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
| Handoff `P2-GROK-HANDOFF.md` | `741d3aa11d849046e5620c1e9d13f2b5bbd2c94567e873418e869ea3e2d49ce6` | UNKNOWN |

Repository context, supplied and not re-verified by this writer: ConWan30/qorcap; required base `4d119020cec8c3b6ae55c30c8bb17f5c6ab845ad` (operator-supplied pin for this job; the handoff itself reported no fresh git-object verification). Supplied tree pin `9fe2160a9dea08b9a501a55eb21612469be10398`. Supplied parent pin `f007728a3f2b83b47e4a49b73a99b1b2d285da9b`. Older feature head `9abea1e33c406b1ca412832ad4f2b7764b4247d1` is a different commit and must not substitute for the base. Branch name in the governing brief: `anchor/track1-phase2-p2-model-20261107`. If main moves, stop for an approved repin. No silent rebase.

Review inputs read for this r3 pass, named by file and SHA-256 only. They are not supplied pins, are not copied into the pins file, and are not acceptance: QorData `P2-PR138-qordata-acceptance.md` as reviewed, `5160bf3f8492327c2659fc6f3240d0e2d01bb0ec38402c446da20d67c4b28483`; QorProof `pr138-p2-schema-redteam.md`, `626e5b1424c56a943c1a0761b943a37ddeda251a7606241ce9ab1fbdabb947d0`.

Precedence used here: latest Data and Proof schema reviews correct r1; `PHASE2-DESIGN-r3-addendum.md` overrides `PHASE2-DESIGN-r2.md` on holder release and publication timing. Normal holder release is DISABLED. Terminal-positive stays BLOCKED. This candidate does not self-certify PASS or freeze.

## 1. Scope, non-goals, rails

In scope for the whole r3 candidate, which covers §2–§15: exact offline schema for a pure owned-memory model of auxiliary evidence. States are only `unknown`, `seen_at_late`, and `seen_at_retained`. There is no pass state. Match 201029 stays VOID. Practice does not submit to a referee, upload capture, or write practice gold. Controller output stays read-only: no HID, rumble, or lightbar writes. Grip stays off for practice. Rumble, if described at all, is information-only. `receipt.v0` and `contracts/` stay frozen. REAL MATCH stays v0.3.38 and unwired. P1 stays unchanged and default-off.

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
- Binary tree node and empty payloads are raw bytes only as adopted in §2.2 from r1 §7.1. Lengths are checked as `u64` before framing. No truncation or saturation.

`ModelSigned<T> = {alg:"synthetic_hash_only", payload:E(T,x), payload_id:H, role:RoleRef, tag:H}` with `payload_id = Hash(T,x)` and `tag = Hash(ModelTag, {alg, payload_id, role})`. The wrapper encodes as `E("ModelSigned", wrapper)`. The nested payload is already `E`. The tag is a public deterministic framing check under a trusted simulation. It is not a cryptographic signature and not authentication. No key material, host status flag, or normal-build signer exists in this schema.

`RoleRef = {issuer:Id, kind:RoleKind, generation:U, policy:H}`.

`RoleKind`, corrected in r2 because r1's set cannot name observation, hold, or recovery without overloading another kind: `challenge` | `ack` | `checkpoint` | `owner` | `terminal` | `snapshot` | `policy` | `observe` | `hold` | `recover`. Mapping of kind to record is §4. Reuse of an older kind for those three records is rejected, not optional.

Hash dependency rule carried forward: no record's preimage contains its own id, tag, response checksum, entry digest, tree root, image checksum, or page root. Manifest and page recursion stays forbidden as in r1 §7. The expected-id cycle is removed in §3, not patched with a placeholder.

### 2.1 Corrections relative to r1 §2–§3

- Delete the type `ClientExpected`. Delete the field `original_request_hash` everywhere. It is not a core field, not a request field, and not a hidden preimage.
- One time-field spelling: `enqueue_at`. The spelling `enqueue` is rejected, including as an alias.
- `ClientExpectedCore` is the only request-independent client declaration. Its domain is `Hash("ClientExpectedCore", core)` under the framing above. Fields are closed in §3.
- `RoleKind` gains `observe`, `hold`, and `recover` as distinct kinds. r1 kinds remain for the records that already had them.
- Schema string prefix and `D(T)` prefix stay the r1 strings. The candidate revision is r3; the framing label is not forked. A new type name is a new `T` under that same framing.
- `AuthBinding.scope:Scope` is replaced by `AuthBinding.subject:AuthSubject` (§4.0). A registry arm does not contain a lineage `Scope`.
- `policy_update` request and signed payload are `PolicyCore` only. `E(PolicyRecord)` is not a request and not a signed payload (§4.0, §5.1).
- `SourceBound` stays exactly `{last, head}`. No `start_ordinal`, `start_head`, `end_ordinal`, or `end_head`.
- Opening `Range` is unchanged and still has no source-domain field. The opening `Commitment` gains `capture_domain:ClockDomain` and `source_domain:ClockDomain`, immediately after `pins` and before `source`.
- `CheckpointRequest`, `Observation`, and `HoldRequest` each gain, immediately after `source_bound`: `pins:Pins`, `source_schema:H`, `capture_domain:ClockDomain`, `source_domain:ClockDomain`, in that order. On `CheckpointRequest` those four sit before `idempotency`. On `HoldRequest` they sit before `expected_revision`. On `Observation` they sit before `journal`; `reason` stays where r1 placed it, after `journal` and before `expected_revision`.
- `CheckpointRecord` gains `client_checkpoint_id:H` after `owner_generation` and before `journal`. That field is only on the checkpoint row (§4.0, §8). Consume does not store it.
- `MinimumKey.scope_digest` is deleted. `MinimumKey` carries `subject:ScopeTag` (§5.3). `ScopeTag` is `{arm:lineage, scope:Scope}` or `{arm:registry, registry:RegistryScope}`.
- Scalar `Usage` is not the accounting stock. `ExternalBasis.accounting` is `Accounting` (§5.3, §6): a bounded list of `KeyedStock`. New names: `StockKey`, `StockClass`, `MetricName`, `KeyedStock`, `Accounting`, `Floor`, `ExpectedMember`, `ExpectedSet`.
- The old `Usage` field `pages` is split into `retained_pages` (inventory) and `page_fetches` (disposable meter). The old field `proof_hashes` is split into `retained_proof_hashes` (inventory) and `proof_compares` (disposable meter). Neither old name is a field.
- `CommitWitness.phase` adds `receive`, `reserve`, and `work` (§6.3). `WitnessOutcome` is unchanged.
- Opening `capture_domain` and `source_domain` are frozen declarations on the opening commitment. They are not a calibrated clock and not a measured mapping (§9.1).
- r1 `JournalRef.checkpoint` and `prior_checkpoint` are deleted. §8 replaces them.
- r1 `StableCore.usage:Usage` is replaced by `StableCore.accounting:Accounting`. No other `StableCore` field changes. `StreamRow` gains `origin` and `fork` (§9); nothing else in `StableImage` changes.
- Vector fixtures carry these r1 records unchanged, with only the deltas above: `Close`, `Cursor`, `Page`, `PageLeaf`, `Manifest`, `SnapshotCore`, `HashPrefixWitness`, `LedgerHead`, `StableImage`, `HoldRecord`, `TerminalRecord`, `ObservationRecord`, `RecoveryRecord`, `Denial`, `Admission`, `GlobalEntry`, `Original`.

Any field not listed here or in the r1 record it carries forward is not allowed (`schema`).

Records carried forward unchanged except for the deltas above: `Scope`, `RegistryScope`, `Pins`, `ClockDomain`, `Mono`, `Wall`, `Range`, `CaptureRange`, `ParentBoundary`, and the request types named in §4 whose fields are not in the delta list. The closed inventory is §2.2.

Clock-role pairs carried forward: `server_mono` with `model_counter` and `ns`; `client_mono` with `client_declared` and `ns`; `capture_declared` with `capture_declared` and `ns`; `wall_audit` with `wall_declared` and `unix_us`. Every `process`, `boot`, `clock_epoch`, and `leader` stays explicit. No cross-role subtraction. Same-role elapsed requires equality of every domain field and nondecreasing samples, checked arithmetic only. `Wall` is audit, not a timing authority.

`Range` rules carried forward: `first <= last`; checked `last+1`; checked `last-first+1 = count`; predecessor null if and only if `first = 0`, and then `predecessor_head = Z`, otherwise `predecessor+1 = first`. An endpoint equal to the `U` maximum rejects even if a one-row count could fit. Count equal to the `U` maximum is representable for `[0, MAX-1]`. That representation is not by itself a valid first close (close rule remains the r3 nonempty split: opening `[0, MAX-2]` then close `[MAX-1, MAX-1]`, `total_rows` MAX).

### 2.2 Closed types and enums

Every type and enum this candidate adds or narrows is listed here. A field or enumerator not in this list or in the r1 record it explicitly carries forward is `schema`. Details and invariants are in the cited section. No extra accounting field is implied.

Enums:

- `Purpose`, `State`, `HoldState`, `Outcome`, `Boundary`, `Op`, `ClientOp`, `RoleKind`: §2.
- `RoleState`: `active` | `retired` | `revoked`.
- `ArchiveDecision`: `allow` | `deny`.
- `HistoricalDecision`: `allow` | `deny` | `unknown`.
- `ChargeProfile`: `b4-r2` only.
- `EventClass`: `ledger` | `side` | `read` | `registry` | `internal`.
- `AttemptResult`: `pending` | `missing` | `invalid` | `late` | `retained` | `unavailable`.
- `CheckpointKind`: `global` | `owner` | `journal` | `policy` | `terminal`.
- `WitnessPhase`: `receive` | `reserve` | `work` | `prepare` | `swap` | `confirm` | `deliver`.
- `StockClass`: `gauge` | `cumulative` | `inventory` | `meter`.
- `MetricName`: `records` | `stored_bytes` | `outstanding` | `inflight` | `concurrent` | `retry_attempts` | `retained_pages` | `page_fetches` | `retained_proof_hashes` | `proof_compares` | `signer_calls` | `store_calls` | `denial_records` | `denial_bytes` | `fault_records` | `audit_records` | `calls` | `call_bytes`.
- `ExpectedClass`: `registry` | `role` | `lineage_scope` | `policy` | `minimum` | `observer`.
- `Authn`: `authenticated` | `unauthenticated`.
- `WitnessOutcome`: `committed` | `refused` | `unknown`.
- `ForkLatch`: `open` | `latched`.
- `StreamLife`: `open` | `closed`.
- `ImageStatus`: `available` | `unavailable`.
- `ModelAlg`: `synthetic_hash_only` only.
- `AuthArm`: `lineage` | `registry`.
- r1 `Error` is closed as carried: `malformed`, `noncanonical`, `schema`, `oversize`, `limit_unset`, `quota`, `unauthorized`, `policy_unknown`, `domain`, `regression`, `overflow`, `entropy`, `collision`, `expired`, `invalidated`, `binding`, `fork`, `conflict`, `closed`, `quarantined`, `stale_writer`, `commit_unknown`, `unavailable`, `not_found`, `incomplete`, `retirement_disabled`. No new error tag.
- r1 `Observation.reason`: `source_change` | `negative_history` | `clock_change` | `unavailable` only.

Types introduced or replaced:

- `ClientExpectedCore`, `LogicalOpKey`, `ClientDispatchRecord`, `ServiceCommitKey` (§3.4). `request_type` is `IssueRequest` | `ConsumeRequest` only.
- `AuthSubject`, `AuthBinding` (§4.0). r1 `AuthBinding.scope` is not a field.
- `PolicyRole`, `PolicyCore`, `PolicyRecord`, `LimitSet`, `ServerDomainPin`, `ConfigCore`, `ConfigRecord`, `ScopeTag`, `MinimumKey`, `Minimum`, `Floor`, `StockKey`, `KeyedStock`, `Accounting`, `Continuity`, `ExpectedMember`, `ExpectedSet`, `ExternalBasis` (§5, §6). r1 `RoleRule` is deleted. r1 `HistoricalRule` is replaced by the §5 record (no nested `RoleRef`). r1 scalar `Usage` is not a field of `ExternalBasis`.
- `CommitWitness` (§6).
- `ClientCheckpointCore`, `GenesisCore` (§8). The service receipt is `CheckpointRecord`, not a second type. `JournalRef` is the §8 record. The r1 names `checkpoint` and `prior_checkpoint` are not fields.
- `StreamOrigin` (§9). r1 `StreamRow` gains `origin:StreamOrigin?` and `fork:ForkLatch`.

There is no scalar `Usage` record. Stocks are `KeyedStock` rows inside `Accounting` (§5.3). Disposable scratch (`workspace_bytes`, `parse_bytes`) is a `LimitSet` ceiling and a `meter` only. It is not an image stock.

`LimitSet` fields, each required `U`, and no others: `request_input_bytes`, `canonical_bytes`, `decoded_bytes`, `parse_bytes`, `depth`, `tokens`, `string_bytes`, `list_items`, `output_bytes`, `workspace_bytes`, `recovery_image_bytes`, `records`, `stored_bytes`, `outstanding`, `inflight`, `concurrent`, `retry_attempts`, `retained_pages`, `page_fetches`, `page_items`, `retained_proof_hashes`, `proof_compares`, `signer_calls`, `store_calls`, `issue_lifetime_ns`, `operation_age_ns`, `denial_records`, `denial_bytes`, `fault_records`, `audit_records`, `calls`, `call_bytes`, `minima_entries`, `accounting_basis_bytes`, `stock_entries`, `expected_entries`. The names `pages` and `proof_hashes` are not fields.

Tree rules adopted unchanged from r1 §7.1–7.2, and thereby closed: leaf `Hash(TreeLeaf,{epoch,position,entry_digest})`; empty tree `SHA-256(F(D("tree.empty"), empty))`; single leaf is those 32 raw bytes; larger trees split at the largest power of two strictly below the length and hash `F(D("tree.node"), left || right)`. The witness carries every leaf hash, not foreign bodies. `LedgerHead` empty and nonempty conditions are §7.6. No compact omission codec is added.

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

### 3.4 Two registries

There are two registries. They are not one map and they do not commit at the same boundary. `LogicalOpKey` is the client key. The service key is different.

```text
LogicalOpKey={scope:Scope, stream:Id, exchange:Id, operation:ClientOp}
ClientDispatchRecord={
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
ServiceCommitKey={scope:Scope, stream:Id, idempotency:Id, operation:Op}
```

`operation` on `LogicalOpKey` is projected. It is not a wire field. `IssueRequest` and `ConsumeRequest` have no `operation` member. Adding one is `schema`. The request type determines the operation: `IssueRequest` projects `issue`; `ConsumeRequest` projects `consume`. `request_type` must equal that projection, and `key.operation` must equal it. `expected_id` equals `Hash("ClientExpectedCore", core)`. `request_hash` equals `Raw(request_bytes)`.

Projection, checked by the trusted client fixture. The server still sees only the opaque `expected_id`.

| Core or key field | Equals |
|---|---|
| `operation` | the request type, as above. Not a field read from the request body. |
| `scope`, `stream`, `purpose`, `ack_ordinal`, `pins` | the same members on `IssueRequest`, or on `ConsumeRequest.commitment` |
| `exchange` | the core only. `IssueRequest` and `Commitment` have no `exchange` member. The fixture compares the core to `LogicalOpKey`. |
| `owner_generation`, `prior_ack`, `client_nonce` | those members on the issue request bytes, or on the nested consume commitment bytes. They are not core fields (§3.2). |

A mismatch is `binding` and stores no record. This is the meaning of "request matches its own core."

A consume's linked issue is the issue `ClientDispatchRecord` in the same exchange group `(scope, stream, exchange)` whose `request_hash` equals the issue `Raw` named by the challenge, and whose challenge bytes are that issue's committed response. A challenge from another exchange group is `binding` even when every hash verifies on its own. The two `expected_id` values stay unequal (§3.5).

**(a) Client pre-dispatch registry.** The value is one immutable `ClientDispatchRecord`. Its durability point is the client fixture's own commit, and that commit finishes **before dispatch**. Dispatch is not allowed for a key that has no durable client record. This commit is not the service image swap and does not write an `Original`. The first durable client record for a key wins. A second core, `expected_id`, or request body for that key is `conflict` and is not stored. Identical bytes are the same record, not a second writer. If the client commit does not complete, the key stays unregistered and nothing may later be treated as that attempt's original.

**(b) Service commit identity.** For `issue` and `consume` only, the service retains one r1 `Original` at the service atomic swap, which is **after** dispatch. The key is `ServiceCommitKey`. The identity is that `Original`: exact `request_bytes`, `request_hash`, `response_bytes`, `response_hash`, and `event` `Ref`. Issue's `Ref` is `Hash(Challenge)` with type `Challenge`. Consume's `Ref` is `Hash(AckCore)` with type `AckCore`. No other row is that identity. The service does not consult the client registry to mint bytes. It stores the request that arrived, and only if the swap commits.

**(c) Reconciliation.** Compare request bytes and `request_hash` only. Do not fill either side from the other.

| Order | Required outcome |
|---|---|
| Client durability does not complete. Dispatch does not happen. | No `ClientDispatchRecord`. No service `Original`. No later body may be called the original for that key. |
| Client record is durable. Crash before dispatch. | Client record stands. No `Original`. A later attempt must present those same bytes or it is `conflict`. It must not replace the record. |
| Dispatch happens. Service crashes before the swap. No `Original`. | Client record stands unchanged. Service outcome is not success. Retry sends the same bytes. A different body is `conflict` and does not become the original. |
| Dispatch happens. Service refuses (resolved refusal, no swap). | Client record stands. No `Original`. The refusal does not free the client record for a later deadline or a different body. |
| Service swap commits. Response is lost. | The service `Original` stands. The retry oracle below returns those exact bytes. The client record is not rewritten. |
| Restart after a durable client record and no `Original`. | Same as crash before swap. The client record is not replaced. |
| Restart after a committed `Original`. | Retry oracle. Both race orders in §3.8 keep the winner's bytes. |
| Restore. Image has the `Original`. Client fixture has the matching record. | Both stand. No rewrite. |
| Restore. Image has the `Original`. Client fixture is missing. | Service bytes stay historical. Client state stays `unknown`. Do not rebuild the client record from the `Original`. |
| Restore. Client record exists. Image has no `Original`. | Client record stands. Service stays not success. Do not invent an `Original` from the client record. |
| Restore. Both exist and the request bytes differ. | `conflict`. Neither side is replaced. |
| Two service commits race after two dispatches of different bodies for one `ServiceCommitKey`. | One `Original`. The other is `conflict`. The client key's first durable record is the only client original. A service `Original` whose bytes are not that client record does not replace it. |

An original is never replaced when dispatch did not happen or when the service swap did not commit. Missing client originals stay `unknown`.

Collision, in addition to the schedule:

| Situation | Outcome |
|---|---|
| No client record, request matches its own core and hash, client commit completes | First client writer, before dispatch. |
| Client record exists, bytes and raw and core identical | Not a new writer. Retry oracle only after a service `Original` exists. Before that, the same bytes may be dispatched again and still create at most one `Original`. |
| Same client key, different core or `expected_id` or body | `conflict`. First client record stands. |
| `expected_id` does not match the core, or the key implied by the core | `binding`. No record. |
| Identical service request after a committed `Original` | Retry oracle. No second advancement. |

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

Registration boundary. These are required fixtures for the generator. digest: see p2-vectors.json. Model outcomes are not executed by the generator.

| ID | Required outcome |
|---|---|
| `V-F1-PRE-DUR` | `ClientDispatchRecord` is durable before dispatch. Its bytes do not include a service `Original`. |
| `V-F1-NO-DISPATCH` | Client commit does not finish. No dispatch. No `Original`. No replacement body. |
| `V-F1-NO-COMMIT` | Dispatch happened. Service crashes before swap. Client record unchanged. No `Original`. |
| `V-F1-LOST-RESP` | Service `Original` exists. Response lost. Retry returns those bytes. Client record not rewritten. |
| `V-F1-REST-MATCH` | Restore finds matching client record and `Original`. Neither changes. |
| `V-F1-REST-NOCLIENT` | `Original` present, client fixture missing. Client state `unknown`. No rebuild. |
| `V-F1-REST-NOORIG` | Client record present, no `Original`. Not service success. No invented `Original`. |
| `V-F1-REST-DIVERGE` | Both present, request bytes differ. `conflict`. No replacement. |
| `V-F1-PROJ-OK` | Request type, projected operation, and the equality rows above all match. Outcome: client record accepted. No wire `operation` field. |
| `V-F1-PROJ-MIS` | One of scope, stream, purpose, ack ordinal, or pins differs from the core. Outcome: `binding`. No record. |
| `V-F1-PROJ-OWN` | Owner generation, prior ack, or client nonce differs from the request or nested commitment bytes. Outcome: `binding`. No record. |
| `V-F1-NO-WIRE` | Request body contains an `operation` member. Outcome: `schema`. |
| `V-F1-LINK` | Consume names the issue `Raw` and challenge inside the same exchange group. Outcome: link resolves. Ids stay distinct. |
| `V-F1-XGROUP` | Challenge bytes verify but belong to another exchange group. Outcome: `binding`. |
| `V-F1-REFUSE` | Service returns a resolved refusal. Outcome: client record stands. No `Original`. No new deadline. |
| `V-F1-RESTART` | Restart with a durable client record and no `Original`. Outcome: record unchanged. No replacement. |

Scope, stream, and exchange mutations change the key, so they must not be implemented as in-place edits of the first record. Purpose, ordinal, pins, and times do not change the key and therefore cannot be "fixed" by publishing a new id.

## 4. B2 — total operation, response, role, auth, and event map

r1 request names are kept except where §2.1 states a field delta. `policy_update` request bytes are `J(E("PolicyCore", core))` only. `E(PolicyRecord)` is not the request, not the signed payload, and not a second result. `ReadOriginalRequest` is the r1 archive request and is not renamed.

### 4.0 Closed cross-record map

One table. Restore uses the same lookup. A missing target is `incomplete` or `unknown`, not a fabricated row.

```text
AuthSubject={arm:lineage, scope:Scope}
          |{arm:registry, registry:RegistryScope}
AuthBinding={subject:AuthSubject, stream:Id?, operation:Op,
             owner_generation:U?, holder:Id?, issuer:Id,
             policy_generation:U, policy_digest:H}
```

Exactly one arm. `arm=lineage` requires `scope` and forbids `registry`. `arm=registry` requires `registry` and forbids `scope`. Building a lineage `Scope` from registry fields is `schema`.

| Field | Type | Preimage | Lookup, including restore |
|---|---|---|---|
| `AuthBinding.subject` | `AuthSubject` | inside the auth check only; not inside `PolicyCore` | Lineage ops require `arm=lineage` and `subject.scope` equal to the request lineage `Scope`. `policy_update` requires `arm=registry` and `subject.registry` equal to `PolicyCore.registry`. The other arm is `unauthorized`. |
| `policy_update` request | `PolicyCore` | `J(E("PolicyCore", core))`. Digest is not a field. | `Hash("PolicyCore", core)` |
| stored policy | `PolicyRecord={core:PolicyCore, digest:H}` | `digest` is outside the core. It must equal the hash above. | Registry row keyed by `(registry, generation)`. Not an `Original`. Not a `GlobalEntry`. |
| signed policy | `ModelSigned` over `PolicyCore` | payload id is that same digest. Kind `policy`. | Tag checks §2. The payload is not `PolicyRecord`. |
| current authorizing policy | the already retained `PolicyRecord` | not the `PolicyCore` submitted on this call | `AuthBinding.policy_generation` and `policy_digest` must equal that retained row. Using the new core's digest as the authorizing digest is `unauthorized`. The new core is stored only as a checked successor. |
| consume result | `AckCore` | r1 `AckCore`, no receipt id inside it | `Ref` type `AckCore`, id `Hash("AckCore", ack)`. One `Original`. One `GlobalEntry`. Charges are the consume row in §6 only. |
| consume journal link | `JournalRef` inside `Commitment` | part of `ConsumeRequest` | `client_checkpoint` looks up `ClientCheckpointCore` retained with that consume's client record. `prior_service_receipt` looks up an existing `CheckpointRecord` or is null. Consume does not insert a `CheckpointRecord`. |
| checkpoint result | `CheckpointRecord` | `request_hash`, scope, stream, `owner_generation`, `client_checkpoint_id`, `journal`, `source_bound` | This is the only service receipt. Id `Hash("CheckpointRecord", record)`. `Ref` type `CheckpointRecord`. One `Original`. One `GlobalEntry`. Charges are the checkpoint row in §6 only. No consume charge is added. |
| `StreamOrigin` | §9 | not hashed as a request | Recomputed only from the opening consume `Original.request_bytes`, using the fields in §9.1. The two domains are declarations, not a calibrated clock. |

Receipt retention for every producing op. One row, one charge family. No second output.

| Op | Retained bytes | Where | Ref | Original / global | Charge |
|---|---|---|---|---|---|
| `owner_grant` | `OwnerRecord` | response of one `Original` | `Hash(OwnerRecord)` | one / one | owner row in §6 |
| `issue` | `Challenge` | response of one `Original` | `Hash(Challenge)` | one / one | issue row in §6 |
| `consume` | `AckCore` | response of one `Original`. Not a `CheckpointRecord`. | `Hash(AckCore)` | one / one | consume row only. `stored_bytes` includes the canonical `AckCore` once, because it is the response. |
| `checkpoint` | `CheckpointRecord` | that `Original`'s response. This is the only service receipt. | `Hash(CheckpointRecord)` | one / one | checkpoint row only |
| `observe` | `ObservationRecord` | response of one `Original` | `Hash(ObservationRecord)` | one / one | observe row |
| `hold` | `HoldRecord` | response of one `Original` | `Hash(HoldRecord)` | one / one | hold row |
| `policy_update` | `PolicyRecord` | registry row | digest of `PolicyCore` | none | policy row |
| `snapshot` | `Manifest` and its pages | side table | `Hash(Manifest)` | none | snapshot row. `retained_pages` counts stored pages. |
| `archive_read`, `page`, `terminal`, `recover` | no new retained result | none | none new | none | the §6 row for that call only |

Opening `Commitment` field order for the new members: `pins`, then `capture_domain`, then `source_domain`, then `source`. Later requests insert `pins`, `source_schema`, `capture_domain`, `source_domain` immediately after `source_bound`, in the positions §2.1 names. A later request that omits any of the four is `malformed`. It is not given a default domain.

r1 said every mutating operation is an `Original` plus a `GlobalEntry`. That blanket rule conflicts with `PolicyRecord` (registry scope, no lineage `Scope`, no idempotency id) and with internal `recover` (no caller request). The correction is an explicit class, not a fabricated scope or idempotency id.

Classes:

- `ledger`: one `Original` and one `GlobalEntry`. Caller request bytes exist. Idempotency is the request's `idempotency` field, scoped by `(scope, stream, operation)`.
- `side`: retained request and response bytes in the r1 snapshot side table (`SnapshotStored`), not a `GlobalEntry`, because a snapshot must not take a ledger self-reference. Idempotency is `SnapshotRequest.idempotency` when the request has that field.
- `read`: no new `Original`, no new idempotency id, no `GlobalEntry`. Response is previously retained bytes or a page derived from a stored manifest.
- `registry`: policy only. Not an `Original` and not a `GlobalEntry`. No synthesized idempotency id and no synthesized lineage. First-writer key is `(RegistryScope, generation)`.
- `internal`: no caller request bytes, no lineage, no idempotency id, no `Original`. Not a successful external response.

`GlobalEntry.scope` is a lineage `Scope`. `GlobalEntry.stream` is null only when the event's own record allows a null stream. None of the ledger ops below allow a null stream. Internal and registry events do not use `GlobalEntry` at all, so they do not invent a stream to satisfy that field.

Historical reads authorize against the current archive and historical policy (`PolicyRecord.archive` and `HistoricalRule`), not against the write authority that was current when the row was produced. Unknown historical cutoff is `policy_unknown`, never "all history". A retired role may be read only when current archive is `allow` and the matching historical decision is `allow`. `deny` and `unknown` do not qualify the read. Current write role is neither necessary nor sufficient for archive.

`RoleRef.policy` on a tag must equal the policy digest that authorized that commit. That digest is `Hash("PolicyCore", core)` (§5.1). It is not a hash of `PolicyRecord` and not a hash of a `RoleRef`. Producers have no authority to fill `policy` by hashing `PolicyRecord` wholesale.

Holder release stays disabled. Rows that mention `terminal` define the negative and quarantine shape only. The positive terminal transition stays BLOCKED and is not a success path.

### 4.1 Caller operations

Auth columns: "requested" is the caller's presented `AuthBinding`. "required" is what the model checks now. Every lineage row requires `AuthSubject.arm=lineage` and `subject.scope` equal to that row's scope. `policy_update` is the only registry arm. `AuthBinding.stream` and `holder` and `owner_generation` follow the nullability in the row. A presented holder never substitutes for archive policy. There is no `AuthBinding.scope` field.

| Op | Request canonical | Result canonical | Requested auth | Required current auth | Stream | Owner gen | Holder | ModelSigned kind | Ref target | Original | Idempotency key | Event class | Counters touched (identity only; charge rules are §6) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `owner_grant` | `E(OwnerRequest)` | `ModelSigned<OwnerRecord>` | `Op=owner_grant`, scope match, stream set, holder set to the requested holder, owner gen = `expected_owner_generation` | current role kind `owner`, state `active`, same scope and stream; policy generation and digest match the retained minimum (minimum rules §5) | required | required; must match expected | required; bound by the synthetic issuer, not by principal id alone | `owner` on `OwnerRecord` | `Hash(OwnerRecord)` | yes | `OwnerRequest.idempotency` | ledger | `records`, `stored_bytes`, `store_calls`, `signer_calls`, denial and audit and fault counters on refusal |
| `issue` | `E(IssueRequest)` including `expected_id` | `ModelSigned<Challenge>` only after confirmed commit | `Op=issue`, scope, stream, owner gen, holder null | current owner role `active` for scope and stream; core registry agrees; caps set | required | required | null; non-null holder is `unauthorized` | `challenge` on `Challenge` | `Hash(Challenge)` | yes | `IssueRequest.idempotency` | ledger | `outstanding`, `inflight` or `concurrent` while prepared, `signer_calls`, `store_calls`, `records`, `stored_bytes`, retry counters if this is a retry, denial and audit and fault on refusal |
| `consume` | `E(ConsumeRequest)` including `commitment.expected_id` | `ModelSigned<AckCore>` only. No `CheckpointRecord` in this commit. | `Op=consume`, `AuthSubject.arm=lineage`, scope, stream, owner gen, holder null | current owner role `active`; challenge unused and unexpired at post-sign admission; fence and revision unchanged at commit; admission label only `post_sign_admission_only` | required | required | null | `ack` on `AckCore` | `Hash(AckCore)` | yes, one row | `ConsumeRequest.idempotency` | ledger | the consume row in §6 only. No receipt charge. |
| `archive_read` | `E(ReadOriginalRequest)` | stored `response_bytes` of the matching `Original`, not a new tag | `Op=archive_read`; `AuthSubject.arm=lineage`; `subject.scope` equals `ReadOriginalRequest.scope`; stream and owner gen and holder null | current `PolicyRecord.archive=allow` and historical decision `allow` for the target record's role through a resolvable cutoff; `request_hash` and `response_hash` and `operation` match one stored `Original`; ids alone do not authorize | null on the auth binding; the stored original's stream is whatever that original recorded | null on the auth binding | null; holder id is not read authority and not a substitute for archive | none minted; the historical object's existing kind is the one historical policy evaluates | the stored `Original.event` | no new original | none; do not invent one | read | `store_calls` or page counters only if a later accounting section assigns them; no stream advance; no consume |
| `snapshot` | `E(SnapshotRequest)` | `ModelSigned<Manifest>` after the side table commit | `Op=snapshot`, scope, stream null (the view covers the lineage, not one caller-chosen stream), holder null, owner gen null | current role kind `snapshot`, state `active`, for that scope; observer basis complete; archive rules do not turn the manifest into write authority | null | null | null | `snapshot` on `Manifest` | `Hash(Manifest)` | no `Original` row; bytes live in `SnapshotStored` | `SnapshotRequest.idempotency` | side | `retained_pages`, `page_items`, `retained_proof_hashes`, `signer_calls`, `store_calls`, `records`, `stored_bytes` |
| `page` | `E(PageRequest)` | `E(Page)` bytes already fixed by the manifest, no new tag | `Op=page`, scope equals the manifest scope, stream null, holder null, owner gen null | current archive `allow` for kind `snapshot` and the page's historical role; cursor and manifest id recompute; wrong scope is `unauthorized` without foreign bodies | null | null | null | none minted | none new; page id is `Hash(Page)` as an integrity id, not a new ledger `Ref` | no | none | read | `page_fetches`, `page_items`, `proof_compares`, `store_calls` |
| `checkpoint` | `E(CheckpointRequest)` including the four context fields in §4.0 | `ModelSigned<CheckpointRecord>` only. This row is the service receipt. | `Op=checkpoint`, `AuthSubject.arm=lineage`, scope, stream, owner gen, holder null | current role kind `checkpoint`, `active` | required | required | null | `checkpoint` on `CheckpointRecord` | `Hash(CheckpointRecord)` | yes, one row | `CheckpointRequest.idempotency` | ledger | the checkpoint row in §6 only. Not a second consume charge. |
| `observe` | `E(Observation)` as the request (r1 name; the request is the `Observation` record) | `ModelSigned<ObservationRecord>` | `Op=observe`, scope, stream, owner gen, holder null | current role kind `observe`, `active`; not kind `checkpoint` | required | required | null | `observe` | `Hash(ObservationRecord)` | yes | `Observation.idempotency` | ledger | same counter family as checkpoint |
| `hold` | `E(HoldRequest)` | `ModelSigned<HoldRecord>` with `state=held` or, if uncertainty is committed, `quarantined` | `Op=hold`, scope, stream, owner gen, holder equal to `HoldRequest.holder` | current role kind `hold`, `active`, plus holder binding; principal match is not sufficient | required | required | required | `hold` | `Hash(HoldRecord)` | yes | `HoldRequest.idempotency` | ledger | `records`, `stored_bytes`, `store_calls`, `signer_calls`; outstanding hold is not a releasable reservation |
| `terminal` | `E(TerminalRequest)` | no success result in this candidate | `Op=terminal`, scope, stream, holder, owner gen | normal path: `retirement_disabled` before any CAS; holder, candidate, and revision checks still apply to the denial | required | required | required | kind `terminal` is reserved for a future reviewed oracle and is not produced by the normal path | none on the normal path | no success original; denial may append a bounded denial (§6) | `TerminalRequest.idempotency` is recorded only on a denial accounting row if §6 defines one; it is not a success key | internal denial, not a ledger success | denial and fault counters only; no release of `records` or holds |
| `policy_update` | `E(PolicyCore)` | stored `PolicyRecord`; `ModelSigned` payload is `PolicyCore`, not `PolicyRecord` | `Op=policy_update`; `AuthSubject.arm=registry`; `subject.registry` equals `PolicyCore.registry`; no lineage field; stream null; holder null; owner gen null | current role kind `policy`, `active`, for that registry; new generation must be a checked successor; same generation and a different digest is `fork` | null | null | null | `policy` on `PolicyCore` | digest `Hash("PolicyCore", core)` | no | none | registry | `records`, `stored_bytes`, `signer_calls`, `store_calls` for the policy row only |
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
| policy outcome | `E(PolicyCore)` only | stored `PolicyRecord` or `fork`; signed payload stays `PolicyCore` | as `policy_update` | not ledger | lower generation is `regression` |
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
| `V-F2-AUTH-LIN` | Lineage op presents `AuthSubject.arm=lineage` and `subject.scope` equal to the request scope. Outcome: check uses that scope. No registry arm. |
| `V-F2-AUTH-REG` | `policy_update` presents `arm=registry` and `subject.registry` equal to `PolicyCore.registry`. Outcome: registry check. No lineage `Scope` on the binding. |
| `V-F2-AUTH-FAKE` | `policy_update` presents `arm=lineage`, or a lineage `Scope` built from registry fields. Outcome: `schema` or `unauthorized`. No fabricated lineage. |
| `V-F2-POL-CORE` | Policy request bytes and `ModelSigned` payload are `PolicyCore`. Stored row is `PolicyRecord`. Outcome: digest outside the core. `E(PolicyRecord)` is not the request. |
| `V-F2-RECEIPT-CONSUME` | Successful consume. Outcome: one `AckCore` `Original` and one `GlobalEntry`. No `CheckpointRecord`. No receipt charge. `prior_service_receipt` is a lookup or null. |
| `V-F2-RECEIPT-CKPT` | Successful checkpoint. Outcome: one `CheckpointRecord` row, id `Hash("CheckpointRecord", record)`. That row is the only service receipt. Checkpoint charge only. |
| `V-F2-POL-CUR` | `policy_update` presents the new core's digest as `AuthBinding.policy_digest`. Outcome: `unauthorized`. The retained current policy stays the authorizer. |
| `V-F2-DECL` | Opening domains stored as declared. Outcome: not treated as a measured clock mapping. Later ops and restore use §9. |

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
  retry_attempts:U, retained_pages:U, page_fetches:U, page_items:U,
  retained_proof_hashes:U, proof_compares:U,
  signer_calls:U, store_calls:U, issue_lifetime_ns:U, operation_age_ns:U,
  denial_records:U, denial_bytes:U, fault_records:U, audit_records:U,
  calls:U, call_bytes:U, minima_entries:U, accounting_basis_bytes:U,
  stock_entries:U, expected_entries:U
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
ScopeTag={arm:lineage, scope:Scope}
        |{arm:registry, registry:RegistryScope}
MinimumKey={registry:RegistryScope, kind:RoleKind, subject:ScopeTag}
Minimum={key:MinimumKey, generation:U, digest:H, basis:Id}
StockKey={arm:principal, principal:Id}
        |{arm:tenant, tenant:Id}
        |{arm:model, model:Id}
        |{arm:aggregate, aggregate:all}
KeyedStock={key:StockKey, class:StockClass, metric:MetricName, value:U}
Floor={key:StockKey, metric:MetricName, minimum:U}
Accounting={stocks:List<KeyedStock>, floors:List<Floor>}
ExpectedMember={class:ExpectedClass, identity:H}
ExpectedSet={members:List<ExpectedMember>}
ExternalBasis={config:ConfigRecord, minima:List<Minimum>,
               expected:ExpectedSet, continuity:Continuity,
               accounting:Accounting}
Continuity={service_epoch:Id, writer_term:U, server_domain:ServerDomainPin,
            config_digest:H}
```

`expected_set_id = Hash("ExpectedSet", expected)`. That id is not a field of `ExpectedSet`. `identity` is the hash of the named object (`PolicyCore`, `Minimum`, observer record, `Scope`, `RegistryScope`, or role bytes) under its own type name. `ScopeTag.arm` says which object. There is no `scope_digest` and no "as named on the key" choice. Swapping lineage and registry arms is `binding`.

`StockKey` has exactly one arm. `aggregate` is the literal `all`. For each `metric` and `class`, the aggregate `value` must equal the checked sum of the principal stocks of that metric and class, and must equal the checked sum of the tenant stocks of that metric and class. The model stock of that metric and class must equal the same aggregate. Inequality is `unavailable`. A list longer than `limits.stock_entries` or an `ExpectedSet` longer than `limits.expected_entries` is unusable, not truncated.

`basis` on a `Minimum` is an id in the external fixture, not a row of the image being authorized. External roots are trusted synthetic inputs. They are not proof that a different party retained the bytes.

Bounds, from the external config, applied to the basis itself:

- `minima` length ≤ `limits.minima_entries`. Over that length the basis is unusable (`unavailable`), not truncated.
- Canonical byte length of `J(E("ExternalBasis", basis))` ≤ `limits.accounting_basis_bytes`. The image's `recovery_image_bytes` cap does not authorize an oversized basis, and the basis cap does not authorize an oversized image.

The image is never a source of minima. Copying `StableCore.policies`, `config_hash`, or `observer_hash` into `Minimum.generation` or `Minimum.digest` is forbidden. A filtered or shortened minima list is missing basis, not a successful subset. Observer loss remains no recovered grant (§4 recover).

### 5.4 Restore checks

Order is fixed. First failure stops grants. Committed original bytes, consumed namespaces, `first_break`, and unresolved hold fences stay as they were; a failure here does not clear them.

1. External `ConfigRecord`, `ExternalBasis`, and observer basis are present and inside their own caps. Else `unavailable` / `policy_unknown`. No seed from the image.
2. `Continuity.config_digest` equals `ConfigRecord.digest` and `ServerDomainPin` equals `ConfigCore.server_domain`. Else `unavailable`.
3. Image `config_hash` equals that same digest. Else deny changed caps or charges (`conflict` / `fork`). Do not adopt the image's limits.
4. Bidirectional completeness against the external `ExpectedSet`, not against the rows the image still carries. Every expected `policy`, `minimum`, `observer`, `role`, `lineage_scope`, and `registry` member must be present. Every present policy row, minimum, and observer must be a member. Expected-but-missing and present-but-unexpected are both `policy_unknown` / `unknown`. Deleting a policy row and its minimum together still fails, because the expected set is outside the image. A matching `Minimum.subject` must equal the policy's registry or lineage tag. Image generation lower than that minimum: `regression`. Same generation and different digest: `fork`. Recomputing the image self-hash does not repair a missing or extra row. A complete expected set with agreeing minima restores.
5. Historical decision `unknown`, or a cutoff that §7 calls unknown: `policy_unknown`. Not all-history.
6. Image `writer_term` must equal `Continuity.writer_term`. Do not take the greater of the two. Disagreement: `commit_unknown`, no grant.
7. On an accepted restore the model then uses a fixture-supplied new `service_epoch` and checked `writer_term+1`. The new term is not read from the image. Unused challenges in the old service epoch or in a server domain that is not the current pin become `invalidated` with `invalidated_by` equal to the new service-epoch `Id` (§7). Consumed challenges and committed originals are unchanged. Client deadlines are not rewritten.
8. Image `Accounting` is reconciled to the external `Accounting` by §6. A cumulative or inventory stock below its external floor, or an aggregate that does not match its keys, is undercharge: latch `unavailable`. A gauge may be lower than an older floor only when §6's explained release applies. Disposable meters are not image stocks.

### 5.5 Deny table

| Condition | Outcome | Vector |
|---|---|---|
| External config absent | `limit_unset`, no grant | `V-B3-CFG-MISS` |
| Minima absent or shorter than the retained basis | `policy_unknown`, no grant | `V-B3-MIN-MISS` |
| Basis filtered (one minimum removed, image otherwise intact) | `policy_unknown` | `V-B3-MIN-FILT` |
| Expected member absent, including policy and minimum both deleted | `unknown` | `V-F3-EXP-MISS` |
| Image row not in the expected set | `unknown` | `V-F3-EXP-EXTRA` |
| `ScopeTag` arm swapped | `binding` | `V-F3-KEY-SWAP` |
| Lineage subject on a registry minimum, or the reverse | `binding` | `V-F3-KEY-ARM` |
| Complete expected set and minima | restore proceeds | `V-F3-EXP-OK` |
| Image self-hash recomputed after a row delete | still `unknown` | `V-F3-HASH` |
| Aggregate stock not equal to the principal sum or the tenant sum | `unavailable` | `V-F3-STOCK` |
| Cumulative stock below its external floor | `unavailable` | `V-F3-FLOOR` |
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

### 6.1 Four stock classes

Every metric has one class. A field does not serve two classes.

| Class | Metrics | Rule |
|---|---|---|
| `gauge` | `outstanding`, `inflight`, `concurrent` | May decrease only by an explained release. `outstanding` decreases by one when a challenge moves from `unused` to `consumed` or `invalidated`, and never below zero. That release is a legitimate consume-to-zero. It is not a rollback. A decrease with no such transition is `unavailable`. `inflight` and `concurrent` are disposable gauges: they return to zero at attempt end and are not image stocks. |
| `cumulative` | `records`, `stored_bytes`, `retry_attempts`, `signer_calls`, `store_calls`, `denial_records`, `denial_bytes`, `fault_records`, `audit_records`, `calls`, `call_bytes` | Never decrease. Image copies of these change only at the atomic swap, except the independently retained basis charges in §6.3. |
| `inventory` | `retained_pages`, `retained_proof_hashes` | Count stored snapshot pages and stored proof hashes. Never decrease. Not a fetch counter. |
| `meter` | `page_fetches`, `proof_compares`, plus ceilings `workspace_bytes` and `parse_bytes` | Disposable. Not an image stock. Reset at attempt end. Reset does not delete inventory. |

`retry_attempts` increments once per retry-class call in the external basis at receive (§6.3) and is never set back to zero at the start of an attempt. Winning-byte retries still pay this retained cost. They do not add a second `records` charge and do not advance the stream (§3 oracle).

Nested retained artifacts, each charged once. `stored_bytes` is the canonical byte length of the retained encoding. `records` is one per `Original` or registry or side-table manifest.

| Artifact | `records` | `stored_bytes` includes |
|---|---|---|
| `Original` response that is `AckCore` (consume) | 1 | request bytes and the `AckCore` response once. No second receipt row. |
| `Original` response that is `Challenge`, `OwnerRecord`, `ObservationRecord`, or `HoldRecord` | 1 | request bytes and that response once |
| `CheckpointRecord` | 1 | request bytes and the checkpoint record once |
| `PolicyRecord` | 1 registry row, not an `Original` | canonical `PolicyRecord` once |
| `Manifest` plus pages | 1 manifest | manifest bytes once. Each stored page adds one `retained_pages` and its byte length. Each stored proof hash adds one `retained_proof_hashes`. |
| `Denial` | the denial counters, not an `Original` | canonical denial bytes |

Attribution for a charge, including a malformed or unauthenticated receive:

```text
ChargeAttr={authn:Authn, principal:Id?, tenant:Id?, model:Id}
```

`authenticated` requires a resolved principal and tenant. `unauthenticated` requires both null. The model id is the receiving model from the external config. Unauthenticated charges update only the model stock and the aggregate. They do not invent a principal or a tenant. Empty attribution is `unavailable`.

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

Phases: receive, reserve, work, prepare, atomic swap, confirmation, delivery. Image cumulative and inventory stocks change only at swap. Independently retained basis charges update `ExternalBasis.accounting` in their own basis commit, which is not the image swap: `calls` and `retry_attempts` at receive, `signer_calls` on a failed sign, `store_calls` on a completed read. A basis commit that the witness does not record is not assumed. Confirmation loss after a committed swap does not undo image stocks. Delivery loss does not undo them.

```text
CommitWitness={before:H, after:H?, writer_term:U, revision:U,
               phase:receive|reserve|work|prepare|swap|confirm|deliver,
               outcome:committed|refused|unknown}
```

`before` and `after` are image digests when an image exists (`after` null if no new image committed). The witness is external fixture state. It is not an idempotency id, not a caller request, and not a lineage.

Resolved refusal: witness `outcome=refused`, image unchanged, and any basis charges the phase table says were already committed stay committed. Unknowable commit: no witness, or witness `unknown`, or witness and image disagree. That is `commit_unknown` / `unavailable`, not a clean refusal and not a success. Ambiguity fails closed. Originals, consumed namespaces, `first_break`, and holds are not evicted.

| Crash crossing | Image | Basis charges already committed | Reservations | Outcome |
|---|---|---|---|---|
| Before reserve | unchanged | receive `calls` / `retry_attempts`, if that receive basis commit is witnessed; nothing else | none | resolved refusal if the attempt is known not to have reached reserve. Witnessed receive charges stay committed and are not refunded. No witness is required for "nothing after receive happened"; if it is not known whether reserve started, unknowable. |
| After reserve, before work | unchanged | none | released | resolved refusal. Witness `refused`. Absent witness here is still refusal only when no work and no swap could have started. If that is not known, unknowable. |
| After work, before swap | unchanged | failed sign, completed read, receive `calls` / `retry_attempts` if those basis commits are witnessed | released | resolved refusal when the witness says `refused`. If the witness is absent, unknowable: do not treat the work as free and do not invent the image commit. |
| After swap, before witness | the swapped image stands and is not rolled back | image stocks stand | disposable meters released | unknowable for new grants: `commit_unknown`. Not a resolved refusal. |
| After witness `committed` | matches `after` | matches the image plus the basis | meters not held | committed. New grants may proceed only if §5 also passes. |
| After witness `refused` | matches `before` | only the pre-swap basis charges | released | resolved refusal. |

A missing witness is not a blanket refusal. It is refusal only in the two rows that say so. Everywhere else it is unknowable.

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
| `outstanding` | count, gauge | `issue` | principal, tenant, model, aggregate | issue prepare | +1 at swap while `unused`. Decrease by one on `consumed` or `invalidated`, including to zero. No other decrease. | not per attempt | EXT. A consume-to-zero with that transition restores. A drop without the transition is `unavailable`. Do not clear by eviction. |
| `outstanding` | count | every call except `issue` | those scopes | none | NA | none | EXT |
| `inflight` | count | every caller call except `malformed` | per-principal, per-tenant, per-model | auth | +1 disposable; release at attempt end and on crash before or after swap (not a retained stock) | every attempt | drop |
| `inflight` | count | `malformed` | those scopes | none | no reservation; parse failed first | every attempt | drop |
| `inflight` | count | `recover` | per-model | prepare | +1 disposable; release at end | every attempt | drop |
| `concurrent` | count | same calls as `inflight` | same as `inflight` | same point as `inflight` | same disposable rule, separate counter | every attempt | drop |
| `retry_attempts` | count, cumulative | `retry` | principal, tenant, model, aggregate | receive | +1 in the external basis at the witnessed receive commit, even if the attempt later conflicts; the image stock gains that +1 only if a later swap commits an image that includes it; never give the winner's bytes a free retry | never, including not at attempt start | EXT |
| `retry_attempts` | count | the twelve ops, `malformed`, `auth_denied`, `read`, `page_call` | those scopes | none | unchanged unless the call is also class `retry` | never | EXT |
| `retained_pages` | count, inventory | `snapshot` | principal, tenant, model, aggregate | prepare | +count at swap for pages stored in the side table | never | EXT |
| `page_fetches` | count, meter | `page`, `page_call` | per-request at principal, tenant, model | receive | +1 disposable fetch. Does not change `retained_pages` | meter resets; inventory does not | drop the meter |
| `retained_pages` | count | every call except `snapshot` | none | none | NA | none | EXT |
| `page_items` | count | `snapshot`, `page`, `page_call` | per-request | prepare or receive | compare items on the page; no stream advance | none | EXT |
| `page_items` | count | every other call | none | none | NA | none | EXT |
| `retained_proof_hashes` | count, inventory | `snapshot` | principal, tenant, model, aggregate | prepare | +count at swap for proof hashes stored with the manifest | never | EXT |
| `proof_compares` | count, meter | `page`, `page_call` | per-request | receive | compare witness length; oversize denies the view; meter only | meter resets | drop the meter |
| `retained_proof_hashes` | count | every call except `snapshot` | none | none | NA | none | EXT |
| `signer_calls` | count, cumulative | `owner_grant`, `issue`, `consume`, `snapshot`, `checkpoint`, `observe`, `hold`, `policy_update` | principal, tenant, model, aggregate | sign | successful tag: +1 in the image at swap. Failed sign: +1 in the external basis at the witnessed work commit, and no positive record | never | EXT |
| `signer_calls` | count | `recover` | per-model | sign of internal tag | +1 only if the optional internal tag is stored; else 0 | never | EXT |
| `signer_calls` | count | `archive_read`, `read`, `page`, `page_call`, `terminal`, `malformed`, `auth_denied`, `retry` | those scopes | none | no new tag. `retry` must not re-sign | never | EXT |
| `store_calls` | count, cumulative | every call except `malformed` | principal, tenant, model, aggregate; `recover` is model and aggregate | prepare or the read | +1 in the image at swap, or +1 in the external basis when a read completes before any swap; crash before the store: no +1 | never | EXT |
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

### 6.5 Operational limit selection protocol (future)

No operational cap is selected in this candidate. Every dependent grant, quota, and restore that needs a real cap stays DISABLED until the following exist, and the selected values stay UNKNOWN until then:

1. Workload and context, named before any run: which operation mix, which principal, tenant, and model scopes, and which synthetic-versus-live boundary. This candidate does not name a live workload.
2. Unit and scope: the `MetricName` or `LimitSet` field, and the `StockKey` arm the measurement applies to.
3. Saturation, failure, and coverage criteria: what observation would show the stock exhausted, what observation would show a false deny, and what share of the named workload was covered. Those criteria are not evaluated here.
4. Measurement record and provenance: who recorded the run, the artifact that holds the record, and the supplied-pin rule for that artifact. This file does not contain that record.
5. Approval by Con, in a later review, before any number becomes an operating cap.

Until that approval, a fixture may carry a `LimitSet` only as a synthetic fixture input. Each such input is one canonical `U` inside that fixture, labeled synthetic, bounded by that fixture's own field, and not a setting, not an observation, and not an approved cap. This document does not choose those integers.

Required vectors, digest: see p2-vectors.json. `V-B4-UNSET` absent config denies. `V-B4-SPLIT` the supplied 61295-versus-16384 pairing is not a request-input success. `V-B4-RETRY` second identical attempt keeps `retry_attempts` and does not zero it. `V-B4-CRASH-PRE` reservations gone, image cumulative unchanged, no positive bytes. `V-B4-CRASH-POST` image cumulative remains, witness `committed`. `V-B4-AMBIG` unknowable witness latches `unavailable` and is not a resolved refusal. `V-B4-EXHAUST` denial overflow or exhausted `calls` latches `unavailable` and keeps `first_break`, originals, consumed namespaces, and hold fences. `V-B4-NOEVICT` filling `records` does not delete a hold or an original. `V-F4-GAUGE-ZERO` consume of the last unused challenge restores `outstanding` at zero. `V-F4-GAUGE-ROLL` outstanding drop without that transition is `unavailable`. `V-F4-PRE-RES`, `V-F4-POST-RES`, `V-F4-POST-WORK`, `V-F4-POST-SWAP`, `V-F4-POST-WIT` are the five crash crossings. `V-F4-REFUSE` is a witnessed resolved refusal. `V-F4-UNKNOWABLE` is a missing witness after work or after swap. `V-F4-SIGN` failed sign charges the basis and writes no positive record. `V-F4-READ` a completed read charges `store_calls` in the basis. `V-F4-NEST` consume `stored_bytes` includes `AckCore` once. `V-F4-ATTR` unauthenticated receive charges model and aggregate only.

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

Historical classification and present eligibility are different passes. The historical label and the historical bytes are immutable. Eligibility never rewrites them.

`unavailable` is assigned by one rule only, checked before step 1: the classification pass is restore or readback and the stored `ClientAttempt` bytes for this attempt are absent from the client fixture image or fail the §2 parse, so no milestone can be read. The result is `unavailable`, not `missing` (which needs a parsed record with null milestones) and not `invalid` (which needs parsed milestones that fail domain or order). It never grants and is never relabeled by a later pass. No step 1–7 below assigns `unavailable`.

Historical classification, first match. `dispatch` and `arrival` may stay null. This pass does not grant.

1. Domain or order failure, as above → `invalid`.
2. Stored `result = retained` while `durable_resolution` is null or `raw_response_hash` is null → inadmissible shape. Do not relabel it `retained`. Twin `V-B5-ATT-RET-NODUR`.
3. Classification pass is restore or readback, and `arrival` is null, and `durable_resolution` is null → `missing`. The deadline is not consulted. Twin `V-B5-ATT-MISSING`. Invalid mutation `V-B5-ATT-MISS-LATE` labels this shape `late`.
4. Any present client milestone among `arrival` and `durable_resolution` is `>= original_deadline` → `late`. `dispatch` alone does not select `late`. Twin `V-B5-ATT-LATE`. Invalid `V-B5-ATT-LATE-EMPTY`.
5. `durable_resolution` present, `raw_response_hash` present, hash equals the retained response, and every present time milestone is `< original_deadline` → historical `retained`. `arrival` may be null. Under the trusted client fixture, that on-time durable milestone bounds chronology. It does not require every optional field to be nonnull. Twin `V-B5-ATT-RETAINED`.
6. Pass is not restore or readback, `arrival` is null, and `durable_resolution` is null → `pending`. Twin `V-B5-ATT-PENDING`. The same bytes on a later readback are step 3 `missing`, not `pending` (`V-B5-ATT-PEND-READ`).
7. Otherwise → `invalid` (`V-B5-ATT-INVALID`), including a response hash that does not match retained bytes.

Present eligibility runs after the historical label exists and before any eligible observation. First failure means not eligible. The historical label stays.

1. Witness, accounting, or continuity for this attempt is uncertain → not eligible. Label unchanged. Twin `V-F5-RET-WIT`, `V-F5-RET-ACCT`, `V-F5-RET-CONT`, one uncertainty each, same retained bytes.
2. Original linkage for this attempt is missing → not eligible. Twin `V-F5-RET-LINK`.
3. A sticky prior `missing`, `late`, `fork`, or other negative on this stream or attempt is set → not eligible. Twins `V-F5-RET-PRIOR-MISS`, `V-F5-RET-PRIOR-LATE`, `V-F5-RET-PRIOR-FORK`.
4. Historical label is not `retained` → not eligible.
5. Only then the historical `retained` observation is eligible as history. It is not a new server grant. Twin `V-F5-RET-OK`.

Readback must not fill a null arrival, dispatch, or durable time, and must not upgrade `missing` or `late` to `retained`. Twin `V-F5-RET-READBACK`. Lost original timing stays lost.

No step uses "any milestone `>= deadline`" on a null. `missing` is not `late`. Historical `retained` with uncertainty is still not eligible.

| `result` | `dispatch` | `arrival` | `raw_response_hash` | `durable_resolution` | Eligible observation |
|---|---|---|---|---|---|
| `pending` | optional | null | null | null | no |
| `missing` | optional | null | null | null | no |
| `invalid` | any | any | any | any | no |
| `late` | optional | at least one of arrival, durable present and past the deadline | optional | see step 4 | no |
| `retained` | optional, may be null | optional, may be null; if present, must not be past the deadline | required | required, not past the deadline | only after the eligibility pass. Uncertainty, missing linkage, or a sticky prior negative: historical label stays `retained`, observation is not eligible |
| `unavailable` | any | any | any | any | no |

`pending` and `missing` share a null shape and are split only by step 3 versus step 6. Restore of an open-looking attempt with no arrival and no durable resolution is `missing`, not a fabricated on-time `retained`.

## 8. B6 — client checkpoint identity and service receipt

r1 `JournalRef.checkpoint` is one required `H`, while `CheckpointRequest` and `CheckpointRecord` both embed `JournalRef`. That single field cannot be both the client commitment and the service receipt: the receipt hash would contain itself, and the first request would need a receipt that does not exist yet. r3 keeps two ids and one receipt type. The service receipt is `CheckpointRecord`. There is no `ServiceReceipt` type. `Z` and an arbitrary `H` are not a root. This section agrees with §4.0 and the §4.1 consume and checkpoint rows: consume commits `AckCore` only; only the `checkpoint` op commits `CheckpointRecord`.

### 8.1 Two ids

```text
ClientCheckpointCore={
  scope:Scope, stream:Id, owner_generation:U,
  first:U, last:U, count:U, head:H, negative_count:U,
  prior_client_checkpoint:H?
}
client_checkpoint_id = Hash("ClientCheckpointCore", core)

GenesisCore={
  scope:Scope, stream:Id, origin:StreamOrigin,
  empty_head:H
}
genesis_id = Hash("GenesisCore", core)

JournalRef={
  first:U, last:U, count:U, head:H, negative_count:U,
  prior_client_checkpoint:H?,
  client_checkpoint:H,
  prior_service_receipt:H?
}

CheckpointRecord={
  request_hash:H, scope:Scope, stream:Id, owner_generation:U,
  client_checkpoint_id:H, journal:JournalRef, source_bound:SourceBound
}
service_receipt_id = Hash("CheckpointRecord", record)
```

`ClientCheckpointCore` has no service-receipt field, no request hash, no tag, and no ledger root. `GenesisCore` has no receipt and no client-checkpoint id. `CheckpointRecord` has no `service_receipt_id` field. `JournalRef` inside a checkpoint record names `prior_service_receipt` (a past id or null), never that record's own id. `SourceBound` on the record is exactly `{last, head}`.

Lookup, and only that lookup:

| Id | Preimage | Table |
|---|---|---|
| `genesis_id` | `GenesisCore` | external trusted genesis fixture |
| `client_checkpoint_id` | `ClientCheckpointCore` | retained next to the consume client record, outside the checkpoint preimage |
| `service_receipt_id` | `CheckpointRecord` | the checkpoint `Original` row only |

A client id is not found in the checkpoint table. A receipt id is not found in the client-core table. Using one in the other's field is `binding`. `Z` is rejected in all three id positions and in both prior fields. An `H` that is not the hash of a retained core, genesis core, or checkpoint record is `not_found` and does not create history.

r1 names `checkpoint` and `prior_checkpoint` are deleted. The request type stays `CheckpointRequest`, with its `journal` field using the `JournalRef` above. `Commitment.journal` uses the same `JournalRef`. Consume stores neither name.

`JournalRef` counters must match the `ClientCheckpointCore` with the same span and `head`. `client_checkpoint` must equal `Hash("ClientCheckpointCore", core)`. `CheckpointRecord.client_checkpoint_id` must equal `journal.client_checkpoint`.

### 8.2 Null priors and genesis

Trusted synthetic genesis is an external fixture row, same trust class as `ExternalBasis`. It is not caller-supplied and not read out of a restored image. `empty_head` must be the empty-tree digest of §2.2 for an empty journal, not `Z` and not a caller `H`. `origin` is the stream origin of §9 and is fixed here for the stream's first opening.

| Situation | `prior_client_checkpoint` | `prior_service_receipt` |
|---|---|---|
| `GenesisCore` | the core has no prior field | the core has no receipt field |
| First client checkpoint, including the opening consume, after that genesis | `genesis_id` | null. No `CheckpointRecord` exists yet. |
| Later consume while no checkpoint row is committed | previous client id | null |
| Any consume or checkpoint request after a `CheckpointRecord` exists | previous client id | that record's `Hash("CheckpointRecord", record)` |

Null `prior_client_checkpoint` does not start history. With no genesis fixture, the stream is `incomplete` and `unknown`. A null `prior_service_receipt` is legal only while that stream has no committed `CheckpointRecord`. If a checkpoint row exists and the prior is null, the outcome is `incomplete` and new grants on the stream are `unknown`. Self as prior (the prior client id equal to this core's own id, or the prior receipt equal to the checkpoint record being hashed) is `binding`. The receipt id this checkpoint commit will produce cannot appear in the request: the request is an input to that hash, and the id does not exist until the hash is computed. A consume never produces that id.

### 8.3 Constructive order

None of these requests contains a future or current service receipt id.

1. Fixture stores `GenesisCore`. `genesis_id` is its hash. No `CheckpointRecord` exists.
2. First `issue`. `IssueRequest` has no `JournalRef` and no receipt field. Constructible immediately after genesis.
3. First `consume`. The caller builds `ClientCheckpointCore` with `prior_client_checkpoint = genesis_id` and with no receipt field. The request sets `prior_service_receipt` null and `client_checkpoint` to that core's hash. The server resolves `genesis_id` in the genesis table, then commits one `AckCore` `Original` and one `GlobalEntry`. It does not commit a `CheckpointRecord`. Charges are the consume row in §6 only. The client checkpoint id stays on the client record. It is not a second output.
4. First `checkpoint` op. The request names the client checkpoint id from step 3 and keeps `prior_service_receipt` null, because no checkpoint row exists yet. The server commits one `CheckpointRecord`. Its id is `Hash("CheckpointRecord", record)` after the request bytes exist. It is not written back into the request. Charges are the checkpoint row in §6 only. This commit does not add a consume charge.
5. Later consume. `prior_service_receipt` is the id from step 4. The consume still commits `AckCore` only and does not insert another `CheckpointRecord`.
6. Linked checkpoint extension. The next core's `prior_client_checkpoint` is the previous client id. The request's `prior_service_receipt` is the previous checkpoint record id. The new record hash covers that request and does not contain itself.

The nullable prior fields exist so a missing link can be encoded and rejected. They are not an alternate root. `Z` is not that encoding.

`empty_head` on genesis is the journal head the first core must extend under §9. It is not a service receipt.

### 8.4 Vectors

digest: see p2-vectors.json. `V-F2-RECEIPT-CONSUME` and `V-F2-RECEIPT-CKPT` in §4.3 are the same receipt rule.

| ID | Case | Outcome |
|---|---|---|
| `V-B6-BOOT` | Genesis, then issue, then first consume with `prior_service_receipt` null and `prior_client_checkpoint = genesis_id` | consume commits `AckCore` only. No `CheckpointRecord`. Request bytes do not contain a receipt id. |
| `V-B6-EXT` | Next checkpoint names that client id and null prior receipt | checkpoint commits one `CheckpointRecord`. New id differs from the client id. The later consume that names the new receipt is `V-FAM-BOOT-LATER` (§11.1); it does not write a second receipt. |
| `V-B6-WRONG-ROOT` | Same priors, `head` or source head swapped | `fork`, no commit |
| `V-B6-SELF` | Prior client id set to this core's own id, or a core field added that copies the id | `binding` |
| `V-B6-CURRENT` | Request field set to the checkpoint id this commit would produce, or to the latest receipt used as the client id | `binding` |
| `V-B6-MISSING` | Extension with null receipt prior after a checkpoint row exists, or prior id that does not resolve | `incomplete`, grant `unknown` |
| `V-B6-Z` | `Z` as genesis, client id, receipt, or either prior | `binding` |
| `V-B6-ANY-H` | Random `H` as genesis or as first prior | `not_found`, no new history |

## 9. B7 — original stream context and equal-height forks

### 9.1 What is retained

At the committed opening consume, the stream stores one immutable origin, taken from that opening's retained request, not from current policy.

```text
StreamOrigin={
  pins:Pins,
  source_schema:H,
  capture_domain:ClockDomain,
  source_domain:ClockDomain
}
```

Restore reads only the opening consume `Original.request_bytes`. Decode that body as `ConsumeRequest`. Read `commitment` in the §4.0 order (`pins`, `capture_domain`, `source_domain`, `source`). The lookup is exact:

| `StreamOrigin` field | Type | Byte it is copied from |
|---|---|---|
| `pins` | `Pins` | `commitment.pins` |
| `capture_domain` | `ClockDomain` | `commitment.capture_domain` |
| `source_domain` | `ClockDomain` | `commitment.source_domain` |
| `source_schema` | `H` | `commitment.source.source_schema` (carried r1 `Range` field; `Range` still has no source-domain field) |

Every opening `CaptureRange.domain` must equal `capture_domain`. The source high-water domain is `commitment.source_domain`, not a field of `Range` and not a field of `SourceBound`. `SourceBound` stays `{last, head}`. Mixed capture domains at opening, or a missing `capture_domain` or `source_domain` on that commitment, are `domain` or `malformed` and do not store an origin. No default domain is filled in. `source_domain` and `capture_domain` are full `ClockDomain` values (every id), not a role name alone. They are frozen declarations copied from the opening request. They are not a calibrated source clock and not a measured mapping between roles.

r1 `StreamRow` gains `origin:StreamOrigin?` and `fork:ForkLatch`. `origin` is null only before the opening consume commits. After that it is required. Restore recomputes `origin` by the table above. Disagreement with the stored origin is `fork` / `unknown` and is not rewritten from the image's later rows or from current auth. If the opening bytes omit a listed field, restore stores nothing and the stream stays `unknown`.

Genesis `GenesisCore.origin` must equal this opening origin. A mismatch is `binding` and does not open the stream.

### 9.2 Later equality

`CheckpointRequest`, `Observation`, and `HoldRequest` carry the four fields in the §2.1 positions: immediately after `source_bound`, in the order `pins`, `source_schema`, `capture_domain`, `source_domain`. `Observation.reason` stays after `journal`. A later consume carries `pins`, `capture_domain`, and `source_domain` on `Commitment` and `source_schema` on `Range`. Omitting any of the four is `malformed`. It is not repaired by copying the stored origin into the request.

Those four must equal the stored `StreamOrigin`. Inequality is `binding` if the request is a new body under an existing key, and `fork` if it would otherwise commit a second context for the stream. Current role, archive allow, revocation, or a newer eligibility or checker digest does not replace `origin`. Those checks can still deny (`unauthorized`, `policy_unknown`). They cannot authorize a different pin. A revoked role denies the call and leaves `origin` as it was.

Restore applies the same equality before any grant. A missing later field fails closed: `malformed`, no grant, origin unchanged.

### 9.3 Equal height is not progress

Same source endpoint (`SourceBound.last` or the range `last`) and same height (`count` or `last` equal to the committed head's endpoint) with a different `SourceBound.head` is `fork`, not a nonregression success. `SourceBound` has no `end_head`. `Range.end_head` is a different field and is not a source-bound member. Same journal `first`/`last`/`count` with a different `head` or a different `client_checkpoint` id is `fork`. The latch is `ForkLatch=latched` on that stream and is sticky. `first_break` is set if it was null and is not cleared. Grant state for new work on that stream is `unknown`.

An exact duplicate of a committed winning request is the §3 retry twin, not a fork. It does not clear a latch that is already `latched`. A new exchange, a higher endpoint, or any later well-formed span does not clear `latched` and does not repair an older `unknown`.

| ID | Case | Outcome |
|---|---|---|
| `V-B7-OPEN` | Opening consume | origin stored from that request | 
| `V-B7-EQ` | Later op, same pins, schema, and both domains | other checks aside, not a context reject |
| `V-B7-PIN` | One pin field, `source_schema`, or either domain differs | `fork` or `binding`; origin unchanged |
| `V-B7-AUTH` | Current archive allow with a different checker pin | origin unchanged; call denied |
| `V-F2-ORIGIN-FIELDS` | Opening consume carries `commitment.pins`, `capture_domain`, `source_domain`, and `source.source_schema` | origin stored by the §9.1 lookup only |
| `V-F2-LATER-EQ` | Later checkpoint, observe, or hold presents the same four fields | not a context reject on those four |
| `V-F2-MISS-DOMAIN` | Opening or later request omits `source_domain` or `capture_domain` | `malformed` or `domain`; no stored origin on an opening miss; no default; no grant |
| `V-B7-FORK-HEAD` | Same endpoint and height, different `SourceBound.head` | `fork`; latch sticky; `unknown` |
| `V-B7-FORK-JRN` | Same journal span, different head or client checkpoint id | `fork`; latch sticky |
| `V-B7-DUP` | Byte-identical retry | twin; no second commit; latch not cleared |
| `V-B7-FWD` | Later span after a latch | still `unknown` |
| `V-B7-RESTORE` | Image origin disagrees with opening bytes | `unknown`; no repair |

digest: see p2-vectors.json.

## 10. Open choices

Status `RESOLVED` means this candidate states one rule and where it is written. It is not schema PASS. Status `UNKNOWN` means this candidate has no authority to pick a value. Reviewer is QorData for acceptance reconciliation, then QorProof for exact-schema PASS or freeze, unless the row says otherwise.

| Choice | Status | Where | Review |
|---|---|---|---|
| 1. Grammar, domains, public tags | RESOLVED as synthetic `E`/`J`/`F`/`Raw`, explicit null, r1 framing prefix | §2, §2.2 | QorData, then QorProof. Tags are not authentication |
| 2. Full hash-only witness | RESOLVED by adopting r1 §7.1–7.2 unchanged | §2.2, §7.6 | QorData, then QorProof. Not a compact codec. O(n) leakage stays a limit |
| 3. API, reservation, accounting (B2) | r3: addresses QorData F1/F2 — pending re-review | §4.0, §4.1 | Not self-RESOLVED. Accounting closure is the B3/B4 rows. Caps are not live defaults |
| 4. Nonempty close | RESOLVED as the r3 split: opening `[0, MAX-2]`, close `[MAX-1, MAX-1]`, not a first close of `[0, MAX-1]` | §2 | QorData, then QorProof |
| 5. Holder release and terminal oracle | RESOLVED as release DISABLED. Positive terminal path BLOCKED. No unlock in this candidate | §4, §7.2 | QorData, then QorProof. A future private test oracle is UNKNOWN and is not authorized here |
| 6. Image, restore, retry (B1) | r3: addresses QorData F1/F2 — pending re-review | §3.4, §3.6, §8, §9 | Not self-RESOLVED. Two registries and the §3.4 schedule are written. F3–F5 are the B3/B4/B5 rows below. Stable image is model-durable only |
| 7. Real source, custody, current-policy freshness, hosting, cost, retention, collection | UNKNOWN | not selected | No authority in this candidate or in the schema reviews. Publication-time freshness is not asserted. Dependent final or pilot stays BLOCKED |
| `enqueue_at` spelling, core without `original_request_hash` | RESOLVED | §3 | QorData, then QorProof |
| Owner generation, prior ack, and client nonce outside the core and inside request bytes | RESOLVED | §3.2 | QorData, then QorProof |
| `RoleKind` adds `observe`, `hold`, `recover` | RESOLVED | §2, §4 | QorData, then QorProof |
| Policy request is `PolicyCore`; digest outside the payload; `RoleRule` deleted (B2) | r3: addresses QorData F1/F2 — pending re-review | §4.0, §5.1 | Not self-RESOLVED |
| `invalidated_by` is a service-epoch `Id` | RESOLVED | §7.1 | QorData, then QorProof |
| Audit and fault are counter-only; denial keeps the bounded `Denial` list | RESOLVED | §6.1 | QorData, then QorProof |
| Client checkpoint id versus service receipt id (B2) | r3: addresses QorData F1/F2 — pending re-review | §4.0, §8 | Not self-RESOLVED. Receipt is `CheckpointRecord` from the checkpoint op only |
| Null client prior does not start history; genesis id is the first prior | RESOLVED | §8.3 | QorData, then QorProof |
| Stream origin equality and sticky equal-height fork (B7) | r3: addresses QorData F1/F2 — pending re-review | §2.1, §9 | Not self-RESOLVED |
| Two registries: client pre-dispatch record and service `Original` (B1) | r3: addresses QorData F1/F2 — pending re-review | §3.4 | Not self-RESOLVED |
| `AuthSubject` tagged union (B2) | r3: addresses QorData F1/F2 — pending re-review | §4.0 | Not self-RESOLVED |
| B3 expected set, minima key, keyed stocks | r3: addresses QorData F3–F5 / QorProof F3–F5 — pending re-review | §5.3, §5.4 | Not self-RESOLVED |
| B4 stock classes, crash crossings, limit-selection protocol | r3: addresses QorData F3–F5 / QorProof F3–F5 — pending re-review | §6 | Not self-RESOLVED. Operational values stay UNKNOWN. Dependents stay DISABLED |
| B5 historical label versus present eligibility | r3: addresses QorData F3–F5 / QorProof F3–F5 — pending re-review | §7.9 | Not self-RESOLVED |
| Complete positive chains, mutation negatives, twelve families (QorData F6; QorProof F6 and family list) | r3: addressed — pending re-review | §11, §11.1 | Not self-RESOLVED. Every vector carries complete typed records; negatives are one-field mutations of a named twin; model outcomes NOT EXECUTED |
| Pins file holds only the §0 source table and generator-observed file pins (QorData F7) | r3: addressed — pending re-review | §0, §12 | Not self-RESOLVED. Strict parse of the §0 table only |
| Execution labels (QorProof F8) | r3: addressed — pending re-review | §11 | Authored negatives are labeled authored and NOT EXECUTED. Only generator-run checks are counted as executed, and they are not the model parser |
| Numeric `LimitSet` values, including any replacement for the supplied 61295-versus-16384 example | UNKNOWN | §5.2, §6.2, §6.5 | No authority. Synthetic fixture inputs are not measurements. This candidate does not pick them |
| Qualified C1 result on these bytes | UNKNOWN in this file | §13 | The operator runs the counts-only scan on the final committed bytes, commit message, and candidate PR text. Results are review evidence outside this file. Not stated here. Not zero |
| Fresh git confirmation of the supplied base | UNKNOWN to this writer | §0 | Supplied pin only. Movement requires a stop |
| Vector bytes and pin file contents | RESOLVED as generator output | §11, §12 | `gen_vectors.py` writes `p2-vectors.json` and `p2-pins.json`. Computed rows are encoding facts. Model outcomes are NOT EXECUTED. QorData, then QorProof |

## 11. Vectors

Canonical bytes, lengths, and digests are not written in this document. digest: see p2-vectors.json.

`tests/anchor_seen_at_p2/vectors/gen_vectors.py` is a Python 3 standard-library generator. It does not import repository code. It implements the §2 framing (`J`, `E`, `F`, `D`, `Hash`, `Raw`, `ModelSigned`) and emits one record per vector ID in §3–§9 and §11.1. Every ID in this document has exactly one vector, and the generator has no ID that is not in this document.

Evidence classes, as written into each vector's `evidence_class`:

- `authored_positive_fixture`: complete typed records built in dependency order (genesis, issue core, `IssueRequest`, `Challenge`, consume core, `ConsumeRequest`, `AckCore`, `Admission`, `GlobalEntry`, `Original`, `LedgerHead`, and onward). Encoding facts are computed. The future model outcome is NOT EXECUTED.
- `authored_negative_expectation`: a one-field mutation of a named positive twin (`valid_twin`, `mutation.path`, `mutation.value`, mutated canonical bytes), or a complete stand-alone record where a mutation cannot express the case. The expected rejection is authored and NOT EXECUTED. No validator has run these. A count of these rows is a count of declarations, not of rejections.
- `authored_schedule_fixture`: an ordered schedule over complete records (crash points, retries, restores). NOT EXECUTED.
- `generator_computed_fixture`: encoding, digest, or arithmetic facts computed by the generator. NOT EXECUTED as model behavior.

`self_checks` in `p2-vectors.json` separates what ran from what did not. `executed_primitive_assertions` and `generator_schema_checks` are checks this generator really runs (primitive bounds, canonical re-serialization, projection, completeness, stock arithmetic, range arithmetic, tree prefixes). They are not the model parser and not a model outcome. `authored_negative_expectations_not_executed` and `model_outcomes_not_executed` are counts of rows whose outcome nobody has executed. Counts are read from that file, not restated here. Any figure used as a cap in a quota family is a synthetic fixture cap, labeled as such in the row; it is not a setting, not a measurement, and not an approved limit.

Regenerate with `python3 tests/anchor_seen_at_p2/vectors/gen_vectors.py --write`. The same command exits nonzero if a self-check fails or a doc vector ID is missing or extra. `python3 tests/anchor_seen_at_p2/vectors/gen_vectors.py --check` rebuilds in memory and exits nonzero if either JSON file differs.

### 11.1 Required families

The twelve families QorProof required. Each row is a complete record or a one-field mutation of the named positive in its family. Expected outcomes are schema expectations, NOT EXECUTED. The terminal row is BLOCKED and is not a success path.

| ID | Family | Evidence class | Expected outcome (NOT EXECUTED) |
|---|---|---|---|
| `V-FAM-DAG-CHAIN` | 1 dependency DAG | authored_positive_fixture | complete issue, Challenge, consume, AckCore, Admission, GlobalEntry, Original chain |
| `V-FAM-DAG-NESTED-SCOPE` | 1 dependency DAG | authored_negative_expectation | binding |
| `V-FAM-DAG-NESTED-PINS` | 1 dependency DAG | authored_negative_expectation | binding |
| `V-FAM-DAG-DOMAIN` | 1 dependency DAG | authored_negative_expectation | domain |
| `V-FAM-DAG-STALE` | 1 dependency DAG | authored_negative_expectation | binding |
| `V-FAM-ORIG-WINNER` | 2 Original registry | authored_positive_fixture | one Original; one advance |
| `V-FAM-ORIG-LOSER` | 2 Original registry | authored_negative_expectation | conflict; never inherits winner success |
| `V-FAM-ORIG-RETRY` | 2 Original registry | authored_schedule_fixture | byte-identical response; no re-sign, no new time, no advance |
| `V-FAM-ORIG-OWNER` | 2 Original registry | authored_negative_expectation | conflict |
| `V-FAM-ORIG-CHALLENGE` | 2 Original registry | authored_negative_expectation | binding |
| `V-FAM-ORIG-TYPE` | 2 Original registry | authored_negative_expectation | binding |
| `V-FAM-ALIAS-GROUP` | 3 issue/consume aliasing | authored_positive_fixture | issue and consume records in one exchange group; distinct ids |
| `V-FAM-ALIAS-REDEADLINE` | 3 issue/consume aliasing | authored_negative_expectation | conflict; stored deadline unchanged |
| `V-FAM-ALIAS-NEWID` | 3 issue/consume aliasing | authored_negative_expectation | conflict |
| `V-FAM-ALIAS-WRONGOP` | 3 issue/consume aliasing | authored_negative_expectation | binding |
| `V-FAM-ALIAS-RESTORE-LATE` | 3 issue/consume aliasing | authored_negative_expectation | late stays late after restore and archive |
| `V-FAM-BOOT-FIRST` | 4 bootstrap and receipts | authored_positive_fixture | genesis, first client core, consume with null receipt prior |
| `V-FAM-BOOT-NEXT` | 4 bootstrap and receipts | authored_positive_fixture | checkpoint commits CheckpointRecord; lookup tables resolve |
| `V-FAM-BOOT-LATER` | 4 bootstrap and receipts | authored_positive_fixture | later consume names the committed receipt as prior; no second receipt |
| `V-FAM-BOOT-HEAD` | 4 bootstrap and receipts | authored_negative_expectation | fork |
| `V-FAM-BOOT-ARCHIVE` | 4 bootstrap and receipts | authored_positive_fixture | archive returns historical bytes; origin not replaced |
| `V-FAM-GRAM-OK` | 5 grammar, roles, nulls | authored_positive_fixture | canonical |
| `V-FAM-GRAM-NONCANON` | 5 grammar, roles, nulls | authored_negative_expectation | noncanonical |
| `V-FAM-GRAM-DUP` | 5 grammar, roles, nulls | authored_negative_expectation | malformed |
| `V-FAM-GRAM-NUMBER` | 5 grammar, roles, nulls | authored_negative_expectation | schema |
| `V-FAM-GRAM-BOOL` | 5 grammar, roles, nulls | authored_negative_expectation | schema |
| `V-FAM-GRAM-SPACE` | 5 grammar, roles, nulls | authored_negative_expectation | noncanonical |
| `V-FAM-GRAM-ESCAPE` | 5 grammar, roles, nulls | authored_negative_expectation | noncanonical |
| `V-FAM-GRAM-NONASCII` | 5 grammar, roles, nulls | authored_negative_expectation | noncanonical |
| `V-FAM-GRAM-UNKNOWN` | 5 grammar, roles, nulls | authored_negative_expectation | schema |
| `V-FAM-GRAM-MISSING` | 5 grammar, roles, nulls | authored_negative_expectation | schema |
| `V-FAM-GRAM-U00` | 5 grammar, roles, nulls | authored_negative_expectation | noncanonical |
| `V-FAM-GRAM-B64PAD` | 5 grammar, roles, nulls | authored_negative_expectation | noncanonical |
| `V-FAM-GRAM-NULL-ABSENT` | 5 grammar, roles, nulls | authored_negative_expectation | malformed |
| `V-FAM-GRAM-ROLE` | 5 grammar, roles, nulls | authored_negative_expectation | binding |
| `V-FAM-GRAM-DOMAIN` | 5 grammar, roles, nulls | authored_negative_expectation | domain |
| `V-FAM-POL-RESTORE` | 6 policy and minima | authored_positive_fixture | restore proceeds |
| `V-FAM-POL-CAP-REHASH` | 6 policy and minima | authored_negative_expectation | conflict; image caps not adopted |
| `V-FAM-POL-GEN-LOW` | 6 policy and minima | authored_negative_expectation | regression |
| `V-FAM-POL-EQFORK` | 6 policy and minima | authored_negative_expectation | fork |
| `V-FAM-POL-FILTER` | 6 policy and minima | authored_negative_expectation | policy_unknown |
| `V-FAM-POL-WRONG-AUTH` | 6 policy and minima | authored_negative_expectation | unauthorized |
| `V-FAM-OP-CONSUME` | 7 operation auth | authored_positive_fixture | op, auth, response, Original and GlobalEntry agree |
| `V-FAM-OP-TENANT` | 7 operation auth | authored_negative_expectation | unauthorized; no body |
| `V-FAM-OP-OWNER` | 7 operation auth | authored_negative_expectation | unauthorized |
| `V-FAM-OP-HOLDER` | 7 operation auth | authored_negative_expectation | unauthorized |
| `V-FAM-OP-READ-FOREIGN` | 7 operation auth | authored_negative_expectation | unauthorized; no body |
| `V-FAM-PROOF-EMPTY` | 8 proofs | authored_positive_fixture | empty authenticated genesis head |
| `V-FAM-PROOF-ONE` | 8 proofs | authored_positive_fixture | one-leaf root equals the leaf hash |
| `V-FAM-PROOF-THREE` | 8 proofs | authored_positive_fixture | three-leaf root; every known prefix recomputes |
| `V-FAM-PROOF-ABA` | 8 proofs | authored_positive_fixture | A positions 0 and 2 scoped-complete without adjacency; B body not returned |
| `V-FAM-PROOF-REORDER` | 8 proofs | authored_negative_expectation | fork or incomplete |
| `V-FAM-PROOF-COUNT` | 8 proofs | authored_negative_expectation | incomplete |
| `V-FAM-PROOF-OFFSET` | 8 proofs | authored_negative_expectation | incomplete |
| `V-FAM-PROOF-LAST` | 8 proofs | authored_negative_expectation | binding |
| `V-FAM-PAGE-SET` | 9 pages | authored_positive_fixture | three pages chain; page root and manifest recompute |
| `V-FAM-PAGE-OMIT-FIRST` | 9 pages | authored_negative_expectation | incomplete |
| `V-FAM-PAGE-OMIT-MID` | 9 pages | authored_negative_expectation | incomplete |
| `V-FAM-PAGE-OMIT-FINAL` | 9 pages | authored_negative_expectation | incomplete |
| `V-FAM-PAGE-OMIT-NEG` | 9 pages | authored_negative_expectation | incomplete |
| `V-FAM-PAGE-MIXED` | 9 pages | authored_negative_expectation | fork |
| `V-FAM-TIME-109` | 10 time | authored_positive_fixture | admit |
| `V-FAM-TIME-110` | 10 time | authored_negative_expectation | expired; no consume |
| `V-FAM-TIME-111` | 10 time | authored_negative_expectation | expired; no consume |
| `V-FAM-TIME-COMMIT200` | 10 time | authored_schedule_fixture | commit allowed under unchanged revision and term; label post_sign_admission_only; not freshness at 200 |
| `V-FAM-TIME-WALL` | 10 time | authored_negative_expectation | same outcome as V-FAM-TIME-109 |
| `V-FAM-TIME-XDOMAIN` | 10 time | authored_negative_expectation | domain |
| `V-FAM-TIME-FENCE` | 10 time | authored_negative_expectation | stale_writer |
| `V-FAM-TIME-LOST-CONFIRM` | 10 time | authored_schedule_fixture | commit_unknown; no provisional bytes; reconcile finds the winner |
| `V-FAM-TIME-RESTORE` | 10 time | authored_schedule_fixture | exact original after restore; divergent loser conflict |
| `V-FAM-QUOTA-RECORDS-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-RECORDS-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-STOREDBYTES-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-STOREDBYTES-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-OUTSTANDING-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-OUTSTANDING-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-RETRYATTEMPTS-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-RETRYATTEMPTS-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-SIGNERCALLS-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-SIGNERCALLS-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-STORECALLS-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-STORECALLS-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-DENIALRECORDS-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-DENIALRECORDS-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-CALLS-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-CALLS-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-RETAINEDPAGES-CAP` | 11 quotas | authored_positive_fixture | charge reaches the synthetic cap |
| `V-FAM-QUOTA-RETAINEDPAGES-OVER` | 11 quotas | authored_negative_expectation | quota; latch unavailable for cumulative; no eviction |
| `V-FAM-QUOTA-REQUESTINPUTBYTES-CAP` | 11 quotas | authored_positive_fixture | accepted at synthetic cap |
| `V-FAM-QUOTA-REQUESTINPUTBYTES-OVER` | 11 quotas | authored_negative_expectation | oversize; no commit |
| `V-FAM-QUOTA-NEG-RESERVE` | 11 quotas | authored_schedule_fixture | unavailable; first_break and earliest denial kept |
| `V-FAM-QUOTA-RESTORE` | 11 quotas | authored_schedule_fixture | serialized restore with independent basis recovers; counters not reset |
| `V-FAM-QUOTA-NOBASIS` | 11 quotas | authored_schedule_fixture | unavailable; no grant |
| `V-FAM-CLOSE-SPLIT` | 12 close and holds | authored_positive_fixture | opening [0,MAX-2] then one-row close [MAX-1,MAX-1]; total_rows MAX |
| `V-FAM-CLOSE-ZERO` | 12 close and holds | authored_negative_expectation | incomplete; zero-new-row close denied |
| `V-FAM-CLOSE-ENDMAX` | 12 close and holds | authored_negative_expectation | overflow |
| `V-FAM-CLOSE-SUCC` | 12 close and holds | authored_negative_expectation | binding |
| `V-FAM-CLOSE-GROWTH` | 12 close and holds | authored_negative_expectation | closed |
| `V-FAM-CLOSE-NEAR` | 12 close and holds | authored_positive_fixture | named near-limit genesis offset MAX-1 admits one leaf; next append exhausts |
| `V-FAM-HOLD-TERM` | 12 close and holds | authored_negative_expectation | BLOCKED; no success path; retirement_disabled |
| `V-FAM-HOLD-WRONG` | 12 close and holds | authored_negative_expectation | retirement_disabled or unauthorized; hold stays |
| `V-FAM-HOLD-PAUSED` | 12 close and holds | authored_negative_expectation | retirement_disabled or unauthorized; hold stays |
| `V-FAM-HOLD-UNKNOWN` | 12 close and holds | authored_negative_expectation | retirement_disabled or unauthorized; hold stays |
| `V-FAM-HOLD-RESTORE` | 12 close and holds | authored_schedule_fixture | hold stays held or quarantined; no release |
| `V-FAM-HOLD-TIME` | 12 close and holds | authored_schedule_fixture | hold stays held or quarantined; no release |
| `V-FAM-HOLD-ADMIN` | 12 close and holds | authored_schedule_fixture | hold stays held or quarantined; no release |
| `V-FAM-HOLD-QUOTA` | 12 close and holds | authored_schedule_fixture | hold stays held or quarantined; no release |

## 12. Pins

`tests/anchor_seen_at_p2/vectors/p2-pins.json` is written only by that generator. It has exactly three members: `base`, `observed_file_pins`, and `supplied_pin_assertions`. `supplied_pin_assertions` is a strict parse of the §0 source table only (three cells, unique file name, lowercase 64-hex digest, integer or `UNKNOWN` length); any other hex string in this document is not a pin. It labels the base commit `4d119020cec8c3b6ae55c30c8bb17f5c6ab845ad` as supplied by the operator. It records SHA-256 and byte length of this document, of `gen_vectors.py`, and of `p2-vectors.json` as observed when the generator runs. Because those pins include this document's own SHA-256, any later edit to this document requires regenerating the pins. The §0 source table is copied through as supplied pins, not re-hashed. This section does not restate those digests.

## 13. C1 scan status

The qualified counts-only C1 scan is run by the operator on the final committed bytes, commit message and candidate PR text; results (labels/counts/coverage/exits only, never patterns or matches) are reported in review evidence outside this file, because a file cannot carry a scan of its own final bytes. In this file the status is "not stated here; see review evidence".

## 14. UNMEASURED

Nothing in this list was measured for this candidate. Synthetic `LimitSet` integers, clock samples, and fixture ids are inputs, not observations and not defaults. The supplied pair of a 61295-byte recovery image against a 16384-byte request-input ceiling is an invalid example from the handoff, not a measurement and not a chosen cap.

- Round-trip time, enqueue-to-arrival delay, durable-resolution delay, deadline-miss and loss rates, and any full denominator of unsent or failed attempts.
- Lifetimes, opening and close cadence, retry and backoff, fault windows, and any admission-to-commit or publication gap.
- Capture, frame, hook, drop, exclusion, and acquisition timing.
- CPU, memory, queue, signer, and store load.
- Real parser, byte, rate, outstanding, inflight, concurrent, page, and proof capacity. The schema fields are not measured limits.
- Stable-image commit, restore, reconciliation, RPO, and RTO. Model bytes are not database, WAL, fsync, filesystem, Windows, replica, or power-loss evidence.
- Real entropy, auth, custody, holder separation, and clock continuity.
- Current-policy response, use, and revocation freshness, and both final integrations.
- Live feature and runtime matrices, practice submit, upload, gold, and controller-output denials as executed checks.
- Cost, spend, retention, deletion, and backup behavior.
- C1–C7 and M1–M17 runs, doctest counts, and scanner counts. The C1 result is not stated in this file. It is review evidence from the operator's scan of the final committed bytes. Absence of a count here is not zero findings.
- Fresh verification of the supplied base commit by this writer.

## 15. Review gate

This candidate is not frozen and is not PASS. Vectors and pins are written by `gen_vectors.py` into `p2-vectors.json` and `p2-pins.json`. Those computed rows are encoding facts only. Model outcomes are NOT EXECUTED. Digests are not hand-typed. The C1 scan is run by the operator on the final committed bytes and is reported in review evidence outside this file. It is not stated here, and this file does not claim zero findings. No model code, feature flag, or test target is added.

Next gate, in order:

1. QorData acceptance reconciliation of this exact document, the generated vectors, and the generated pins.
2. QorProof exact-schema PASS or freeze of that same artifact.
3. Only after that gate, offline model code, under the allowlist and a later review. The C1 evidence stays outside this file.

Stop rather than self-certify. Terminal-positive stays BLOCKED. Holder release stays disabled. Publication-time freshness is not asserted. REAL MATCH stays v0.3.38. Practice does not submit, upload, or write gold. Match 201029 stays VOID.

Acceptance ids are not renumbered. Each row is NOT EXECUTED. A section citation is where the schema rule lives, not evidence that the case ran.

| ID | Schema rule in this candidate | Status |
|---|---|---|
| C1 | privacy and wording; scan not stated here (§13) | NOT EXECUTED |
| C2 | REAL MATCH unwired (§1) | NOT EXECUTED |
| C3 | no gold, submit, or upload; frozen receipt and contracts (§1) | NOT EXECUTED |
| C4 | only `unknown`, `seen_at_late`, `seen_at_retained` (§1, §7) | NOT EXECUTED |
| C5 | sticky break, 201029 VOID, controller read-only (§1, §9) | NOT EXECUTED |
| C6 | original deadline and negative history (§3, §7, §9) | NOT EXECUTED |
| C7 | feature not added; no enablement follows (§1, §15) | NOT EXECUTED |
| M1 | canonical preimages and mutations (§2, §3) | NOT EXECUTED |
| M2 | scope and archive denial (§4, §5) | NOT EXECUTED |
| M3 | one winner, conflict loser (§3, §6) | NOT EXECUTED |
| M4 | admission-only expiry (§1, §6) | NOT EXECUTED |
| M5 | crash before and after commit (§6.3) | NOT EXECUTED |
| M6 | no partial commit; database durability is out of scope (§6) | NOT EXECUTED |
| M7 | new epoch invalidates unused challenges (§5.4) | NOT EXECUTED |
| M8 | missing basis is `unknown` (§5, §7) | NOT EXECUTED |
| M9 | archive does not grant new eligibility (§4, §9) | NOT EXECUTED |
| M10 | holder denial; terminal success BLOCKED (§4, §7.2) | NOT EXECUTED |
| M11 | fork and sticky unknown (§9); close split (§2) | NOT EXECUTED |
| M12 | range and counter bounds (§2, §6) | NOT EXECUTED |
| M13 | separate caps, no eviction (§6) | NOT EXECUTED |
| M14 | lost client arrival stays missing (§3, §7.9) | NOT EXECUTED |
| M15 | immutable origin (§9) | NOT EXECUTED |
| M16 | policy generation and digest (§5) | NOT EXECUTED |
| M17 | defaults off, frozen surface (§1, §15) | NOT EXECUTED |

Live finals, a current-policy service, and platform suites are outside this cut and are not executed by naming them. M10's positive terminal case is BLOCKED, not passed.
