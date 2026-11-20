"""glimpse-lab: post-match, sandboxed, observer-only self-improvement loop for Elorian Glimpse in QorCap.

Invariants (enforced in code, tested in tests/):
  I1 observer-only: nothing here imports QorCap scoring/receipt/referee code or writes outside LAB_ROOT.
  I2 independent labels: labels come only from QorCap's own hash-chained locks / confirmed final /
     legality / timeline; a Glimpse reply can never become a label.
  I3 promotion is never automatic: a passing candidate becomes PENDING_APPROVAL for Con.
  I4 budget: per-game and weekly caps, checked before any (live) call; replay mode spends $0.
"""
LAB_VERSION = "glimpse-lab/0.1.0-proposal"
TAG = "ADVISORY_AI"
