"""Score-transition grammar for the standard bar.

A new score is kept only when it is a legal step from the last locked score, and only when a
FIELD GOAL or TOUCHDOWN banner seen inside ``BANNER_WINDOW_MS`` agrees with that step. The banner
never mints digits: the score still has to be read off the bug, and a score that was not read stays
unclaimed. An illegal step is abstained. It is never rewritten to a nearby legal score.

No banner, no new leniency. The read goes through the existing gates. Decreases are always illegal.
A bare +1 is illegal unless a touchdown is already locked and the try window is still open. +4, +5,
+7 and +8 with no touchdown banner abstain. +3 and +6 with no banner are deferred (the ordinary
path, which already rechecks a suspicious jump). +2 with no banner is a safety and is kept.

Windows (constructor arguments; these constants are the default):

* ``BANNER_WINDOW_MS`` = 12_000. One public highlight clip is about 12 s. The banner is on screen
  for about two seconds and the score returns in the same clip.
* ``TRY_WINDOW_MS`` = 45_000. A point-after can land in the following highlight and expires before
  an unrelated +1.

Re-anchor (constructor arguments; these constants are the default):

* ``REANCHOR_READS`` = 4 and ``REANCHOR_SPAN_MS`` = 2_000. The agreement window already demands 3
  samples over 400 ms. A score that is not a legal step from the anchor is still abstained until
  the same pair has passed the existing glyph gates on 4 reads whose first and last are at least
  2 s apart. That read is kept, with ``detail["grammar_reanchor"]`` set, and becomes the anchor.
  Its confidence is the confidence the gates already gave it. A one-frame glitch cannot move the
  anchor. This is also how a wrong first lock recovers: the first lock is ordinary, and a later
  score that keeps agreeing takes over.

A re-anchor stays unconfirmed until ``note_lock`` records that same pair (the agreement window
locked it). While a disagreement has persisted across the vote, or the last re-anchor is still
unconfirmed, ``allows_final_fallback`` is false. The match final must abstain instead of
publishing the frozen anchor.

The vote uses the agreement window's numbers: ``vote_reads`` 3, ``vote_interval_ms`` 200,
``vote_span_ms`` 400, ``vote_gap_ms`` 2_000. A sample closer than the interval is not new
evidence and is not a reset. One illegal frame blanks that frame and does not flip
``allows_final_fallback``. The same illegal run, persisted across the vote, does. A gap longer
than ``vote_gap_ms`` starts the count over and does not clear a disagreement that already
persisted. ``suspicious_reads`` 9 over ``suspicious_span_ms``
2_000 is the agreement window's bar for an implausible jump, so a short forged decrease still
cannot confirm a re-anchor. See ``docs/SCOREBUG_CLOCK_VOTE.md``.

A backward timestamp is a cut. The old anchor is dropped and the next gated score is a fresh
entry. An open try is kept only when that next lock is the same score that had the try open, so
the point-after is still legal. A quarter change does not reset the grammar: scores carry over.

State lives on the reader. Nothing here is written into receipt.v0 or into a checkpoint hash.
The grammar can only abstain. It does not raise a confidence and it does not fill a blank.

Legal transitions from a locked score, one side only unless noted:

| Last lock | Banner in the window | Phase | New score | Result |
|---|---|---|---|---|
| none yet | any | any | any read | defer (ordinary gates) |
| same | any | any | same | accept |
| either side down | any | any | decrease | abstain, unless it re-anchors |
| both sides move | any | not a try return | both | abstain, unless it re-anchors |
| +3 on one side | field goal | any | that side +3 | accept |
| anything else | field goal | any | not +3 | abstain |
| +6, +7, or +8 on one side | touchdown | any | that side | accept |
| +1 or +2 on the touchdown side | none or touchdown | try | that side | accept |
| +2 on the other side | none or touchdown | try | defensive return | accept |
| +2 on one side | none | none | safety | accept |
| +3 or +6 on one side | none | none | ordinary path | defer |
| +1, +4, +5, +7, +8, other | none | none | illegal | abstain, unless it re-anchors |
| +3 | touchdown only | none | not a field goal | abstain |
| +6 | field goal only | none | not a touchdown | abstain |
| same illegal pair, 4 reads over 2 s | any | any | that pair | re-anchor |

The banner does not name a team. Both sides stay candidates. A banner that is still on screen
after the transition locks is spent until its window ends, so the same pixels cannot authorise a
second jump.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# One highlight clip is ~12 s. The score returns inside that clip after the banner.
BANNER_WINDOW_MS = 12_000
# A try can finish in the next highlight. 45 s covers a short gap and not a later drive.
TRY_WINDOW_MS = 45_000
# Four gated reads spanning two seconds. Stricter than one agreement window (3 x 400 ms).
REANCHOR_READS = 4
REANCHOR_SPAN_MS = 2_000
# Same numbers as QorTime AgreementConfig. A disagreement has to clear the lock's own vote.
VOTE_READS = 3
VOTE_INTERVAL_MS = 200
VOTE_SPAN_MS = 400
VOTE_GAP_MS = 2_000
SUSPICIOUS_READS = 9
SUSPICIOUS_SPAN_MS = 2_000
JUMP_MEMORY_MS = 30_000

_TD_COMBINED = (6, 7, 8)
_TRY_STEPS = (1, 2)


def judge(prev, new, banner, phase, td_side) -> str:
    """``accept``, ``abstain``, or ``defer``.

    ``prev`` and ``new`` are ``(away, home)`` or ``prev`` is None. ``banner`` is ``"fg"``,
    ``"td"``, or None. ``phase`` is ``"try"`` or None. ``td_side`` is ``"away"`` or ``"home"``
    while a try is open.
    """
    if prev is None:
        return "defer"
    la, lh = int(prev[0]), int(prev[1])
    a, h = int(new[0]), int(new[1])
    da, dh = a - la, h - lh
    if da < 0 or dh < 0:
        return "abstain"
    if da == 0 and dh == 0:
        return "accept"
    if da and dh:
        return "abstain"
    delta = da or dh
    side = "away" if da else "home"
    if banner == "fg":
        return "accept" if delta == 3 else "abstain"
    if banner == "td" and delta in _TD_COMBINED:
        return "accept"
    if phase == "try" and td_side == side and delta in _TRY_STEPS:
        return "accept"
    if phase == "try" and td_side is not None and td_side != side and delta == 2:
        return "accept"
    if banner == "td":
        return "abstain"
    if delta == 2:
        return "accept"
    if delta in (3, 6):
        return "defer"
    return "abstain"


@dataclass
class ScoreGrammar:
    """Lock memory, banner window, and try window. Not a receipt and not a hash input."""

    banner_window_ms: int = BANNER_WINDOW_MS
    try_window_ms: int = TRY_WINDOW_MS
    reanchor_reads: int = REANCHOR_READS
    reanchor_span_ms: int = REANCHOR_SPAN_MS
    vote_reads: int = VOTE_READS
    vote_interval_ms: int = VOTE_INTERVAL_MS
    vote_span_ms: int = VOTE_SPAN_MS
    vote_gap_ms: int = VOTE_GAP_MS
    suspicious_reads: int = SUSPICIOUS_READS
    suspicious_span_ms: int = SUSPICIOUS_SPAN_MS
    jump_memory_ms: int = JUMP_MEMORY_MS

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last_t: int | None = None
        self.locked: tuple[int, int] | None = None
        self.banner_kind: str | None = None
        self.banner_until: int | None = None
        self.spent_kind: str | None = None
        self.spent_until: int | None = None
        self.phase: str | None = None
        self.td_side: str | None = None
        self.try_until: int | None = None
        self._cand: tuple[int, int] | None = None
        self._cand_n = 0
        self._cand_t0: int | None = None
        self._try_carry: tuple[tuple[int, int], str] | None = None
        self.disagreeing = False
        self.reanchor_pair: tuple[int, int] | None = None
        self.reanchor_confirmed = True
        self._vote_n = 0
        self._vote_t0: int | None = None
        self._vote_last: int | None = None

    def _cut(self) -> None:
        """Drop the anchor. Keep an open try so the same score can be re-locked legally."""
        carry = None
        if self.phase == "try" and self.locked is not None and self.td_side in ("away", "home"):
            carry = (tuple(self.locked), self.td_side)
        self.reset()
        self._try_carry = carry

    def _rewind(self, t_ms: int) -> None:
        if self.last_t is not None and int(t_ms) < self.last_t:
            self._cut()

    def active_banner(self, t_ms: int) -> str | None:
        if self.banner_kind and self.banner_until is not None and int(t_ms) <= self.banner_until:
            return self.banner_kind
        return None

    def _phase_at(self, t_ms: int):
        if self.phase == "try" and self.try_until is not None and int(t_ms) <= self.try_until:
            return "try", self.td_side
        return None, None

    def note_banner(self, t_ms: int, kind: str | None) -> None:
        """Arm a banner. Seeing the same kind again does not extend the window."""
        self._rewind(t_ms)
        self.last_t = int(t_ms)
        if kind not in ("fg", "td"):
            return
        if self.spent_kind == kind and self.spent_until is not None and int(t_ms) <= self.spent_until:
            return
        if self.banner_kind == kind and self.banner_until is not None and int(t_ms) <= self.banner_until:
            return
        self.banner_kind = kind
        self.banner_until = int(t_ms) + int(self.banner_window_ms)

    def _consume(self, kind: str) -> None:
        self.spent_kind = kind
        self.spent_until = self.banner_until
        self.banner_kind = None
        self.banner_until = None

    def _open_try(self, t_ms: int, side: str) -> None:
        self.phase = "try"
        self.td_side = side
        self.try_until = int(t_ms) + int(self.try_window_ms)

    def _clear_cand(self) -> None:
        self._cand = None
        self._cand_n = 0
        self._cand_t0 = None

    def _clear_vote(self) -> None:
        self._vote_n = 0
        self._vote_t0 = None
        self._vote_last = None

    def _note_disagreement(self, t_ms: int) -> None:
        """Count an illegal gated score. One frame does not set ``disagreeing``."""
        t_ms = int(t_ms)
        last = self._vote_last
        if last is None or t_ms < last or t_ms - last > int(self.vote_gap_ms):
            self._vote_n = 1
            self._vote_t0 = t_ms
            self._vote_last = t_ms
            # A gap starts a new count. It does not erase a disagreement that already persisted.
            return
        if t_ms - last < int(self.vote_interval_ms):
            return
        self._vote_n += 1
        self._vote_last = t_ms
        span = t_ms - (t_ms if self._vote_t0 is None else int(self._vote_t0))
        if self._vote_n >= int(self.vote_reads) and span >= int(self.vote_span_ms):
            self.disagreeing = True

    def _reanchor(self, pair: tuple[int, int], t_ms: int) -> bool:
        """True when this gated pair has agreed for K reads spanning T milliseconds."""
        t_ms = int(t_ms)
        if pair != self._cand:
            self._cand = pair
            self._cand_n = 1
            self._cand_t0 = t_ms
            return False
        self._cand_n += 1
        span = t_ms - (t_ms if self._cand_t0 is None else int(self._cand_t0))
        return self._cand_n >= int(self.reanchor_reads) and span >= int(self.reanchor_span_ms)

    def allows_final_fallback(self) -> bool:
        """False when a frozen anchor must not be published as the match score."""
        if self.disagreeing:
            return False
        if self.reanchor_pair is not None and not self.reanchor_confirmed:
            return False
        return True

    def filter(self, fr, t_ms: int | None, banner: str | None = None):
        """Blank an illegal score. Quarter and clock stay. A missing score is not filled in.

        A score that is not a legal step is kept, flagged ``grammar_reanchor``, once it has
        passed the existing gates on ``reanchor_reads`` reads spanning ``reanchor_span_ms``.
        That does not raise ``score_conf``.
        """
        if t_ms is None:
            return fr
        self.note_banner(t_ms, banner)
        if getattr(fr, "left_score", None) is None or getattr(fr, "right_score", None) is None:
            return fr
        pair = (int(fr.left_score), int(fr.right_score))
        phase, side = self._phase_at(t_ms)
        decision = judge(self.locked, pair, self.active_banner(t_ms), phase, side)
        if decision != "abstain":
            self._clear_cand()
            self._clear_vote()
            self.disagreeing = False
            return fr
        self._note_disagreement(int(t_ms))
        if not self._reanchor(pair, t_ms):
            return replace(
                fr,
                ok=False,
                reason="score_transition",
                left_score=None,
                right_score=None,
                score_conf=None,
                score_margin=None,
            )
        self.locked = pair
        self.phase = None
        self.td_side = None
        self.try_until = None
        self.reanchor_pair = pair
        self.reanchor_confirmed = False
        self.disagreeing = False
        self._clear_cand()
        self._clear_vote()
        detail = dict(getattr(fr, "detail", None) or {})
        detail["grammar_reanchor"] = True
        return replace(fr, detail=detail)

    def note_lock(self, t_ms: int, away: int, home: int) -> None:
        """Remember a score the agreement window actually locked. Does not run during a bare read."""
        self._rewind(t_ms)
        self.last_t = int(t_ms)
        new = (int(away), int(home))
        if self.reanchor_pair == new:
            self.reanchor_confirmed = True
        prev = self.locked
        banner = self.active_banner(t_ms)
        phase, side = self._phase_at(t_ms)
        carry = self._try_carry
        self.locked = new
        self._try_carry = None
        if prev is None:
            if carry is not None and new == carry[0]:
                self._open_try(t_ms, carry[1])
            return
        da, dh = new[0] - prev[0], new[1] - prev[1]
        if da == 0 and dh == 0:
            return
        if banner and judge(prev, new, banner, phase, side) == "accept":
            self._consume(banner)
        if da == 6 and dh == 0:
            self._open_try(t_ms, "away")
            return
        if dh == 6 and da == 0:
            self._open_try(t_ms, "home")
            return
        self.phase = None
        self.td_side = None
        self.try_until = None


def lock_agreement_config(grammar: ScoreGrammar | None = None):
    """The agreement window, with N and the span written down.

    The numbers are QorTime's ``AgreementConfig`` defaults. Passing them explicitly keeps a
    lock and the disagreement vote on the same bar. ``need_suspicious`` stays 9 over 2 s, so a
    forged decrease cannot confirm on a short run.
    """
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementConfig

    g = grammar if grammar is not None else ScoreGrammar()
    return AgreementConfig(
        need=int(g.vote_reads),
        min_interval_ns=int(g.vote_interval_ms) * 1_000_000,
        min_span_ns=int(g.vote_span_ms) * 1_000_000,
        max_gap_ns=int(g.vote_gap_ms) * 1_000_000,
        need_suspicious=int(g.suspicious_reads),
        min_span_suspicious_ns=int(g.suspicious_span_ms) * 1_000_000,
        jump_memory_ns=int(g.jump_memory_ms) * 1_000_000,
    )
