//! PRACTICE-ONLY synthetic adversarial tests for `qorcap::anchor_seen_at`.
//! Built only with `--features practice-anchor-seen-at`. No network, no player data, no HID.

mod fixtures;

mod coverage_tests;
mod crypto_parser;
mod durability_clocks;
mod envelope_tests;
mod isolation;
mod readiness_tests;
mod service;
