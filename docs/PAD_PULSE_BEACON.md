# Pad pulse beacon

not implemented: needs controller-write approval

A pulse on the controller (an output report, rumble, or lightbar) would be a
round trip the host could time. QorCap does not send it. The HID reader stays
read-only: no output report, no feature report, no rumble, no lightbar.

Seal confirmation is a console line (`Seal: waiting`, `Seal: locked`,
`Seal: failed (reason)`). Gap-spanning liveness uses reports the host already
receives. See `docs/SEAL_DUAL_BIND.md` and `docs/PAD_LINK.md`.
