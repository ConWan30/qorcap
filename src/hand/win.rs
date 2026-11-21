//! Windows reader for the hand logger: SetupAPI + hid.dll enumeration (metadata only, query-only handles),
//! then one `CreateFileW(GENERIC_READ)` handle per pad and overlapped `ReadFile` of input reports.
//!
//! Listen-only by construction: every extern below is on the allowlist in `tests/hand_listen_only.rs`, the
//! only `CreateFileW` call takes its access mask from `device::access_mask` (0 or `GENERIC_READ`), and there
//! is no extern that could send a report. With no write access on the handle, Windows itself refuses any write
//! (ERROR_ACCESS_DENIED); the Windows CI test opens a file through `open_path` and checks that.
//! Only the HID class interface GUID is enumerated, so audio endpoints are never seen or opened.
#![allow(non_snake_case, non_camel_case_types, clippy::upper_case_acronyms, dead_code)]

use super::device::{self, Access, Candidate, DeviceInfo, Reject, USB_REPORT_LEN};
use super::source::{OpenOutcome, ReadOutcome, ReportSource};
use std::ffi::c_void;
use std::ptr::{null, null_mut};

type HANDLE = isize;
type BOOL = i32;
type BOOLEAN = u8;
const INVALID_HANDLE_VALUE: HANDLE = -1;
// Share modes say what OTHER handles may do (the game, the inputs.jsonl reader). They grant this handle nothing.
const FILE_SHARE_READ: u32 = 0x1;
const FILE_SHARE_WRITE: u32 = 0x2;
const OPEN_EXISTING: u32 = 3;
const FILE_FLAG_OVERLAPPED: u32 = 0x4000_0000;
const DIGCF_PRESENT: u32 = 0x2;
const DIGCF_DEVICEINTERFACE: u32 = 0x10;
const ERROR_IO_PENDING: u32 = 997;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_TIMEOUT: u32 = 258;
const HIDP_STATUS_SUCCESS: i32 = 0x0011_0000;
const THREAD_PRIORITY_ABOVE_NORMAL: i32 = 1;

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct GUID { data1: u32, data2: u16, data3: u16, data4: [u8; 8] }
#[repr(C)]
struct SP_DEVICE_INTERFACE_DATA { cb_size: u32, class_guid: GUID, flags: u32, reserved: usize }
#[repr(C)]
#[derive(Default)]
struct HIDD_ATTRIBUTES { size: u32, vendor_id: u16, product_id: u16, version_number: u16 }
#[repr(C)]
#[derive(Default)]
struct HIDP_CAPS { usage: u16, usage_page: u16, input_len: u16, output_len: u16, feature_len: u16, reserved: [u16; 17], counts: [u16; 10] }
#[repr(C)]
struct OVERLAPPED { internal: usize, internal_high: usize, offset: u32, offset_high: u32, h_event: HANDLE }

#[link(name = "kernel32")]
extern "system" {
    fn CreateFileW(name: *const u16, access: u32, share: u32, sa: *const c_void, disposition: u32, flags: u32, template: HANDLE) -> HANDLE;
    fn ReadFile(h: HANDLE, buf: *mut u8, len: u32, read: *mut u32, ov: *mut OVERLAPPED) -> BOOL;
    fn GetOverlappedResult(h: HANDLE, ov: *mut OVERLAPPED, n: *mut u32, wait: BOOL) -> BOOL;
    fn CreateEventW(sa: *const c_void, manual_reset: BOOL, initial: BOOL, name: *const u16) -> HANDLE;
    fn WaitForSingleObject(h: HANDLE, ms: u32) -> u32;
    fn CancelIoEx(h: HANDLE, ov: *mut OVERLAPPED) -> BOOL;
    fn CloseHandle(h: HANDLE) -> BOOL;
    fn GetLastError() -> u32;
    fn QueryPerformanceCounter(c: *mut i64) -> BOOL;
    fn QueryPerformanceFrequency(f: *mut i64) -> BOOL;
    fn SetThreadPriority(h: HANDLE, p: i32) -> BOOL;
    fn GetCurrentThread() -> HANDLE;
}
#[link(name = "setupapi")]
extern "system" {
    fn SetupDiGetClassDevsW(guid: *const GUID, enumerator: *const u16, parent: isize, flags: u32) -> HANDLE;
    fn SetupDiEnumDeviceInterfaces(set: HANDLE, devinfo: *const c_void, guid: *const GUID, index: u32, data: *mut SP_DEVICE_INTERFACE_DATA) -> BOOL;
    fn SetupDiGetDeviceInterfaceDetailW(set: HANDLE, data: *const SP_DEVICE_INTERFACE_DATA, detail: *mut u8, size: u32, required: *mut u32, devinfo: *mut c_void) -> BOOL;
    fn SetupDiDestroyDeviceInfoList(set: HANDLE) -> BOOL;
}
#[link(name = "hid")]
extern "system" {
    fn HidD_GetHidGuid(guid: *mut GUID);
    fn HidD_GetAttributes(h: HANDLE, a: *mut HIDD_ATTRIBUTES) -> BOOLEAN;
    fn HidD_GetPreparsedData(h: HANDLE, p: *mut isize) -> BOOLEAN;
    fn HidD_FreePreparsedData(p: isize) -> BOOLEAN;
    fn HidP_GetCaps(p: isize, caps: *mut HIDP_CAPS) -> i32;
    fn HidD_GetSerialNumberString(h: HANDLE, buf: *mut c_void, len: u32) -> BOOLEAN;
}

