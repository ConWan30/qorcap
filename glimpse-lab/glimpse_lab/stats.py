from math import comb

def mcnemar_exact(b, c):
    """One-sided exact McNemar: b = frames only the candidate got right, c = only the champion got right.
    p = P(X >= b | X ~ Binom(b+c, 0.5))."""
    n = b + c
    if n == 0:
        return 1.0
    return sum(comb(n, k) for k in range(b, n + 1)) / 2 ** n
