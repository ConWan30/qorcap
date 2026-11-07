//! In-memory double of the client's auxiliary evidence store, with injectable write, flush, sync
//! and torn-write faults (design sections 2.5 and 9, durability). A record counts as durable, and
//! its binding as consumed, only after write, flush and sync all succeed. Identical records are
//! stored once. A torn record stays on "disk" and must load as unknown, never as success.

use super::Reason;
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault { Write, Flush, Sync, TornWrite }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Persist { Durable, AlreadyDurable, Failed(Reason) }

#[derive(Default, Debug)]
pub struct LocalStoreDouble {
    disk: Vec<Vec<u8>>,
    consumed: BTreeSet<String>,
    next_fault: Option<Fault>,
}

impl LocalStoreDouble {
    pub fn inject(&mut self, f: Fault) { self.next_fault = Some(f); }

    pub fn persist(&mut self, record: &[u8], binding: &str) -> Persist {
        if self.disk.iter().any(|r| r == record) && self.consumed.contains(binding) { return Persist::AlreadyDurable; }
        match self.next_fault.take() {
            Some(Fault::Write) | Some(Fault::Flush) | Some(Fault::Sync) => Persist::Failed(Reason::PersistFailed),
            Some(Fault::TornWrite) => {
                self.disk.push(record[..record.len() / 2].to_vec());
                Persist::Failed(Reason::PersistFailed)
            }
            None => {
                self.disk.push(record.to_vec());
                self.consumed.insert(binding.to_string());
                Persist::Durable
            }
        }
    }

    pub fn is_consumed(&self, binding: &str) -> bool { self.consumed.contains(binding) }
    pub fn records(&self) -> &[Vec<u8>] { &self.disk }
}