/// An open Win32 handle, closed on drop.
pub struct Handle(HANDLE);
impl Handle { pub fn raw(&self) -> isize { self.0 } }
impl Drop for Handle { fn drop(&mut self) { if self.0 != 0 && self.0 != INVALID_HANDLE_VALUE { unsafe { CloseHandle(self.0); } } } }

/// The one place a path is opened. `access` maps to 0 (query-only) or `GENERIC_READ`; there is no other value.
/// `Access::Read` adds `FILE_FLAG_OVERLAPPED` for the async read loop.
pub fn open_path(path: &str, access: Access) -> Result<Handle, u32> {
    let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
    let flags = if access == Access::Read { FILE_FLAG_OVERLAPPED } else { 0 };
    let h = unsafe { CreateFileW(wide.as_ptr(), device::access_mask(access), FILE_SHARE_READ | FILE_SHARE_WRITE, null(), OPEN_EXISTING, flags, 0) };
    if h == INVALID_HANDLE_VALUE { Err(unsafe { GetLastError() }) } else { Ok(Handle(h)) }
}

pub fn qpc_now() -> Option<u64> { let mut c = 0i64; (unsafe { QueryPerformanceCounter(&mut c) } != 0 && c >= 0).then_some(c as u64) }
pub fn qpc_freq() -> Option<u64> { let mut f = 0i64; (unsafe { QueryPerformanceFrequency(&mut f) } != 0 && f > 0).then_some(f as u64) }
pub fn raise_thread_priority() { unsafe { SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL); } }

/// HID interface paths that might be a Sony pad (USB or Bluetooth form). Other HID devices are never opened.
fn sony_hid_paths() -> Vec<String> {
    let mut out = vec![];
    let mut guid = GUID::default();
    unsafe { HidD_GetHidGuid(&mut guid) };
    let set = unsafe { SetupDiGetClassDevsW(&guid, null(), 0, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE) };
    if set == INVALID_HANDLE_VALUE { return out; }
    let mut i = 0u32;
    loop {
        let mut d = SP_DEVICE_INTERFACE_DATA { cb_size: std::mem::size_of::<SP_DEVICE_INTERFACE_DATA>() as u32, class_guid: GUID::default(), flags: 0, reserved: 0 };
        if unsafe { SetupDiEnumDeviceInterfaces(set, null(), &guid, i, &mut d) } == 0 { break; }
        i += 1;
        let mut need = 0u32;
        unsafe { SetupDiGetDeviceInterfaceDetailW(set, &d, null_mut(), 0, &mut need, null_mut()) };
        if need < 8 || need > 4096 { continue; }
        // u32 storage keeps the struct's cbSize field aligned.
        let mut buf = vec![0u32; (need as usize).div_ceil(4) + 1];
        buf[0] = if cfg!(target_pointer_width = "64") { 8 } else { 6 };
        let ok = unsafe { SetupDiGetDeviceInterfaceDetailW(set, &d, buf.as_mut_ptr() as *mut u8, need, null_mut(), null_mut()) };
        if ok == 0 { continue; }
        let wide: Vec<u16> = buf[1..].iter().flat_map(|w| [(*w & 0xFFFF) as u16, (*w >> 16) as u16]).take_while(|c| *c != 0).collect();
        let path = String::from_utf16_lossy(&wide);
        let lower = path.to_ascii_lowercase();
        if lower.contains("vid_054c") || lower.contains("_vid&0002054c") { out.push(path); }
    }
    unsafe { SetupDiDestroyDeviceInfoList(set) };
    out
}

