"""Optional JEV tie-breaker (TypeSafe System One, Choice primitive). OFF unless calibrated.

Rules: asked only where harness versions disagree; options are ONLY the readings the versions produced plus
'unreadable'; the answer can pick one of them or abstain, never a new number (never mints digits).
It must first pass calibration on independently labelled frames, and its pick is advisory like everything else.
Not wired to the network in this build."""

class NotCalibrated(RuntimeError):
    pass

class JevTieBreaker:
    MIN_CALIBRATION = 50
    def __init__(self, ask=None):
        self.ask = ask              # callable(image, options) -> {option: probability}; None = disabled
        self.threshold = None

    def calibrate(self, examples):
        """examples: [(probs_dict, true_option)] on labelled frames. Pick the lowest confidence threshold at
        which picks are >= 99% correct; refuse if there are too few examples or no threshold qualifies."""
        if len(examples) < self.MIN_CALIBRATION:
            raise NotCalibrated(f"need >= {self.MIN_CALIBRATION} labelled disagreements, have {len(examples)}")
        for th in [x / 100 for x in range(50, 100)]:
            sel = [(max(p, key=p.get), t) for p, t in examples if max(p.values()) >= th]
            if len(sel) >= 10 and sum(a == b for a, b in sel) / len(sel) >= 0.99:
                self.threshold = th
                return th
        raise NotCalibrated("no confidence threshold reaches 99% on labelled frames")

    def tiebreak(self, image, readings):
        if self.ask is None or self.threshold is None:
            return None
        options = sorted(set(readings)) + ["unreadable"]
        probs = self.ask(image, options)
        pick = max(probs, key=probs.get)
        if pick not in options:                       # never mint a reading
            raise ValueError("Jev returned an option that was not offered")
        if pick == "unreadable" or probs[pick] < self.threshold:
            return None
        return pick
