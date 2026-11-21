//! Listen-only audit for the hand logger (SCOPE-edge-logger section 2.3).
//!
//! 1. Static source audit: no denylisted token in non-comment code anywhere under src/hand/.
//! 2. Every OS function the module declares is on the allowlist, and only win.rs declares any.
//! 3. The one CreateFileW call takes its access mask from `device::access_mask`, which yields 0 or GENERIC_READ.
//! 4. Windows only: a handle opened through the module's own `open_path(.., Access::Read)` is refused a write
//!    by the OS (ERROR_ACCESS_DENIED), using the exact flags the pad reader uses. This test opens a regular
//!    file, not a HID handle, and does not call HidD_Set*. There is no PE import-table check (hidapi is
//!    already linked into the same binary for the existing pad path).

use std::path::{Path, PathBuf};

const DENY: &[&str] = &[
    "GENERIC_WRITE", "GENERIC_ALL", "FILE_WRITE_DATA", "FILE_APPEND_DATA", "FILE_ALL_ACCESS", "WriteFile", "DeviceIoControl",
    "HidD_SetOutputReport", "HidD_SetFeature", "HidD_GetFeature", "HidD_GetInputReport", "HidD_SetNumInputBuffers", "HidD_SetConfiguration",
    "HidD_FlushQueue", "IOCTL_HID_SET_", "IOCTL_HID_GET_FEATURE", "send_feature_report", "get_feature_report", "hidapi",
    "hid_write", "LoadLibrary", "GetProcAddress",
    "waveIn", "WASAPI", "IMMDeviceEnumerator", "IAudioClient", "DirectSound", "mmdevapi", "winmm",
];

const ALLOW: &[&str] = &[
    "SetupDiGetClassDevsW", "SetupDiEnumDeviceInterfaces", "SetupDiGetDeviceInterfaceDetailW", "SetupDiDestroyDeviceInfoList",
    "HidD_GetHidGuid", "HidD_GetAttributes", "HidD_GetPreparsedData", "HidD_FreePreparsedData", "HidP_GetCaps",
    "HidD_GetSerialNumberString", "HidD_GetProductString",
    "CreateFileW", "ReadFile", "GetOverlappedResult", "CreateEventW", "WaitForSingleObject", "CancelIoEx", "CloseHandle", "GetLastError",
    "QueryPerformanceCounter", "QueryPerformanceFrequency", "SetThreadPriority", "GetCurrentThread",
];

fn hand_dir() -> PathBuf { Path::new(env!("CARGO_MANIFEST_DIR")).join("src").join("hand") }

fn sources() -> Vec<(String, String)> {
    let mut v: Vec<(String, String)> = std::fs::read_dir(hand_dir()).unwrap().filter_map(Result::ok)
        .filter(|e| e.path().extension().is_some_and(|x| x == "rs"))
        .map(|e| (e.file_name().to_string_lossy().into_owned(), std::fs::read_to_string(e.path()).unwrap())).collect();
    v.sort();
    assert!(v.len() >= 6, "expected the hand module sources, got {:?}", v.iter().map(|x| &x.0).collect::<Vec<_>>());
    v
}

/// Code with `//` comments (incl. doc comments) and `/* */` blocks removed.
fn strip_comments(src: &str) -> String {
    let mut out = String::new();
    let mut in_block = false;
    for line in src.lines() {
        let mut l = line.to_string();
        if in_block { match l.find("*/") { Some(i) => { l = l[i + 2..].to_string(); in_block = false; } None => { out.push('\n'); continue; } } }
        while let Some(i) = l.find("/*") { match l[i..].find("*/") { Some(j) => l.replace_range(i..i + j + 2, ""), None => { l.truncate(i); in_block = true; break; } } }
        if let Some(i) = l.find("//") { l.truncate(i); }
        out.push_str(&l);
        out.push('\n');
    }
    out
}

#[test]
fn no_denylisted_token_in_hand_module_code() {
    for (name, src) in sources() {
        let code = strip_comments(&src);
        for d in DENY {
            assert!(!code.contains(d), "src/hand/{name}: denylisted `{d}` in code");
        }
    }
}

