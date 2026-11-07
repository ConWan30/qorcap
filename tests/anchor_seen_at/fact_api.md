
`CheckedSeenAt` (also named `SeenAtFact` for compatibility) is an opaque, immutable checker token. Only `check_ack` creates one;
cloning preserves both its contents and its late/retained classification.
There is no deserializer, default, public constructor or mutable accessor.

E2: an outside crate cannot mutate the checked timing or commitment fields.

```compile_fail,E0616
use qorcap::anchor_seen_at::check::SeenAtFact;
fn change(f: &mut SeenAtFact) {
    f.rtt_ns = 0;
    f.received_mono_ns = 0;
    f.commitment.commitment_index = "0".into();
}
```

A getter cannot be used as a mutable route into a checked commitment either.

```compile_fail,E0594
use qorcap::anchor_seen_at::check::SeenAtFact;
fn change(f: &SeenAtFact) {
    f.commitment().commitment_index = "0".into();
}
```

E3: even an outside caller supplying every field cannot construct a fact.

```compile_fail,E0451
use qorcap::anchor_seen_at::{check::SeenAtFact, types::Commitment};
fn scratch(commitment: Commitment) -> SeenAtFact {
    SeenAtFact {
        ack_id: String::new(), ack_key_id: String::new(),
        challenge_key_id: String::new(), trust_policy_id: String::new(),
        commitment, commitment_digest: String::new(),
        server_boot_id: String::new(), server_clock_epoch_id: String::new(),
        client_boot_id: String::new(), client_clock_epoch_id: String::new(),
        received_mono_ns: 0, received_wall_us: String::new(), ledger_seq: 0,
        prev_ledger_entry_digest: String::new(), rtt_ns: 0, late: false,
    }
}
```

The read-only API remains available to external crates.

```
use qorcap::anchor_seen_at::check::SeenAtFact;
fn read(f: &SeenAtFact) -> (u64, bool) { (f.rtt_ns(), f.is_late()) }
```
