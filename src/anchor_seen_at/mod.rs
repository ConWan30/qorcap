//! PRACTICE-ONLY, disabled by default (cargo feature `practice-anchor-seen-at`).
//!
//! Track 1 seen-at acknowledgment, phase 1: types, canonical preimages, key policy, the typed
//! `AnchorEvidenceV1` envelope and an in-process service double. Nothing outside this module and
//! its own tests reaches it: it is not a receipt field, not a verdict input, not a match path, and
//! it never submits anything.
//!
//! What a valid acknowledgment means, and nothing more: the referee service saw this commitment
//! (a declared digest) at this time, in the service's own clock domain, and says it retained it.
//! It does not say the committed capture is genuine, complete, timely or played by anyone in
//! particular. A host can commit fabricated bytes and still get a valid acknowledgment, so a
//! seen-at fact never makes a capture valid; see [`readiness`].
//!
//! Fail closed: anything missing, malformed, untrusted, late or unverifiable maps to
//! [`AnchorState::Unknown`] (or a late fact that readiness ignores), and readiness stays withheld.
//! No state here is a pass, a match status or a referee verdict.
//!
//! All schema, endpoint and field names are the design's proposals, not an existing service.

pub mod canon;
pub mod check;
pub mod coverage;
pub mod envelope;
pub mod keys;
pub mod local_store;
pub mod readiness;
pub mod service_double;
pub mod types;

use serde::Serialize;

/// Label carried by every artifact this module builds.
pub const PRACTICE_ONLY: &str = "practice-only: auxiliary seen-at evidence, not a receipt field or verdict input";
/// What a valid acknowledgment establishes. Kept next to the code so callers cannot round it up.
pub const ACK_MEANING: &str = "the referee service saw this commitment at this time (service clock); capture validity is not established by it";

pub const DOMAIN_CHALLENGE: &str = "QOR-AUX-CHALLENGE-v1";
pub const DOMAIN_ACK: &str = "QOR-AUX-SEENAT-ACK-v1";
pub const DOMAIN_COMMITMENT: &str = "QOR-AUX-COMMITMENT-v1";
pub const DOMAIN_ACK_ID: &str = "QOR-AUX-ACK-ID-v1";
pub const DOMAIN_SEAL: &str = "QOR-SEAL-v1";
/// Not named in the design; used only by the service double's retention ledger.
pub const DOMAIN_LEDGER_ENTRY: &str = "QOR-AUX-LEDGER-ENTRY-v1";

pub const ALG_ED25519: &str = "Ed25519";

/// Why something stayed unknown or withheld. Serialized as snake_case reason codes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Reason {
    NoAck,
    Oversized,
    Malformed,
    NumberNotAllowed,
    NonCanonical,
    SchemaViolation,
    FieldFormat,
    WrongSchema,
    BindingMismatch,
    DigestMismatch,
    AckIdMismatch,
    UnknownKey,
    KeyScopeMismatch,
    KeyServiceMismatch,
    AlgorithmMismatch,
    PolicyMismatch,
    KeyRevoked,
    KeyRetiredNotArchived,
    BadSignature,
    ChallengeExpired,
    ServerEpochMismatch,
    ClientClockDomainChanged,
    LocalOrdering,
    NegativeRtt,
    TimingBudgetUnset,
    OverBudget,
    TransportFailed,
    PayloadCount,
    UnsupportedKind,
    UnknownKind,
    EnvelopeMismatch,
    Gap,
    Fork,
    Reordered,
    LedgerRollback,
    RowsAfterClose,
    MissingClose,
    LateAck,
    PersistFailed,
    AnchorNotRetained,
    CaptureValidityNotEstablished,
    ExclusionUnresolved,
    LagUnmeasured,
    StaleFrame,
    DuplicateFrame,
    TooFewDistinctFrames,
    FrameBudgetUnset,
    SourceBoundRegression,
}

impl Reason {
    pub fn code(&self) -> String {
        serde_json::to_value(self).ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_default()
    }
}

/// Result of checking one acknowledgment. There is no pass state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AnchorState {
    /// Missing, malformed, untrusted or unverifiable. Never treated as seen-at.
    Unknown { reasons: Vec<Reason> },
    /// Cryptographically consistent but outside the timing budget: retained as late evidence,
    /// unusable for readiness, and it never clears earlier play.
    SeenAtLate(check::SeenAtFact),
    /// The service saw this commitment at this time. Says nothing about capture validity.
    SeenAtRetained(check::SeenAtFact),
}

impl Default for AnchorState {
    fn default() -> Self { AnchorState::Unknown { reasons: vec![Reason::NoAck] } }
}

impl AnchorState {
    pub fn unknown(r: Reason) -> Self { AnchorState::Unknown { reasons: vec![r] } }
    pub fn status_code(&self) -> &'static str {
        match self {
            AnchorState::Unknown { .. } => "unknown",
            AnchorState::SeenAtLate(_) => "seen_at_late",
            AnchorState::SeenAtRetained(_) => "seen_at_retained",
        }
    }
    pub fn reasons(&self) -> Vec<Reason> {
        match self {
            AnchorState::Unknown { reasons } => reasons.clone(),
            AnchorState::SeenAtLate(_) => vec![Reason::LateAck],
            AnchorState::SeenAtRetained(_) => vec![],
        }
    }
    pub fn retained(&self) -> Option<&check::SeenAtFact> {
        match self { AnchorState::SeenAtRetained(f) => Some(f), _ => None }
    }
}
