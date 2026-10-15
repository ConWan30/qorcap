//! Free-space checks for the recording volume. Refuse to start (and stop cleanly
//! mid-record) rather than write into ENOSPC and corrupt the open part.
use std::path::Path;

/// Default: refuse to start when free space is below this (2 GiB).
pub const DEFAULT_MIN_FREE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
/// Mid-record: stop cleanly when free space drops below this (512 MiB).
pub const CRITICAL_FREE_BYTES: u64 = 512 * 1024 * 1024;

/// Bytes free on the volume that contains `path` (or its nearest existing ancestor).
pub fn free_bytes(path: &Path) -> Option<u64> {
    let probe = nearest_existing(path)?;
    free_bytes_of(&probe)
}

fn nearest_existing(path: &Path) -> Option<std::path::PathBuf> {
    let mut p = if path.as_os_str().is_empty() {
        std::env::current_dir().ok()?
    } else {
        path.to_path_buf()
    };
    loop {
        if p.exists() {
            return Some(p);
        }
        if !p.pop() {
            return std::env::current_dir().ok();
        }
    }
}

#[cfg(unix)]
fn free_bytes_of(path: &Path) -> Option<u64> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let c = CString::new(path.as_os_str().as_bytes()).ok()?;
    unsafe {
        let mut s: libc::statvfs = std::mem::zeroed();
        if libc::statvfs(c.as_ptr(), &mut s) != 0 {
            return None;
        }
        Some(s.f_bavail as u64 * s.f_frsize as u64)
    }
}

#[cfg(windows)]
fn free_bytes_of(path: &Path) -> Option<u64> {
    use std::os::windows::ffi::OsStrExt;
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut free_for_caller: u64 = 0;
    let mut total: u64 = 0;
    let mut free_total: u64 = 0;
    let ok = unsafe {
        GetDiskFreeSpaceExW(
            wide.as_ptr(),
            &mut free_for_caller,
            &mut total,
            &mut free_total,
        )
    };
    if ok == 0 {
        None
    } else {
        Some(free_for_caller)
    }
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn GetDiskFreeSpaceExW(
        root: *const u16,
        free_for_caller: *mut u64,
        total: *mut u64,
        free_total: *mut u64,
    ) -> i32;
}

#[cfg(not(any(unix, windows)))]
fn free_bytes_of(_path: &Path) -> Option<u64> {
    None
}

pub fn fmt_bytes(n: u64) -> String {
    const GB: f64 = 1024.0 * 1024.0 * 1024.0;
    const MB: f64 = 1024.0 * 1024.0;
    if n as f64 >= GB {
        format!("{:.1} GB", n as f64 / GB)
    } else {
        format!("{:.0} MB", n as f64 / MB)
    }
}

/// Ok(()) if enough space to start; Err(message) if known-low or unreadable when required.
pub fn check_start(path: &Path, min_bytes: u64) -> Result<(), String> {
    match free_bytes(path) {
        Some(free) if free < min_bytes => Err(format!(
            "not enough free disk space on {}: {} free, need at least {} to start recording (free space and try again)",
            path.display(),
            fmt_bytes(free),
            fmt_bytes(min_bytes)
        )),
        Some(_) | None => Ok(()), // if we cannot measure, do not block start
    }
}

/// true when free space is critically low (should stop recording cleanly).
pub fn is_critical(path: &Path) -> Option<(bool, u64)> {
    free_bytes(path).map(|f| (f < CRITICAL_FREE_BYTES, f))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fmt_and_probe_cwd() {
        assert!(fmt_bytes(2 * 1024 * 1024 * 1024).contains("GB"));
        assert!(fmt_bytes(100 * 1024 * 1024).contains("MB"));
        let free = free_bytes(Path::new(".")).expect("cwd volume readable");
        assert!(free > 0);
        assert!(check_start(Path::new("."), 1).is_ok());
        assert!(check_start(Path::new("."), free + 1).is_err());
    }
}
