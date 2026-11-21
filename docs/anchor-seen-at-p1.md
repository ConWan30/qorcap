# Local seen-at lineage: P1 boundaries

This optional module is compiled only with `practice-anchor-seen-at`, which remains off by default. No runtime, worker, endpoint, or final-match path calls it. It does not submit, upload captures, produce output, or change the frozen receipt format.

## Local ownership and ordering

`DurableLineage::create` exclusively creates a new journal. Its non-Clone owner retains immutable expected exchanges, the first arrival bytes and timing, pinned public policy, and the actual accepted opening. Exchange and policy-observation handles belong to that owner. Public getters expose immutable views only. Inputs before minting remain untrusted host-origin claims; safe-API opacity supplies neither trusted clocks nor trusted source data.

Each event extends a hash-linked append-only journal; append uses `write_all`, `flush`, and `sync_all`. Root creation also syncs the parent directory on Unix. An append, sequence, or storage failure cannot yield a successful local check. The resolution includes the original deadline; a crossing during durability makes the checked fact late internally as well as externally. Unresolved registered originals withhold a decision check; an observed original deadline with no response latches missing history. Identical retry retains the original classification; changed timing, bytes, context, or expectation cannot replace it. Source regressions and post-close growth still reach the existing sticky coverage guards before duplicate acceptance.

This is local file persistence, not independently operated retention. Filesystem/platform power-loss behavior has not been certified. There is no restore, reopen, import, truncation, reset, or filtered-history recovery API. Hash links alone do not establish absence of deletion or rollback. A fresh object is not authority to recover missing historical eligibility. Host administrators and arbitrary filesystem replacement are outside this local ownership boundary. Restart reconciliation and exclusive durable cross-process ownership remain later work.

## Selected time claim

The opaque diagnostic `DecisionTimeRecord` says exactly **checked at decision time**. The method samples process-local `Instant`, applies a strict age limit against a same-owner latest-generation policy observation, then appends its result. Equality at the age boundary is expired. An unset limit, missing opening, failed history, or stale generation fails closed.

The observation is a local host policy claim, not an authenticated remote current-policy response. The record is not a live final-publication or egress capability. Expiry or an arbitrary pause after the sample, including during the journal append, is not bounded. There is no freshness assertion at a later CAS, storage commit, or publication; there is no such service in P1. Process-local offsets are not restart-stable time and must not be subtracted from server clock values. Historical decisions never clear an old gap, late result, or unknown history.

## Validation and deferred work

External negative compilation checks require the expected private-field, construction, mutation, Default, deserialization, conversion, and Clone errors; positive external getters must compile. Doctests independently cover the public boundary. Runtime tests cover original-context retention, exact and changed retries, policy generation, missing history, deadline resolution, clock/source continuity, terminal growth, and MAX boundaries. Synthetic fixture limits are not measured budgets.

All deployment and capture budgets remain UNMEASURED. Authenticated currentness, network transport, fenced publication, independent retention, restoration, and production clock/source trust are deliberately absent. No keys or services are provisioned. Existing real-mode wiring, defaults, referee behavior, and frozen contracts are unchanged. Broader cross-platform and external-verifier suites must be reported separately from the scoped local tests.

## Explicit filesystem and restart ceiling

The filesystem check is a lexical whole-file exception for `p1.rs`, not I/O capability confinement or a sandbox. The caller must choose trusted local parents and keep synchronous storage off capture. Parent symlink resolution and parent rename/replacement races are not confined by this API; reopening the textual parent for Unix directory sync may address a replaced directory. Exclusive leaf creation is not a namespace ownership fence.

An error can leave a newly created empty, partial or complete file. No rollback cleanup is promised. Windows directory durability and ACL behavior are unqualified; Unix creation mode is not a Windows permission guarantee. There is no recovery reader or cross-process fence. A fresh path can replay public material and produce new local diagnostics unaware of a dropped owner's prior failure. That expected local success neither repairs old history nor supplies final authority. Future authority requires separate complete-history reconciliation; missing history remains UNKNOWN there.

Deterministic unit schedules may advance a test-only elapsed sample across real file-sync calls. They exercise the two deadline branches with matched predeadline controls, not physical disk latency, crash recovery or power-loss behavior. Near-limit counter setup is private synthetic test state, not a complete imported journal. No measured budget or broader final-path acceptance follows.
