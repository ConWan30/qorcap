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
  for about two seconds and the score returns in the same clip, so 12 s still covers that return
  and does not reach the next clip when clips are spaced 20 s apart in the harness.
* ``TRY_WINDOW_MS`` = 45_000. A point-after can land in the following highlight (the 170123 clips
  go 9-0 then 10-0 about 19 s later in that harness) and expires before an unrelated +1.

State lives on the reader. Nothing here is written into receipt.v0 or into a checkpoint hash.
The grammar can only abstain. It does not raise a confidence and it does not fill a blank.

Legal transitions from a locked score, one side only unless noted:

| Last lock | Banner in the window | Phase | New score | Result |
|---|---|---|---|---|
| none yet | any | any | any read | defer (ordinary gates) |
| same | any | any | same | accept |
| either side down | any | any | decrease | abstain |
| both sides move | any | not a try return | both | abstain |
| +3 on one side | field goal | any | that side +3 | accept |
| anything else | field goal | any | not +3 | abstain |
| +6, +7, or +8 on one side | touchdown | any | that side | accept |
| +1 or +2 on the touchdown side | none or touchdown | try | that side | accept |
| +2 on the other side | none or touchdown | try | defensive return | accept |
| +2 on one side | none | none | safety | accept |
| +3 or +6 on one side | none | none | ordinary path | defer |
| +1, +4, +5, +7, +8, other | none | none | illegal | abstain |
| +3 | touchdown only | none | not a field goal | abstain |
| +6 | field goal only | none | not a touchdown | abstain |

The banner does not name a team. Both sides stay candidates. A banner that is still on screen
after the transition locks is spent until its window ends, so the same pixels cannot authorise a
second jump.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# One highlight clip is ~12 s. The score returns inside that clip after the banner.
BANNER_WINDOW_MS = 12_000
# A try can finish in the next highlight. 45 s covers that gap and not a later drive.
TRY_WINDOW_MS = 45_000

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

    def _rewind(self, t_ms: int) -> None:
        if self.last_t is not None and int(t_ms) < self.last_t:
            self.reset()

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

    def filter(self, fr, t_ms: int | None, banner: str | None = None):
        """Blank an illegal score. Quarter and clock stay. A missing score is not filled in."""
        if t_ms is None:
            return fr
        self.note_banner(t_ms, banner)
        if getattr(fr, "left_score", None) is None or getattr(fr, "right_score", None) is None:
            return fr
        phase, side = self._phase_at(t_ms)
        decision = judge(
            self.locked, (fr.left_score, fr.right_score), self.active_banner(t_ms), phase, side,
        )
        if decision != "abstain":
            return fr
        return replace(
            fr,
            ok=False,
            reason="score_transition",
            left_score=None,
            right_score=None,
            score_conf=None,
            score_margin=None,
        )

    def note_lock(self, t_ms: int, away: int, home: int) -> None:
        """Remember a score the agreement window actually locked. Does not run during a bare read."""
        self._rewind(t_ms)
        self.last_t = int(t_ms)
        new = (int(away), int(home))
        prev = self.locked
        banner = self.active_banner(t_ms)
        phase, side = self._phase_at(t_ms)
        self.locked = new
        if prev is None:
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
