//! In-memory replay ring buffer over an MPEG-TS byte stream.
//!
//! Chunks are whole 188-byte TS packets. For each chunk we remember the offset
//! of the first packet carrying the random_access_indicator (keyframe), and we
//! cache the latest PAT/PMT so a saved clip always starts with tables + an IDR.
use std::collections::VecDeque;

pub const TS: usize = 188;

#[derive(Debug)]
struct Chunk {
    t_ms: u64,
    data: Vec<u8>,
    key_off: Option<usize>,
}

#[derive(Debug)]
pub struct ReplayRing {
    window_ms: u64,
    max_bytes: usize,
    bytes: usize,
    chunks: VecDeque<Chunk>,
    pat: Option<Vec<u8>>,
    pmt_pid: Option<u16>,
    pmt: Option<Vec<u8>>,
}

pub fn pid(p: &[u8]) -> u16 {
    (((p[1] & 0x1f) as u16) << 8) | p[2] as u16
}

/// True if this TS packet has the random access (keyframe) flag set.
pub fn is_random_access(p: &[u8]) -> bool {
    p.len() >= 6 && p[0] == 0x47 && (p[3] & 0x20) != 0 && p[4] > 0 && (p[5] & 0x40) != 0
}

/// Extract the first program's PMT PID from a PAT packet.
pub fn pat_pmt_pid(p: &[u8]) -> Option<u16> {
    if p[0] != 0x47 || pid(p) != 0 || (p[1] & 0x40) == 0 { return None; }
    let mut i = 4;
    if p[3] & 0x20 != 0 { i += 1 + p[4] as usize; }
    if i >= TS { return None; }
    i += 1 + p[i] as usize; // pointer field
    if i + 8 > TS || p[i] != 0x00 { return None; }
    let sec_len = (((p[i + 1] & 0x0f) as usize) << 8) | p[i + 2] as usize;
    let end = (i + 3 + sec_len).saturating_sub(4).min(TS);
    let mut j = i + 8;
    while j + 4 <= end {
        let prog = ((p[j] as u16) << 8) | p[j + 1] as u16;
        let pmt = (((p[j + 2] & 0x1f) as u16) << 8) | p[j + 3] as u16;
        if prog != 0 { return Some(pmt); }
        j += 4;
    }
    None
}

impl ReplayRing {
    pub fn new(window_secs: u64, max_bytes: usize) -> Self {
        Self { window_ms: window_secs * 1000, max_bytes, bytes: 0, chunks: VecDeque::new(), pat: None, pmt_pid: None, pmt: None }
    }
    pub fn len_bytes(&self) -> usize { self.bytes }
    pub fn span_ms(&self) -> u64 {
        match (self.chunks.front(), self.chunks.back()) { (Some(a), Some(b)) => b.t_ms - a.t_ms, _ => 0 }
    }
    /// Drop everything (e.g. after a device reopen, timestamps restart).
    pub fn clear(&mut self) { self.chunks.clear(); self.bytes = 0; }

    /// Push whole TS packets captured at time `t_ms` (monotonic).
    pub fn push(&mut self, t_ms: u64, data: &[u8]) {
        let n = data.len() / TS * TS;
        let data = &data[..n];
        let mut key_off = None;
        for (k, p) in data.chunks_exact(TS).enumerate() {
            if p[0] != 0x47 { continue; }
            let id = pid(p);
            if id == 0 {
                if let Some(m) = pat_pmt_pid(p) { self.pmt_pid = Some(m); self.pat = Some(p.to_vec()); }
            } else if Some(id) == self.pmt_pid && p[1] & 0x40 != 0 {
                self.pmt = Some(p.to_vec());
            }
            if key_off.is_none() && is_random_access(p) { key_off = Some(k * TS); }
        }
        if n == 0 { return; }
        self.bytes += n;
        self.chunks.push_back(Chunk { t_ms, data: data.to_vec(), key_off });
        self.trim(t_ms);
    }

    fn trim(&mut self, now: u64) {
        // Keep the window, but never drop the chunk holding the oldest needed
        // keyframe: we trim while the *second* keyframe chunk is still old enough.
        while let Some(f) = self.chunks.front() {
            let too_old = now.saturating_sub(f.t_ms) > self.window_ms;
            let too_big = self.bytes > self.max_bytes;
            if !(too_old || too_big) || self.chunks.len() <= 1 { break; }
            let c = self.chunks.pop_front().unwrap();
            self.bytes -= c.data.len();
        }
    }

