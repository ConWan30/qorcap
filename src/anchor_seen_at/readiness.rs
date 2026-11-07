//! Practice readiness gate for this module's own tests (design sections 2.6 and 7.6, isolated).
//!
//! Readiness is conjunctive and withheld by default. A retained seen-at fact is one input; it
//! never supplies capture validity, frame freshness, lag calibration or the exclusion endpoint.
//! Frames are judged by their original acquisition epoch and sequence; a polling time is ignored,
//! so re-reading an old snapshot cannot make it fresh, and relabeled copies of one image count as
//! one frame. The only positive result is a practice-only forward readiness, not a verdict.

use super::coverage::{StreamContext, StreamEligibility};
use super::types::Purpose;
use super::{AnchorState, Reason};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CaptureValidity {
    /// What every seen-at fact reports: the ack does not speak to capture validity.
    NotEstablishedByAck,
    Unknown,
    /// Only a separate capture gate could report this. None is wired in this phase; tests set it
    /// for the synthetic positive control.
    SeparatelyEstablished,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrameObservation {
    pub frame_digest: String,
    pub capture_epoch_id: String,
    pub acquisition_seq: u64,
    /// When a reader polled the frame. Not an acquisition record; ignored by the gate.
    pub polled_at_mono_ns: u64,
}

pub struct ReadinessInputs<'a> {
    /// The caller's presentation of the opening. Readiness derives the authoritative
    /// opening from `eligibility` and withholds unless this matches it exactly.
    pub opening: &'a AnchorState,
    /// Forward-only eligibility state, recorded in arrival order. Never an archival
    /// reconstruction: a reconstructed view can fill ranges whose eligibility was
    /// already lost, and readiness must not see those as eligible.
    pub eligibility: &'a StreamEligibility,
    pub capture_validity: CaptureValidity,
    pub frames: &'a [FrameObservation],
    pub min_distinct_frames: Option<usize>,
    pub exclusion_end_seq: Option<u64>,
    pub hook_lag_measured: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Readiness {
    Withheld { reasons: Vec<Reason> },
    /// Practice-only, forward from the anchored rows. Not a receipt status or verdict.
    PracticeReadyForward,
}

pub fn evaluate(i: &ReadinessInputs) -> Readiness {
    let mut why = BTreeSet::new();
    // The opening is derived from the eligibility state: the opening it actually
    // accepted, with the bound it was accepted under. A caller-supplied opening is
    // only a presentation of that fact and must match its immutable context exactly
    // (ack id, client clock identity, stream and server epoch, chain, commitment and
    // ledger); a rechecked, relabeled, or otherwise substituted opening withholds,
    // even when it shares the stream context.
    let accepted_opening = i.eligibility.accepted_opening();
    let opening = accepted_opening;
    let supplied = i.opening.retained().filter(|f| f.commitment.purpose == Purpose::Opening);
    if supplied.is_none() { why.insert(Reason::AnchorNotRetained); }
    match (supplied, accepted_opening) {
        (Some(s), Some(a)) => {
            if let Some(r) = super::coverage::immutable_context_reason(a, s) { why.insert(r); }
        }
        (Some(_), None) => { why.insert(Reason::AnchorNotRetained); }
        _ => {}
    }
    // The eligibility state must belong to the opening's full stream context. An opening
    // from one stream paired with eligibility from another (same ranges, different
    // stream) is not readiness.
    if let Some(o) = opening {
        if i.eligibility.stream_context() != Some(&StreamContext::of(o)) { why.insert(Reason::BindingMismatch); }
    }
    // A preserved eligibility break withholds readiness even for frames in the eligible
    // prefix: the stream's forward-only eligibility was broken, and no later
    // reconstruction clears that.
    if let Some(r) = i.eligibility.break_reason() { why.insert(r); }
    if i.capture_validity != CaptureValidity::SeparatelyEstablished { why.insert(Reason::CaptureValidityNotEstablished); }
    if i.exclusion_end_seq.is_none() { why.insert(Reason::ExclusionUnresolved); }
    if !i.hook_lag_measured { why.insert(Reason::LagUnmeasured); }

    let mut digest_seqs: BTreeMap<&str, BTreeSet<u64>> = BTreeMap::new();
    let mut seq_digests: BTreeMap<u64, BTreeSet<&str>> = BTreeMap::new();
    for f in i.frames {
        let fresh = opening.is_some_and(|o| o.commitment.capture_epoch_id == f.capture_epoch_id)
            && i.eligibility.is_eligible(f.acquisition_seq)
            && i.exclusion_end_seq.is_some_and(|x| f.acquisition_seq > x);
        if !fresh { why.insert(Reason::StaleFrame); }
        digest_seqs.entry(&f.frame_digest).or_default().insert(f.acquisition_seq);
        seq_digests.entry(f.acquisition_seq).or_default().insert(&f.frame_digest);
    }
    if digest_seqs.values().any(|s| s.len() > 1) || seq_digests.values().any(|d| d.len() > 1) { why.insert(Reason::DuplicateFrame); }
    match i.min_distinct_frames {
        None => { why.insert(Reason::FrameBudgetUnset); }
        Some(n) if digest_seqs.len() < n => { why.insert(Reason::TooFewDistinctFrames); }
        Some(_) => {}
    }
    if why.is_empty() { Readiness::PracticeReadyForward } else { Readiness::Withheld { reasons: why.into_iter().collect() } }
}
