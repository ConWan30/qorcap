# Lag band

PROVISIONAL. Not validated on a real practice match.

A lag band needs at least 30 samples. At least 0.999 of them sit within one
frame of the median (16_667 µs). The three time-thirds differ by at most one
frame. One OPTIONS hold, or a set that is only OPTIONS holds, does not meet
that floor.

Samples can come from an L2 aim-zoom step in a center crop, an R2 ammo-crop
change, or OPTIONS-to-pause when that read exists. The pause crop stays
`stub_not_calibrated`. The center and ammo crops are not validated on
match-20261106-201029.

If measured lag is missing, or the pause-menu exit is missing, the seal
exclusion window stays unresolved and does not release later events into counts.