    pub fn tables(&self) -> (Option<Vec<u8>>, Option<Vec<u8>>) { (self.pat.clone(), self.pmt.clone()) }

    /// Return a playable TS clip: PAT + PMT + data starting at the first keyframe.
    pub fn snapshot(&self) -> Option<Vec<u8>> { self.snapshot_last(None) }
    /// Like `snapshot`, limited to roughly the last `ms` (starts at the first keyframe inside it).
    pub fn snapshot_last(&self, ms: Option<u64>) -> Option<Vec<u8>> {
        let from = match (ms, self.chunks.back()) { (Some(m), Some(b)) => b.t_ms.saturating_sub(m), _ => 0 };
        // latest keyframe at/before `from` (clip covers at least `ms`), else the first keyframe
        let start = self.chunks.iter().rposition(|c| c.t_ms <= from && c.key_off.is_some())
            .or_else(|| self.chunks.iter().position(|c| c.key_off.is_some()))?;
        let mut out = Vec::with_capacity(self.bytes + 2 * TS);
        if let Some(p) = &self.pat { out.extend_from_slice(p); }
        if let Some(p) = &self.pmt { out.extend_from_slice(p); }
        for (i, c) in self.chunks.iter().enumerate().skip(start) {
            let off = if i == start { c.key_off.unwrap() } else { 0 };
            out.extend_from_slice(&c.data[off..]);
        }
        Some(out)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    pub fn pkt(pid: u16, ra: bool) -> Vec<u8> {
        let mut p = vec![0xffu8; TS];
        p[0] = 0x47; p[1] = ((pid >> 8) as u8) & 0x1f; p[2] = pid as u8;
        if ra { p[3] = 0x30; p[4] = 1; p[5] = 0x40; } else { p[3] = 0x10; }
        p
    }
    pub fn pat(pmt: u16) -> Vec<u8> {
        let mut p = vec![0xffu8; TS];
        p[..4].copy_from_slice(&[0x47, 0x40, 0x00, 0x10]);
        p[4] = 0; // pointer
        let s = [0x00, 0xb0, 13, 0, 1, 0xc1, 0, 0, 0, 1, 0xe0 | (pmt >> 8) as u8, pmt as u8, 0, 0, 0, 0];
        p[5..5 + s.len()].copy_from_slice(&s);
        p
    }
    #[test]
    fn parses_pat_and_ra() {
        assert_eq!(pat_pmt_pid(&pat(0x1000)), Some(0x1000));
        assert!(is_random_access(&pkt(0x100, true)));
        assert!(!is_random_access(&pkt(0x100, false)));
    }
    #[test]
    fn window_trims_and_starts_at_keyframe() {
        let mut r = ReplayRing::new(2, usize::MAX);
        let mut pmt = pkt(0x1000, false); pmt[1] |= 0x40;
        r.push(0, &[pat(0x1000), pmt.clone()].concat());
        for t in 0..50u64 {
            let ra = t % 10 == 0; // keyframe every second at 10 chunks/s
            r.push(t * 100, &[pkt(0x100, false), pkt(0x100, ra)].concat());
        }
        assert!(r.span_ms() <= 2000);
        let s = r.snapshot().unwrap();
        assert_eq!(s.len() % TS, 0);
        assert_eq!(pid(&s[0..]), 0, "PAT first");
        assert_eq!(pid(&s[TS..]), 0x1000, "PMT second");
        assert!(is_random_access(&s[2 * TS..]), "then a keyframe");
        let short = r.snapshot_last(Some(500)).unwrap();
        assert!(short.len() < s.len() && is_random_access(&short[2 * TS..]));
    }
    #[test]
    fn byte_cap_and_partial_packets() {
        let mut r = ReplayRing::new(1000, TS * 10);
        for t in 0..100 { r.push(t, &pkt(0x100, true)); }
        assert!(r.len_bytes() <= TS * 10);
        r.push(200, &[0x47u8; 100]); // partial packet ignored
        assert!(r.len_bytes() <= TS * 10);
        r.clear();
        assert!(r.snapshot().is_none());
    }
}
