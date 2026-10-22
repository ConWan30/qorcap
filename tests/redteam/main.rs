//! The committed matrix and the docs page match a fresh `qorcap redteam` run.
//! Newlines are normalized so a Windows checkout that rewrites LF as CRLF still matches.

fn lf(s: &str) -> String { s.replace("\r\n", "\n") }

#[test]
fn committed_matrix_matches_a_fresh_run() {
    assert_eq!(lf(include_str!("matrix.v0.json")), qorcap::redteam::matrix_pretty());
}

#[test]
fn committed_markdown_matches_a_fresh_run() {
    assert_eq!(lf(include_str!("../../docs/REDTEAM.md")), qorcap::redteam::markdown());
}
