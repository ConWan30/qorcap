//! Practice readiness gate for this module's own tests (design sections 2.6 and 7.6, isolated).
//!
//! Readiness is conjunctive and withheld by default. A retained seen-at fact is one input; it
//! never supplies capture validity, frame freshness, lag calibration or the exclusion endpoint.
//! Frames are judged by their original acquisition epoch and sequence; a polling time is ignored,
//! so re-reading an old snapshot cannot make it fresh, and relabeled copies of one image count as
//! one frame. The only positive result is a practice-only forward readiness, not a verdict.

use super::coverage::{CoverageView, StreamContext};
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
    pub opening: &'a AnchorState,
    pub coverage: &'a CoverageView,
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
    let opening = i.opening.retained().filter(|f| f.commitment.purpose == Purpose::Opening);
    if opening.is_none() { why.insert(Reason::AnchorNotRetained); }
    // The coverage view must belong to the opening's full stream context. An opening from one
    // stream paired with coverage from another (same ranges, different stream) is not readiness.
    if let Some(o) = opening {
        if i.coverage.stream_context.as_ref() != Some(&StreamContext::of(o)) { why.insert(Reason::BindingMismatch); }
    }
    if i.capture_validity != CaptureValidity::SeparatelyEstablished { why.insert(Reason::CaptureValidityNotEstablished); }
    if i.exclusion_end_seq.is_none() { why.insert(Reason::ExclusionUnresolved); }
    if !i.hook_lag_measured { why.insert(Reason::LagUnmeasured); }

    let mut digest_seqs: BTreeMap<&str, BTreeSet<u64>> = BTreeMap::new();
    let mut seq_digests: BTreeMap<u64, BTreeSet<&str>> = BTreeMap::new();
    for f in i.frames {
        let fresh = opening.is_some_and(|o| o.commitment.capture_epoch_id == f.capture_epoch_id)
            && i.coverage.is_seen_at(f.acquisition_seq)
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