#[test]
fn every_declared_os_function_is_allowlisted_and_only_win_rs_declares_any() {
    let mut declared = vec![];
    for (name, src) in sources() {
        let code = strip_comments(&src);
        let mut rest = code.as_str();
        while let Some(i) = rest.find("extern \"") {
            assert_eq!(name, "win.rs", "src/hand/{name} declares an extern block");
            let block = &rest[i..];
            let end = block.find('}').expect("extern block end");
            for part in block[..end].split("fn ").skip(1) {
                let f: String = part.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                assert!(ALLOW.contains(&f.as_str()), "src/hand/{name}: `{f}` is not on the listen-only allowlist");
                declared.push(f);
            }
            rest = &block[end..];
        }
        assert!(!code.contains("#[link(name") || name == "win.rs", "src/hand/{name} links a native library");
    }
    assert!(declared.iter().any(|f| f == "CreateFileW") && declared.iter().any(|f| f == "ReadFile"), "{declared:?}");
}

#[test]
fn the_only_createfilew_call_takes_its_mask_from_access_mask() {
    let mut calls = vec![];
    for (name, src) in sources() {
        let code = strip_comments(&src);
        for (i, _) in code.match_indices("CreateFileW(") {
            if code[..i].ends_with("fn ") { continue; }
            let args = &code[i + "CreateFileW(".len()..];
            let args: Vec<&str> = args.splitn(3, ',').map(str::trim).collect();
            calls.push((name.clone(), args[1].to_string()));
        }
    }
    assert_eq!(calls.len(), 1, "exactly one CreateFileW call site: {calls:?}");
    assert_eq!(calls[0].1, "device::access_mask(access)", "{calls:?}");
    // access_mask can only yield query-only (0) or GENERIC_READ.
    let dev = strip_comments(&std::fs::read_to_string(hand_dir().join("device.rs")).unwrap());
    let body = &dev[dev.find("pub fn access_mask").unwrap()..];
    let body = &body[..body.find('}').unwrap()];
    assert!(body.contains("Access::QueryOnly => QUERY_ONLY") && body.contains("Access::Read => GENERIC_READ") && body.matches("=>").count() == 2, "{body}");
    use qorcap::hand::device::{access_mask, Access, GENERIC_READ};
    assert_eq!(GENERIC_READ, 0x8000_0000);
    assert_eq!(access_mask(Access::Read), 0x8000_0000);
    assert_eq!(access_mask(Access::QueryOnly), 0);
}

#[test]
fn report_source_has_read_only() {
    let src = strip_comments(&std::fs::read_to_string(hand_dir().join("source.rs")).unwrap());
    let t = &src[src.find("pub trait ReportSource").unwrap()..];
    let t = &t[..t.find('}').unwrap()];
    assert_eq!(t.matches("fn ").count(), 1, "the trait has a single method: {t}");
    assert!(t.contains("fn read(&mut self, buf: &mut [u8], timeout_ms: u32) -> ReadOutcome"), "{t}");
}

#[cfg(windows)]
mod kernel {
    use qorcap::hand::device::Access;
    #[repr(C)]
    #[allow(dead_code)]
    struct Overlapped { internal: usize, internal_high: usize, offset: u32, offset_high: u32, h_event: isize }
    #[link(name = "kernel32")]
    extern "system" {
        fn WriteFile(h: isize, buf: *const u8, len: u32, written: *mut u32, ov: *mut Overlapped) -> i32;
        fn GetLastError() -> u32;
    }
    #[test]
    fn os_refuses_a_write_on_a_handle_from_open_path_read() {
        // Regular file, not a HID handle. Does not call HidD_Set*. Confirms the access mask only.
        let dir = std::env::temp_dir().join(format!("qorcap-hand-ro-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("target.bin");
        std::fs::write(&p, b"unchanged").unwrap();
        {
            let h = qorcap::hand::win::open_path(p.to_str().unwrap(), Access::Read).expect("open read-only");
            let mut ov = Overlapped { internal: 0, internal_high: 0, offset: 0, offset_high: 0, h_event: 0 };
            let mut n = 0u32;
            let ok = unsafe { WriteFile(h.raw(), b"X".as_ptr(), 1, &mut n, &mut ov) };
            let err = unsafe { GetLastError() };
            assert_eq!(ok, 0, "write on a GENERIC_READ handle must fail");
            assert_eq!(err, 5, "ERROR_ACCESS_DENIED");
        }
        assert_eq!(std::fs::read(&p).unwrap(), b"unchanged");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
