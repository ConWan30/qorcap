P1 local records expose immutable getters, not live final authority. Journal creation is
exclusive and cannot resume a missing history. Original source/time/policy claims still need
separate trusted origin. File persistence is local, not independent retention.

Private fields cannot be assigned:
```compile_fail,E0616
use qorcap::anchor_seen_at::p1::OriginalExchange;
fn edit(x: &mut OriginalExchange) { x.event = 0; }
```

Nested values stay immutable:
```compile_fail,E0594
use qorcap::anchor_seen_at::p1::OriginalExchange;
fn edit(x: &OriginalExchange) { x.commitment().session_id = String::new(); }
```

No diagnostic-to-record construction:
```compile_fail,E0451
use qorcap::anchor_seen_at::p1::DecisionTimeRecord;
fn main() { let _ = DecisionTimeRecord { generation: 0, ..unreachable!() }; }
```

No snapshot Default:
```compile_fail,E0277
use qorcap::anchor_seen_at::p1::DurableLineage;
fn main() { let _: DurableLineage = Default::default(); }
```

Wire bytes cannot mint an observation:
```compile_fail,E0277
use qorcap::anchor_seen_at::p1::PolicyObservation;
fn main() { let _: PolicyObservation = serde_json::from_str("{}").unwrap(); }
```

A lineage cannot fork with Clone:
```compile_fail,E0277
use qorcap::anchor_seen_at::p1::DurableLineage;
fn needs_clone<T: Clone>() {}
fn main() { needs_clone::<DurableLineage>(); }
```

Getters and exact original clones compile:
```no_run
use qorcap::anchor_seen_at::p1::{OriginalExchange, DecisionTimeRecord};
fn inspect(x: &OriginalExchange, d: &DecisionTimeRecord) {
    let same = x.clone();
    assert_eq!(same.event(), x.event());
    let _ = (x.challenge(), x.commitment(), x.origin(), x.policy());
    assert_eq!(d.label(), "checked at decision time");
    assert!(!d.publication_time_freshness_asserted());
}
```

The decision-time record only qualifies the earlier sample. Arbitrary later pause or expiry
is not bounded and does not become a claim of freshness at a later effect. No publication,
remote policy service, transport or final gate is implemented by this local P1 module.
