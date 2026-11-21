//! The read side of the hand logger: one trait with a single input-report read. There is no method that
//! could send anything to the controller, so neither the Windows reader nor a test double can.

use super::device::DeviceInfo;
use std::collections::VecDeque;

/// One read attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadOutcome {
    /// `n` bytes of one input report are in the caller's buffer (report id at byte 0).
    Report(usize),
    /// Nothing arrived within the timeout. Never turned into a report.
    Timeout,
    /// The device went away or the read failed; the handle is closed and enumeration restarts.
    Disconnected(String),
}

/// Listen-only input-report source.
pub trait ReportSource: Send {
    fn read(&mut self, buf: &mut [u8], timeout_ms: u32) -> ReadOutcome;
}

/// Result of one enumeration + open attempt.
pub enum OpenOutcome {
    Open(Box<dyn ReportSource>, DeviceInfo),
    /// No DualSense Edge / DualSense on wired USB.
    Missing(String),
    /// A DualSense-family pad was seen but not opened (`rejected_transport` / `rejected_device`).
    Rejected { state: &'static str, detail: String },
}

/// Scripted source for tests: plays back reports, timeouts and disconnects in order, then times out.
pub struct MockSource {
    pub script: VecDeque<MockStep>,
}

#[derive(Debug, Clone)]
pub enum MockStep { Report(Vec<u8>), Timeout, Disconnect(String) }

impl MockSource {
    pub fn new(steps: impl IntoIterator<Item = MockStep>) -> Self { Self { script: steps.into_iter().collect() } }
}

impl ReportSource for MockSource {
    fn read(&mut self, buf: &mut [u8], timeout_ms: u32) -> ReadOutcome {
        match self.script.pop_front() {
            Some(MockStep::Report(r)) => { let n = r.len().min(buf.len()); buf[..n].copy_from_slice(&r[..n]); ReadOutcome::Report(n) }
            Some(MockStep::Disconnect(e)) => ReadOutcome::Disconnected(e),
            Some(MockStep::Timeout) | None => {
                std::thread::sleep(std::time::Duration::from_millis(u64::from(timeout_ms.min(5))));
                ReadOutcome::Timeout
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mock_plays_its_script_then_times_out() {
        let mut m = MockSource::new([MockStep::Report(vec![1, 2, 3]), MockStep::Disconnect("gone".into())]);
        let mut b = [0u8; 64];
        assert_eq!(m.read(&mut b, 1), ReadOutcome::Report(3));
        assert_eq!(&b[..3], &[1, 2, 3]);
        assert_eq!(m.read(&mut b, 1), ReadOutcome::Disconnected("gone".into()));
        assert_eq!(m.read(&mut b, 1), ReadOutcome::Timeout);
    }
}
