//! Which source rows have a retained seen-at fact (design section 5).
//!
//! Two different things live here, and they must not be confused:
//!
//! * [`reconstruct_archival`] rebuilds a coverage view from a set of facts, sorted by
//!   commitment index. It is a digest/archival reconstruction only: it pays no attention to
//!   the order facts arrived in, or to unknown observations seen along the way, so a fact
//!   that arrives later can fill a range an earlier reconstruction left unknown. It must
//!   never be used to decide eligibility.
//! * [`StreamEligibility`] is the persistent, forward-only eligibility state. Facts and
//!   unknown/late observations are recorded in arrival order; the first break (loss,
//!   reorder, deadline miss, fork, ledger rollback, restart, rows after close) is kept
//!   forever, and no later fact clears it or makes the rows after it eligible again.
//!   Readiness consumes this state, never the archival reconstruction.
//!
//! In both, acks must form one linked stream from the opening: exact next index, prior ack
//! id, predecessor sequence and head, same session/epochs/chain/seal, strictly increasing
//! service ledger. Identical duplicates count once. Coverage is complete only with a
//! retained close whose prefix ends at the source's last row. The close ack is a trailer
//! outside its own coverage.

use super::canon;
use super::check::SeenAtFact;
use super::types::Purpose;
use super::{AnchorState, Reason, DOMAIN_LEDGER_ENTRY};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

/// Full stream context a coverage view belongs to. Two views (or a view and an opening) with
/// different contexts are different streams, even when their sequence ranges line up.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StreamContext {
    pub session_id: String,
    pub device_epoch_id: String,
    pub capture_epoch_id: String,
    pub seal_hash: String,
    pub chain_id: String,
    pub service: String,
    pub audience: String,
    pub policy_id: String,
    pub trust_policy_id: String,
    pub server_boot_id: String,
    pub server_clock_epoch_id: String,
}

impl StreamContext {
    pub fn of(f: &SeenAtFact) -> Self {
        let c = f.commitment();
        StreamContext {
            session_id: c.session_id.clone(), device_epoch_id: c.device_epoch_id.clone(), capture_epoch_id: c.capture_epoch_id.clone(),
            seal_hash: c.seal_hash.clone(), chain_id: c.chain.chain_id.clone(), service: c.service.clone(), audience: c.audience.clone(),
            policy_id: c.policy_id.clone(), trust_policy_id: f.trust_policy_id().clone(),
            server_boot_id: f.server_boot_id().clone(), server_clock_epoch_id: f.server_clock_epoch_id().clone(),
        }
    }
}

/// Recompute one retention-ledger entry digest from a fact, exactly as the service double
/// appends it: `H(ledger-entry domain, {ledger_seq, ack_id, commitment_digest, prev})`.
pub fn ledger_entry_digest(f: &SeenAtFact) -> Option<String> {
    canon::digest(DOMAIN_LEDGER_ENTRY, &json!({
        "ledger_seq": canon::dec(f.ledger_seq()), "ack_id": f.ack_id(),
        "commitment_digest": f.commitment_digest(), "prev": f.prev_ledger_entry_digest(),
    })).ok()
}