/// Metadata of one interface through a query-only handle (no report traffic).
fn describe(path: &str) -> Option<Candidate> {
    let h = open_path(path, Access::QueryOnly).ok()?;
    let mut a = HIDD_ATTRIBUTES { size: std::mem::size_of::<HIDD_ATTRIBUTES>() as u32, ..Default::default() };
    if unsafe { HidD_GetAttributes(h.0, &mut a) } == 0 { return None; }
    let mut caps = HIDP_CAPS::default();
    let mut pp = 0isize;
    if unsafe { HidD_GetPreparsedData(h.0, &mut pp) } == 0 { return None; }
    let st = unsafe { HidP_GetCaps(pp, &mut caps) };
    unsafe { HidD_FreePreparsedData(pp) };
    if st != HIDP_STATUS_SUCCESS { return None; }
    let mut sn = [0u16; 128];
    let serial = (unsafe { HidD_GetSerialNumberString(h.0, sn.as_mut_ptr() as *mut c_void, (sn.len() * 2) as u32) } != 0)
        .then(|| String::from_utf16_lossy(&sn[..sn.iter().position(|c| *c == 0).unwrap_or(sn.len())]))
        .filter(|s| !s.trim().is_empty());
    Some(Candidate { path: path.to_string(), vid: a.vendor_id, pid: a.product_id, bcd_device: a.version_number,
        usage_page: caps.usage_page, usage: caps.usage, input_report_len: caps.input_len, serial })
}

/// Enumerate, gate, and open the first wired DualSense Edge (preferred) or DualSense with `GENERIC_READ`.
pub fn open_pad() -> OpenOutcome {
    let mut transport: Option<String> = None;
    let mut found: Vec<(Candidate, device::DeviceModel)> = vec![];
    for p in sony_hid_paths() {
        let Some(c) = describe(&p) else { continue };
        match device::gate(&c) {
            Ok(m) => found.push((c, m)),
            Err(Reject::Transport(why)) => { transport.get_or_insert(why); }
            Err(Reject::NotGamepadCollection(_)) | Err(Reject::OtherDevice) => {}
        }
    }
    found.sort_by_key(|(_, m)| if m.fallback() { 1 } else { 0 });
    let mut open_denied: Option<String> = None;
    for (c, m) in found {
        match open_path(&c.path, Access::Read) {
            Ok(h) => {
                let ev = unsafe { CreateEventW(null(), 1, 0, null()) };
                if ev == 0 {
                    open_denied.get_or_insert_with(|| format!("CreateEventW failed for {}", c.path));
                    continue;
                }
                let src = WinSource { h, ev: Handle(ev), ov: Box::new(zero_ov()), buf: Box::new([0u8; USB_REPORT_LEN]), pending: false };
                return OpenOutcome::Open(Box::new(src), DeviceInfo::from_candidate(&c, m));
            }
            Err(e) => {
                open_denied.get_or_insert_with(|| format!("GENERIC_READ open failed for {} (win32 {e})", c.path));
            }
        }
    }
    super::source::classify_no_open(transport, open_denied)
}

fn zero_ov() -> OVERLAPPED { OVERLAPPED { internal: 0, internal_high: 0, offset: 0, offset_high: 0, h_event: 0 } }

/// Overlapped input-report reader. The read buffer and OVERLAPPED are boxed so a pending read keeps a stable
/// address across timeouts; drop cancels and waits for the pending read before the handle closes.
pub struct WinSource { h: Handle, ev: Handle, ov: Box<OVERLAPPED>, buf: Box<[u8; USB_REPORT_LEN]>, pending: bool }
// The handles are plain kernel handle values used from one thread at a time.
unsafe impl Send for WinSource {}

impl ReportSource for WinSource {
    fn read(&mut self, out: &mut [u8], timeout_ms: u32) -> ReadOutcome {
        if !self.pending {
            *self.ov = zero_ov();
            self.ov.h_event = self.ev.0;
            let ok = unsafe { ReadFile(self.h.0, self.buf.as_mut_ptr(), USB_REPORT_LEN as u32, null_mut(), &mut *self.ov) };
            if ok == 0 {
                let e = unsafe { GetLastError() };
                if e != ERROR_IO_PENDING { return ReadOutcome::Disconnected(format!("ReadFile failed (Win32 error {e})")); }
            }
            self.pending = true;
        }
        match unsafe { WaitForSingleObject(self.ev.0, timeout_ms) } {
            WAIT_OBJECT_0 => {
                let mut n = 0u32;
                let ok = unsafe { GetOverlappedResult(self.h.0, &mut *self.ov, &mut n, 0) };
                self.pending = false;
                if ok == 0 { return ReadOutcome::Disconnected(format!("read completion failed (Win32 error {})", unsafe { GetLastError() })); }
                let n = (n as usize).min(out.len()).min(USB_REPORT_LEN);
                out[..n].copy_from_slice(&self.buf[..n]);
                ReadOutcome::Report(n)
            }
            WAIT_TIMEOUT => ReadOutcome::Timeout,
            other => ReadOutcome::Disconnected(format!("wait failed ({other})")),
        }
    }
}

impl Drop for WinSource {
    fn drop(&mut self) {
        if self.pending {
            let mut n = 0u32;
            unsafe {
                CancelIoEx(self.h.0, &mut *self.ov);
                GetOverlappedResult(self.h.0, &mut *self.ov, &mut n, 1);
            }
            self.pending = false;
        }
    }
}