/// Number of rows in `first..=last`, saturated at `u64::MAX`: a range ending at
/// `u64::MAX` has an unrepresentable true count, so it reports the largest
/// representable count instead of wrapping to zero.
fn row_count(first_seq: u64, last_seq: u64) -> u64 {
    last_seq.saturating_sub(first_seq).saturating_add(1)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RangeStatus { SeenAtRetained { ack_id: String }, Unknown(Reason) }

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Segment { pub first_seq: u64, pub last_seq: u64, pub status: RangeStatus }

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoverageView { pub segments: Vec<Segment>, pub complete: bool, pub stream_context: Option<StreamContext> }

impl CoverageView {
    pub fn status_at(&self, seq: u64) -> Option<&RangeStatus> {
        self.segments.iter().find(|s| s.first_seq <= seq && seq <= s.last_seq).map(|s| &s.status)
    }
    pub fn is_seen_at(&self, seq: u64) -> bool { matches!(self.status_at(seq), Some(RangeStatus::SeenAtRetained { .. })) }
    pub fn unknown_rows(&self) -> u64 {
        self.segments.iter().filter(|s| matches!(s.status, RangeStatus::Unknown(_)))
            .fold(0u64, |n, s| n.saturating_add(row_count(s.first_seq, s.last_seq)))
    }
}

/// Persistent, forward-only eligibility for one stream. See the module docs: this is the
/// only state readiness may consume. Once [`StreamEligibility::record`] observes a break,
/// the break (and the first ineligible sequence) is preserved permanently; later facts are
/// still checkable archivally elsewhere, but they never clear it here.
///
/// Deliberately not `Clone`: a clone taken before a break could be carried forward and
/// completed independently while the original stays broken, forking the break lineage.
/// Eligibility has one owner and one history.
///
/// Permanence holds only within a preserved object. [`StreamEligibility::new`] and
/// [`StreamEligibility::from_arrival_order`] start with no history: they cannot know
/// about unknown/late observations that were seen before they were created, and
/// constructing a fresh object mid-stream does not resurrect eligibility. A caller
/// that cannot present the preserved object for a stream must treat that stream's
/// history as missing — and missing history reads UNKNOWN, never eligible.
#[derive(Debug, PartialEq, Eq)]
pub struct StreamEligibility {
    eligible: Vec<Segment>,
    stream_context: Option<StreamContext>,
    /// Each accepted ack, with the source bound it was accepted under. Kept so a later
    /// observation carrying the same ack id can be compared against the accepted fact's
    /// full immutable context before it is treated as an idempotent retry.
    accepted: BTreeMap<String, (SeenAtFact, u64)>,
    /// The accepted opening fact and the source bound it was accepted under. Readiness
    /// derives its opening from here, never from a caller-supplied substitute.
    opening: Option<(SeenAtFact, u64)>,
    prev: Option<SeenAtFact>,
    next_index: u64,
    next_seq: u64,
    source_last_seq: u64,
    break_reason: Option<Reason>,
    closed: bool,
    /// The last row sealed by the accepted close, once one is accepted.
    sealed_last_seq: Option<u64>,
}

impl Default for StreamEligibility {
    fn default() -> Self { Self::new() }
}

impl StreamEligibility {
    /// A fresh eligibility state with no recorded history. See the type docs: this is
    /// only a starting point for recording a stream from its first observation. It is
    /// not a way to restart a stream whose earlier observations are unavailable — that
    /// history is missing, and missing history reads UNKNOWN.
    pub fn new() -> Self {
        StreamEligibility {
            eligible: vec![], stream_context: None, accepted: BTreeMap::new(), opening: None, prev: None,
            next_index: 0, next_seq: 0, source_last_seq: 0, break_reason: None, closed: false,
            sealed_last_seq: None,
        }
    }

    /// Build eligibility by recording observations in the exact order they arrived.
    /// This deliberately does not sort: arrival order is part of the evidence. Only the
    /// observations passed here exist for the new object; any earlier observation the
    /// caller failed to preserve is missing history and must be supplied as an explicit
    /// unknown observation, not silently restarted over.
    pub fn from_arrival_order(states: &[AnchorState], source_last_seq: u64) -> Self {
        let mut e = Self::new();
        for s in states { e.record(s, source_last_seq); }
        e
    }

    fn break_at(&mut self, reason: Reason) {
        if self.break_reason.is_none() { self.break_reason = Some(reason); }
    }

    /// Record one observation in arrival order: a retained fact, a late fact (a deadline
    /// miss), or an unknown observation (including the observed loss of an expected ack).
    pub fn record(&mut self, state: &AnchorState, source_last_seq: u64) {
        // A source-bound regression is rejected before anything else — in particular
        // before duplicate-ack deduplication, so a duplicate arriving with a lowered
        // bound cannot quietly shrink the stream. It latches a sticky break and the
        // previously recorded (higher) bound is kept.
        if source_last_seq < self.source_last_seq {
            self.break_at(Reason::SourceBoundRegression);
            return;
        }
        // Any extension of the bound past the sealed endpoint is rows after the close,
        // whatever observation carries it — including a duplicate that deduplication
        // would otherwise swallow. Checked before dedup; the grown bound is kept so
        // the rows past the seal read unknown.
        if let Some(sealed) = self.sealed_last_seq {
            if source_last_seq > sealed {
                self.source_last_seq = source_last_seq;
                self.break_at(Reason::RowsAfterClose);
                return;
            }
        }
        self.source_last_seq = source_last_seq;
        if self.break_reason.is_some() { return; }
        match state {
            AnchorState::Unknown { reasons } => {
                self.break_at(reasons.first().copied().unwrap_or(Reason::Gap));
            }
            AnchorState::SeenAtLate(_) => self.break_at(Reason::LateAck),
            AnchorState::SeenAtRetained(f) if f.is_late() => self.break_at(Reason::LateAck),
            AnchorState::SeenAtRetained(f) => {
                // A same-ack-id observation is an idempotent retry only if its full
                // immutable context is identical to the accepted fact's: client clock
                // identity, the source bound it was accepted under, the stream and
                // server epoch, and the chain/commitment/ledger it carries. Any
                // difference is not a retry; it latches a sticky break. This runs
                // before dedup, so a recheck under a changed clock cannot hide here.
                if let Some((accepted, accepted_bound)) = self.accepted.get(f.ack_id()) {
                    if let Some(r) = duplicate_context_reason(accepted, *accepted_bound, f, source_last_seq) {
                        self.break_at(r);
                    }
                    return;
                }
                if self.closed { self.break_at(Reason::RowsAfterClose); return; }
                if f.commitment().index() != self.next_index {
                    // A fact that is not the exact next one breaks eligibility in arrival
                    // order: skipping the expected index is an observed loss, and any
                    // other out-of-order arrival is a reorder. Neither is recoverable.
                    let reason = if f.commitment().index() > self.next_index && self.prev.is_some() {
                        Reason::Gap
                    } else {
                        Reason::Reordered
                    };
                    self.break_at(reason);
                    return;
                }
                if let Some(r) = self.prev.as_ref().and_then(|p| link_reason(p, f)) { self.break_at(r); return; }
                if f.chain_last() > source_last_seq { self.break_at(Reason::BindingMismatch); return; }
                if let Some(c) = &f.commitment().close {
                    if super::canon::dec_u64(&c.total_rows) != f.chain_last().checked_add(1) {
                        self.break_at(Reason::SchemaViolation);
                        return;
                    }
                }
                if f.commitment().chain.first() > self.next_seq { self.break_at(Reason::Gap); return; }
                if f.commitment().chain.first() < self.next_seq { self.break_at(Reason::Fork); return; }
                // A range ending at u64::MAX has no representable successor row; it
                // fails closed as unknown instead of overflowing into the next state.
                // (Validation rejects such ranges, so a checked fact cannot carry one;
                // this keeps hand-assembled facts from panicking or wrapping here.)
                let Some(next_seq) = f.chain_last().checked_add(1) else {
                    self.break_at(Reason::SchemaViolation);
                    return;
                };
                self.eligible.push(Segment {
                    first_seq: f.commitment().chain.first(), last_seq: f.chain_last(),
                    status: RangeStatus::SeenAtRetained { ack_id: f.ack_id().clone() },
                });
                if self.stream_context.is_none() { self.stream_context = Some(StreamContext::of(f)); }
                self.accepted.insert(f.ack_id().clone(), (f.clone(), source_last_seq));
                if f.commitment().index() == 0 { self.opening = Some((f.clone(), source_last_seq)); }
                self.closed = f.commitment().purpose == Purpose::Close;
                if self.closed {
                    self.sealed_last_seq = Some(f.chain_last());
                    // The bound already extends past the endpoint this close seals:
                    // those rows are after the close.
                    if source_last_seq > f.chain_last() { self.break_at(Reason::RowsAfterClose); }
                }
                self.next_index = self.next_index.saturating_add(1);
                self.next_seq = next_seq;
                self.prev = Some(f.clone());
            }
        }
    }

    pub fn break_reason(&self) -> Option<Reason> { self.break_reason }
    pub fn stream_context(&self) -> Option<&StreamContext> { self.stream_context.as_ref() }
    /// The opening this state actually accepted (index 0), derived from the recorded
    /// stream — the only opening readiness may anchor to.
    pub fn accepted_opening(&self) -> Option<&SeenAtFact> { self.opening.as_ref().map(|(f, _)| f) }
    /// The source bound the accepted opening was recorded under.
    pub fn accepted_opening_bound(&self) -> Option<u64> { self.opening.as_ref().map(|(_, b)| *b) }

    /// Eligible prefix plus, when anything up to the source's last row is not eligible,
    /// one trailing unknown segment carrying the preserved break (or the reason nothing
    /// eligible follows yet).
    pub fn segments(&self) -> Vec<Segment> {
        let mut out = self.eligible.clone();
        if self.next_seq <= self.source_last_seq {
            let reason = self.break_reason.unwrap_or(if self.closed {
                Reason::RowsAfterClose
            } else if self.prev.is_none() {
                Reason::NoAck
            } else {
                Reason::MissingClose
            });
            out.push(Segment { first_seq: self.next_seq, last_seq: self.source_last_seq, status: RangeStatus::Unknown(reason) });
        }
        out
    }

    pub fn status_at(&self, seq: u64) -> Option<RangeStatus> {
        self.segments().iter().find(|s| s.first_seq <= seq && seq <= s.last_seq).map(|s| s.status.clone())
    }
    pub fn is_eligible(&self, seq: u64) -> bool {
        matches!(self.status_at(seq), Some(RangeStatus::SeenAtRetained { .. }))
    }
    pub fn unknown_rows(&self) -> u64 {
        self.segments().iter().filter(|s| matches!(s.status, RangeStatus::Unknown(_)))
            .fold(0u64, |n, s| n.saturating_add(row_count(s.first_seq, s.last_seq)))
    }
    pub fn complete(&self) -> bool {
        self.break_reason.is_none() && self.closed && self.next_seq == self.source_last_seq.checked_add(1).unwrap_or(u64::MAX)
            && !self.eligible.is_empty()
    }
}

/// Why a same-ack-id observation is not an idempotent retry of the accepted fact, or
/// `None` when it is one. Volatile receipt timing (`received_*`, `rtt_ns`) is not part
/// of the immutable context; everything that identifies what was seen, by whom, under
/// which clocks and epochs, and under what source bound, is.
/// Why `f` does not carry the same immutable context as the accepted fact, or `None`
/// when it does. This is the comparison any purported copy of an accepted fact must
/// pass — a duplicate observation in [`StreamEligibility::record`], or the opening a
/// caller presents to readiness. Volatile receipt timing is excluded (see above).
pub(crate) fn immutable_context_reason(accepted: &SeenAtFact, f: &SeenAtFact) -> Option<Reason> {
    if f.ack_id() != accepted.ack_id() {
        return Some(Reason::BindingMismatch);
    }
    if f.client_boot_id() != accepted.client_boot_id() || f.client_clock_epoch_id() != accepted.client_clock_epoch_id() {
        return Some(Reason::ClientClockDomainChanged);
    }
    if f.server_boot_id() != accepted.server_boot_id() || f.server_clock_epoch_id() != accepted.server_clock_epoch_id() {
        return Some(Reason::ServerEpochMismatch);
    }
    if StreamContext::of(f) != StreamContext::of(accepted) {
        return Some(Reason::BindingMismatch);
    }
    if f.commitment().chain != accepted.commitment().chain {
        return Some(Reason::Fork);
    }
    if f.commitment() != accepted.commitment() || f.commitment_digest() != accepted.commitment_digest()
        || f.ack_key_id() != accepted.ack_key_id() || f.challenge_key_id() != accepted.challenge_key_id()
        || f.ledger_seq() != accepted.ledger_seq() || f.prev_ledger_entry_digest() != accepted.prev_ledger_entry_digest() {
        return Some(Reason::BindingMismatch);
    }
    None
}

fn duplicate_context_reason(accepted: &SeenAtFact, accepted_bound: u64, f: &SeenAtFact, source_last_seq: u64) -> Option<Reason> {
    if let Some(r) = immutable_context_reason(accepted, f) { return Some(r); }
    if source_last_seq != accepted_bound {
        // A lowered bound is caught earlier as a regression against the stream bound;
        // reaching here with a different bound means it grew since acceptance.
        return Some(Reason::BindingMismatch);
    }
    None
}

fn link_reason(prev: &SeenAtFact, f: &SeenAtFact) -> Option<Reason> {
    let (p, c) = (prev.commitment(), f.commitment());
    if p.purpose == Purpose::Close { return Some(Reason::RowsAfterClose); }
    // A service restart (new boot or clock epoch) invalidates the stream link: facts from two
    // boots never form one coverage stream, and nothing after the break recovers it.
    if prev.server_boot_id() != f.server_boot_id() || prev.server_clock_epoch_id() != f.server_clock_epoch_id() {
        return Some(Reason::ServerEpochMismatch);
    }
    // Client clock continuity across exchanges: each fact was checked against its own
    // samples, but a stream checked partly under one client clock domain and partly
    // under another has a discontinuity at the join, even if every exchange is
    // internally consistent.
    if prev.client_boot_id() != f.client_boot_id() || prev.client_clock_epoch_id() != f.client_clock_epoch_id() {
        return Some(Reason::ClientClockDomainChanged);
    }
    // Full stream context, not just session and seal.
    let same_stream = StreamContext::of(prev) == StreamContext::of(f)
        && p.chain.chain_schema == c.chain.chain_schema && p.chain.algorithm == c.chain.algorithm;
    if !same_stream { return Some(Reason::BindingMismatch); }
    if c.prior_ack_id.as_deref() != Some(prev.ack_id().as_str()) { return Some(Reason::Fork); }
    if c.chain.predecessor_head != p.chain.end_head || c.chain.predecessor() != Some(p.chain.last()) { return Some(Reason::Fork); }
    // Reorder/overlap at the chain level: the next range must start exactly where the prior ended.
    if p.chain.last().checked_add(1) != Some(c.chain.first()) { return Some(Reason::Fork); }
    if f.ledger_seq() <= prev.ledger_seq() { return Some(Reason::LedgerRollback); }
    // Recomputed ledger linkage where it can be checked: when this fact is the very next
    // ledger entry, it must name the entry the prior fact actually appends. A larger jump
    // means other streams appended in between, whose entries this view does not hold.
    if prev.ledger_seq().checked_add(1) == Some(f.ledger_seq())
        && ledger_entry_digest(prev).as_deref() != Some(f.prev_ledger_entry_digest().as_str()) {
        return Some(Reason::LedgerRollback);
    }
    None
}

/// Archival reconstruction from a set of facts, sorted by commitment index.
/// `source_last_seq` is the last durable local source row the caller holds.
///
/// This ignores arrival order and unknown observations, so it can show a range as
/// seen-at even after eligibility for that range was lost and recovered. It is for
/// digest/archival reporting only and must never feed readiness; use
/// [`StreamEligibility`] for anything that decides eligibility.
pub fn reconstruct_archival(states: &[AnchorState], source_last_seq: u64) -> CoverageView {
    let mut seen = BTreeSet::new();
    let mut by_index: BTreeMap<u64, Vec<&SeenAtFact>> = BTreeMap::new();
    let mut late_index = BTreeSet::new();
    for s in states {
        match s {
            AnchorState::SeenAtRetained(f) if f.is_late() => { late_index.insert(f.commitment().index()); }
            AnchorState::SeenAtRetained(f) if seen.insert(f.ack_id().clone()) => by_index.entry(f.commitment().index()).or_default().push(f),
            AnchorState::SeenAtLate(f) => { late_index.insert(f.commitment().index()); }
            _ => {}
        }
    }
    let mut segments = vec![];
    let mut stream_context: Option<StreamContext> = None;
    let mut next_seq = 0u64;
    let mut prev: Option<&SeenAtFact> = None;
    let mut stop = None;
    for i in 0u64.. {
        if late_index.contains(&i) { stop = Some(Reason::LateAck); break; }
        let f = match by_index.get(&i).map(Vec::as_slice) {
            None | Some([]) => {
                let closed = prev.is_some_and(|p| p.commitment().purpose == Purpose::Close);
                stop = if late_index.contains(&i) { Some(Reason::LateAck) }
                    else if by_index.range(i..).next().is_some() { Some(Reason::Gap) }
                    else if prev.is_none() { Some(Reason::NoAck) }
                    else if closed { None } else { Some(Reason::MissingClose) };
                break;
            }
            Some([f]) => *f,
            Some(_) => { stop = Some(Reason::Fork); break; }
        };
        if let Some(r) = prev.and_then(|p| link_reason(p, f)) { stop = Some(r); break; }
        if f.chain_last() > source_last_seq { stop = Some(Reason::BindingMismatch); break; }
        if let Some(c) = &f.commitment().close {
            if super::canon::dec_u64(&c.total_rows) != f.chain_last().checked_add(1) { stop = Some(Reason::SchemaViolation); break; }
        }
        // No representable successor row: fail closed with the range left unknown
        // rather than overflowing (validation rejects such ranges upstream).
        let Some(next) = f.chain_last().checked_add(1) else { stop = Some(Reason::SchemaViolation); break; };
        if f.commitment().chain.first() > next_seq {
            segments.push(Segment { first_seq: next_seq, last_seq: f.commitment().chain.first() - 1, status: RangeStatus::Unknown(Reason::Gap) });
        }
        segments.push(Segment { first_seq: f.commitment().chain.first(), last_seq: f.chain_last(), status: RangeStatus::SeenAtRetained { ack_id: f.ack_id().clone() } });
        if stream_context.is_none() { stream_context = Some(StreamContext::of(f)); }
        next_seq = next;
        prev = Some(f);
    }
    if next_seq <= source_last_seq {
        let closed = prev.is_some_and(|p| p.commitment().purpose == Purpose::Close);
        let r = if closed && stop.is_none() { Reason::RowsAfterClose } else { stop.unwrap_or(Reason::MissingClose) };
        segments.push(Segment { first_seq: next_seq, last_seq: source_last_seq, status: RangeStatus::Unknown(r) });
    }
    let closed_at_end = stop.is_none() && prev.is_some_and(|p| p.commitment().purpose == Purpose::Close && p.chain_last() == source_last_seq);
    let complete = closed_at_end && segments.iter().all(|s| matches!(s.status, RangeStatus::SeenAtRetained { .. }));
    CoverageView { segments, complete, stream_context }
}

impl SeenAtFact {
    fn chain_last(&self) -> u64 { self.commitment().chain.last() }
}
