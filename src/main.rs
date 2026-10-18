use clap::Parser;
use log::{error, info, LevelFilter};
use qorcap::{api, cli::{Cli, Cmd}, engine::Engine, ffmpeg};
use std::sync::atomic::Ordering;
use std::sync::Arc;

fn init_log(path: Option<&str>) -> String {
    let p = path.map(String::from).unwrap_or_else(|| "qorcap.log".into());
    let cfg = simplelog::ConfigBuilder::new().set_time_format_rfc3339().build();
    let mut ls: Vec<Box<dyn simplelog::SharedLogger>> = vec![simplelog::TermLogger::new(LevelFilter::Info, cfg.clone(), simplelog::TerminalMode::Stderr, simplelog::ColorChoice::Never)];
    match std::fs::OpenOptions::new().create(true).append(true).open(&p) {
        Ok(f) => ls.push(simplelog::WriteLogger::new(LevelFilter::Info, cfg, f)),
        Err(e) => eprintln!("warning: cannot open log file {p}: {e}"),
    }
    let _ = simplelog::CombinedLogger::init(ls);
    p
}

fn print_resp(r: Result<(u16, String), String>) -> i32 {
    match r { Ok((c, b)) => { println!("{b}"); if c == 200 { 0 } else { 1 } } Err(e) => { eprintln!("error: {e}"); 2 } }
}

/// `qorcap finalize`: hand off to the Python finalizer shipped in sidecar/ next to the exe.
fn finalize(bundle: Option<String>, python: Option<String>, rest: Vec<String>, ff: &str) -> i32 { sidecar("finalize", bundle, python, rest, ff) }

/// Run `sidecar/qorcap_match.py <sub>` (finalize | submit) with QorCap's Python.
fn sidecar(sub: &str, bundle: Option<String>, python: Option<String>, rest: Vec<String>, ff: &str) -> i32 {
    let me = std::env::current_exe().unwrap_or_default();
    let base = me.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let roots = [base.clone(), base.join("..").join(".."), std::env::current_dir().unwrap_or_default()];
    let Some(script) = roots.iter().map(|r| r.join("sidecar").join("qorcap_match.py")).find(|p| p.exists()) else {
        eprintln!("error: sidecar/qorcap_match.py not found next to qorcap"); return 2;
    };
    let mut cands: Vec<Vec<String>> = Vec::new();
    if let Some(p) = python.or_else(|| std::env::var("QORCAP_PYTHON").ok()) { cands.push(vec![p]); }
    let home = std::env::var("QORCAP_HOME").map(std::path::PathBuf::from).ok().or_else(|| {
        if cfg!(windows) { std::env::var("LOCALAPPDATA").ok().map(|d| std::path::Path::new(&d).join("QorCap")) }
        else { std::env::var("HOME").ok().map(|d| std::path::Path::new(&d).join(".qorcap")) } });
    if let Some(h) = home {
        let v = if cfg!(windows) { h.join("venv").join("Scripts").join("python.exe") } else { h.join("venv").join("bin").join("python") };
        if v.exists() { cands.push(vec![v.to_string_lossy().into_owned()]); }
    }
    if cfg!(windows) { cands.push(vec!["py".into(), "-3".into()]); cands.push(vec!["python".into()]); } else { cands.push(vec!["python3".into()]); cands.push(vec!["python".into()]); }
    let mut args = vec![script.to_string_lossy().into_owned(), sub.to_string()];
    if let Some(b) = bundle { args.push(b); }
    args.extend(["--qorcap".into(), me.to_string_lossy().into_owned(), "--ffmpeg".into(), ff.to_string()]);
    let qt = base.join("qortime");
    if qt.join("qor_receipt").join("receipt.py").exists() && !rest.iter().any(|r| r == "--qortime") { args.extend(["--qortime".into(), qt.to_string_lossy().into_owned()]); }
    args.extend(rest);
    for c in cands {
        match std::process::Command::new(&c[0]).args(&c[1..]).args(&args).status() {
            Ok(s) => return s.code().unwrap_or(1),
            Err(_) => continue,
        }
    }
    eprintln!("error: Python not found. Run Start Match once (it sets Python up), or pass --python");
    2
}


/// Best-effort single-instance lock so a second Start Match does not spawn a competing sidecar.
/// Lock file is per control port under the QorCap data dir; stale locks (dead pid) are replaced.
struct InstanceLock { path: std::path::PathBuf, _file: std::fs::File }
impl Drop for InstanceLock {
    fn drop(&mut self) { let _ = std::fs::remove_file(&self.path); }
}
fn instance_lock(port: u16) -> Result<InstanceLock, String> {
    let root = std::env::var("QORCAP_HOME").map(std::path::PathBuf::from).ok().or_else(|| {
        if cfg!(windows) { std::env::var("LOCALAPPDATA").ok().map(|d| std::path::Path::new(&d).join("QorCap")) }
        else { std::env::var("HOME").ok().map(|d| std::path::Path::new(&d).join(".qorcap")) }
    }).unwrap_or_else(|| std::env::temp_dir().join("QorCap"));
    let _ = std::fs::create_dir_all(&root);
    let path = root.join(format!("qorcap-{port}.instance"));
    if path.exists() {
        if let Ok(s) = std::fs::read_to_string(&path) {
            let pid: u32 = s.trim().parse().unwrap_or(0);
            if pid != 0 && pid_alive(pid) {
                return Err(format!("QorCap is already running (pid {pid}, port {port}). Close that Start Match window first."));
            }
        }
        let _ = std::fs::remove_file(&path);
    }
    match std::fs::OpenOptions::new().write(true).create_new(true).open(&path) {
        Ok(mut f) => {
            use std::io::Write;
            let _ = writeln!(f, "{}", std::process::id());
            let _ = f.sync_all();
            Ok(InstanceLock { path, _file: f })
        }
        Err(e) => Err(format!("QorCap is already running (could not claim instance lock: {e}). Close the other Start Match window first.")),
    }
}
#[cfg(unix)]
fn pid_alive(pid: u32) -> bool { unsafe { libc::kill(pid as i32, 0) == 0 } }
#[cfg(windows)]
fn pid_alive(pid: u32) -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut std::ffi::c_void;
        fn CloseHandle(h: *mut std::ffi::c_void) -> i32;
    }
    const SYNCHRONIZE: u32 = 0x0010_0000;
    unsafe {
        let h = OpenProcess(SYNCHRONIZE, 0, pid);
        if h.is_null() { false } else { CloseHandle(h); true }
    }
}
#[cfg(not(any(unix, windows)))]
fn pid_alive(_: u32) -> bool { true }

/// Parent process of this recorder. Start Match's Python process is the parent; when that
/// console is closed the recorder notices and writes the bundle instead of dying with the shell.
#[cfg(unix)]
fn parent_pid() -> Option<u32> {
    let p = unsafe { libc::getppid() };
    if p <= 0 { None } else { Some(p as u32) }
}
#[cfg(windows)]
fn parent_pid() -> Option<u32> {
    #[link(name = "ntdll")]
    extern "system" {
        fn NtQueryInformationProcess(process: *mut std::ffi::c_void, class: u32, info: *mut std::ffi::c_void, len: u32, ret_len: *mut u32) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" { fn GetCurrentProcess() -> *mut std::ffi::c_void; }
    #[repr(C)]
    struct Pbi {
        exit_status: i32,
        peb: *mut std::ffi::c_void,
        affinity: usize,
        base_priority: i32,
        pid: usize,
        parent_pid: usize,
    }
    const _: () = assert!(std::mem::size_of::<Pbi>() == 48);
    unsafe {
        let mut info: Pbi = std::mem::zeroed();
        let mut got = 0u32;
        let st = NtQueryInformationProcess(GetCurrentProcess(), 0, &mut info as *mut Pbi as *mut _, std::mem::size_of::<Pbi>() as u32, &mut got);
        if st < 0 { return None; }
        let ppid = info.parent_pid as u32;
        if ppid == 0 { None } else { Some(ppid) }
    }
}
#[cfg(not(any(unix, windows)))]
fn parent_pid() -> Option<u32> { None }

/// True only when the OS reports that pid does not exist. Access denied is treated as still alive.
#[cfg(unix)]
fn parent_gone(pid: u32) -> bool {
    let rc = unsafe { libc::kill(pid as i32, 0) };
    rc != 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}
#[cfg(windows)]
fn parent_gone(pid: u32) -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut std::ffi::c_void;
        fn CloseHandle(h: *mut std::ffi::c_void) -> i32;
        fn GetLastError() -> u32;
    }
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const ERROR_INVALID_PARAMETER: u32 = 87;
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if !h.is_null() { CloseHandle(h); return false; }
        GetLastError() == ERROR_INVALID_PARAMETER
    }
}
#[cfg(not(any(unix, windows)))]
fn parent_gone(_: u32) -> bool { false }

/// A closed terminal delivers SIGHUP. Ignoring it lets the parent watcher stop the recorder
/// and write the bundle. Ctrl+C is still SIGINT.
#[cfg(unix)]
fn ignore_parent_hangup() { unsafe { libc::signal(libc::SIGHUP, libc::SIG_IGN); } }
#[cfg(not(unix))]
fn ignore_parent_hangup() {}

fn watch_parent(e: Arc<Engine>) {
    let Some(ppid) = parent_pid() else { return };
    if ppid == 0 || ppid == std::process::id() { return; }
    std::thread::spawn(move || {
        while !e.shutdown.load(Ordering::SeqCst) {
            std::thread::sleep(std::time::Duration::from_millis(400));
            if e.shutdown.load(Ordering::SeqCst) { break; }
            if parent_gone(ppid) {
                info!("launcher process {ppid} is gone; stopping recording");
                e.request_stop("launcher_gone");
                break;
            }
        }
    });
}

#[cfg(windows)]
static CONSOLE_ENGINE: std::sync::Mutex<Option<Arc<Engine>>> = std::sync::Mutex::new(None);

#[cfg(windows)]
extern "system" fn console_ctrl(ctrl: u32) -> i32 {
    let e = CONSOLE_ENGINE.lock().unwrap_or_else(|err| err.into_inner()).clone();
    let Some(e) = e else { return 0 };
    match ctrl {
        0 | 1 => {
            // CTRL_C / CTRL_BREAK: note it, then let the ctrlc handler finish the stop.
            e.note_stop(if ctrl == 0 { "ctrl_c" } else { "ctrl_break" });
            let _ = e.persist_stop_bundle();
            0
        }
        2 | 5 | 6 => {
            // Console close, logoff, shutdown. The process is about to be killed; write the bundle now.
            e.request_stop("console_close");
            1
        }
        _ => 0,
    }
}

#[cfg(windows)]
fn install_console_stop(e: Arc<Engine>) {
    #[link(name = "kernel32")]
    extern "system" {
        fn SetConsoleCtrlHandler(handler: Option<extern "system" fn(u32) -> i32>, add: i32) -> i32;
    }
    *CONSOLE_ENGINE.lock().unwrap_or_else(|err| err.into_inner()) = Some(e);
    unsafe { SetConsoleCtrlHandler(Some(console_ctrl), 1); }
}
#[cfg(not(windows))]
fn install_console_stop(_: Arc<Engine>) {}

/// `qorcap input-test`: live proof that the laptop is reading the DualSense over USB HID.
fn input_test(secs: Option<u64>) -> i32 {
    use qorcap::inputs::{spawn_logger, InputLog};
    use std::io::Write;
    use std::sync::{atomic::AtomicBool, Arc, Mutex};
    use std::time::{Duration, Instant};
    println!("QorCap input test: reading the DualSense / DualSense Edge over the laptop's USB cable.");
    println!("Keep playing on the PS5 as normal (Bluetooth); press buttons, move sticks, pull triggers. Ctrl+C to stop.");
    println!("Haptics: put the controller down or HOLD IT STILL for the first 3 s (IMU baseline calibration).");
    println!("Then make the PS5 rumble the pad (play a moment that vibrates: a hit, a tackle, a crash)\n");
    let log = Arc::new(Mutex::new(InputLog::new(None, true, 0)));
    let stop = Arc::new(AtomicBool::new(false));
    { let s = stop.clone(); let _ = ctrlc::set_handler(move || s.store(true, Ordering::SeqCst)); }
    let clock = Instant::now();
    let h = spawn_logger(log.clone(), clock, stop.clone());
    let (mut last_state, mut prev_btn, mut last_reports, mut last_tick) = (String::new(), Vec::<String>::new(), 0u64, Instant::now());
    let mut rate = 0u64; let mut ever_ok = false; let mut seen_haptic = 0u64; let mut calib_said = false;
    while !stop.load(Ordering::SeqCst) && secs.map(|s| clock.elapsed() < Duration::from_secs(s)).unwrap_or(true) {
        std::thread::sleep(Duration::from_millis(50));
        let lv = log.lock().unwrap().live.clone();
        if lv.state != last_state {
            match lv.state.as_str() {
                "connected" => { ever_ok = true; println!("\r\n[ OK ] HID is being read: {}", lv.device.clone().unwrap_or_default()); }
                "searching" => {}
                _ => println!("\r\n[WARN] {}", lv.warning.clone().unwrap_or_else(|| lv.state.clone())),
            }
            last_state = lv.state.clone();
        }
        if last_tick.elapsed() >= Duration::from_secs(1) { rate = lv.reports - last_reports; last_reports = lv.reports; last_tick = Instant::now(); }
        let newly: Vec<&String> = lv.buttons.iter().filter(|b| !prev_btn.contains(b)).collect();
        if !newly.is_empty() {
            println!("\r{:>8.2}s  PRESS {}{}", clock.elapsed().as_secs_f64(), newly.iter().map(|s| s.as_str()).collect::<Vec<_>>().join("+"), " ".repeat(40));
        }
        prev_btn = lv.buttons.clone();
        if lv.haptic.imu_present && !lv.haptic.calibrating && !calib_said {
            calib_said = true;
            println!("\r{:>8.2}s  IMU baseline calibrated; haptic detection is live{}", clock.elapsed().as_secs_f64(), " ".repeat(40));
        }
        if lv.haptic.events > seen_haptic {
            let pend = log.lock().unwrap().pending().iter().rev().find(|r| r["type"] == "haptic").cloned();
            if let Some(e) = pend {
                println!("\r{:>8.2}s  HAPTIC  peak={}x dur={}ms energy={} confidence={} ({}){}{}", e["t_us"].as_u64().unwrap_or(0) as f64 / 1e6,
                    e["peak"], e["duration_ms"], e["energy"], e["confidence"], e["confidence_label"].as_str().unwrap_or(""),
                    if e["flags"].as_array().map(|a| !a.is_empty()).unwrap_or(false) { " [near a button press]" } else { "" }, " ".repeat(20));
            }
            seen_haptic = lv.haptic.events;
        }
        if lv.state == "connected" {
            print!("\r  {rate:>4} reports/s | held: {:<18} | L2 {:>3} R2 {:>3} | LS {:>3},{:>3} RS {:>3},{:>3} | presses {}   ",
                if lv.buttons.is_empty() { "-".into() } else { lv.buttons.join("+") }, lv.l2, lv.r2, lv.sticks[0], lv.sticks[1], lv.sticks[2], lv.sticks[3], lv.presses);
            let _ = std::io::stdout().flush();
        }
    }
    stop.store(true, Ordering::SeqCst);
    let _ = h.join();
    let l = log.lock().unwrap();
    println!("\n\nSummary: {} HID reports read, {} logged, {} button presses.", l.live.reports, l.live.logged, l.live.presses);
    if l.live.reports == 0 {
        println!("RESULT: FAIL. No HID reports were read. {}", if ever_ok { "The controller was found but sent nothing." } else { "The DualSense was not visible over USB on this laptop. Check the USB cable into the laptop; the PS5 Bluetooth link stays as is." });
        return 1;
    }
    let hl = &l.live.haptic;
    println!("\nIMU / haptics:");
    if hl.imu_present {
        println!("[ OK ] IMU readable: {} IMU reports at {} reports/s (band seen: 20..{} Hz)", hl.imu_reports, hl.imu_rate_hz,
            ((hl.imu_rate_hz as f64 * 0.45) as u32).min(qorcap::haptics::BAND_TOP_HZ as u32));
        if hl.imu_rate_hz < 150 { println!("[WARN] IMU rate is low; short or high-pitched vibrations may be missed."); }
        if hl.calibrating { println!("[WARN] Test ended before the 3 s still-baseline finished; run it longer."); }
        println!("       haptic events: {} ({} low-confidence, near a button press)", hl.events, hl.low_confidence);
        if hl.events == 0 && !hl.calibrating { println!("       No vibration seen. If the PS5 was rumbling, note it: detection is advisory and varies by game."); }
    } else {
        println!("[WARN] IMU not readable: reports carried no gyro/accelerometer data, so haptic detection is OFF.");
    }
    let hs = qorcap::humanlike::score(l.pending());
    println!("RESULT: OK. The laptop is reading the controller over USB HID.");
    println!("Human-likeness of this test (needs ~40+ presses to mean much): {} ({}, confidence {})", hs["score_permille"], hs["label"].as_str().unwrap_or(""), hs["confidence"].as_str().unwrap_or(""));
    for r in hs["reasons"].as_array().into_iter().flatten() { println!("  - {}", r.as_str().unwrap_or("")); }
    0
}

#[cfg(test)]
mod tests {
    #[test]
    fn parent_watch_sees_a_live_parent_and_a_missing_pid() {
        let parent = super::parent_pid().expect("parent pid");
        assert!(parent > 0);
        assert!(!super::parent_gone(parent));
        assert!(!super::parent_gone(std::process::id()));
        assert!(super::parent_gone(2_000_000_001));
    }
}

fn main() {
    let cli = Cli::parse();
    let ff = ffmpeg::find_ffmpeg(cli.ffmpeg.as_deref());
    let code = match cli.cmd {
        Cmd::Devices => match ffmpeg::list_devices(&ff) {
            Ok(d) => {
                for k in ["video", "audio"] {
                    println!("{k} devices:");
                    for x in d.iter().filter(|x| x.kind == k) { println!("  \"{}\"", x.name); }
                }
                if d.is_empty() { eprintln!("no DirectShow devices found (is ffmpeg a Windows build? used: {ff})"); 1 } else { 0 }
            }
            Err(e) => { eprintln!("error: cannot run ffmpeg `{ff}`: {e}. Put ffmpeg.exe next to qorcap.exe or pass --ffmpeg"); 2 }
        },
        Cmd::SaveReplay { out } => print_resp(api::call(cli.port, "POST", "/replay/save", &serde_json::json!({"out": out}).to_string())),
        Cmd::Stop => print_resp(api::call(cli.port, "POST", "/shutdown", "")),
        Cmd::Status => print_resp(api::call(cli.port, "GET", "/status", "")),
        Cmd::VerifyBundle { dir } => match qorcap::bundle::verify(std::path::Path::new(&dir)) {
            Ok(r) => {
                println!("segments={} bytes={} checkpoints={} chain_head={}", r.segments, r.bytes, r.checkpoints, r.chain_head);
                if let Some(h) = &r.input_head { println!("controller input chunks={} inputs_chain_head={h}", r.input_chunks); }
                for e in &r.errors { println!("FAIL: {e}"); }
                println!("{}", if r.errors.is_empty() { "OK: video bytes match the hash chain; checkpoints link. (Integrity + timing only; does not prove who played.)" } else { "FAILED" });
                if r.errors.is_empty() { 0 } else { 1 }
            }
            Err(e) => { eprintln!("error: {e}"); 2 }
        },
        Cmd::Finalize { bundle, python, recover, mut rest } => {
            if recover { rest.insert(0, "--recover".into()); }
            finalize(bundle, python, rest, &ff)
        }
        Cmd::Submit { bundle, python, full, referee, yes } => {
            let mut rest = Vec::new();
            if full { rest.push("--full".to_string()); }
            if yes { rest.push("--yes".to_string()); }
            if let Some(r) = referee { rest.extend(["--referee".to_string(), r]); }
            sidecar("submit", bundle, python, rest, &ff)
        }
        Cmd::InputTest { secs } => input_test(secs),
        Cmd::InputProof { dir, frames, no_video, force } => {
            let d = std::path::Path::new(&dir);
            if qorcap::inputproof::recorder_running(d) && !force {
                eprintln!("error: QorCap is still recording into {dir} (inputs, haptics and video are still growing).");
                eprintln!("       Stop the match first; Start Match writes input_proof.json itself after the stop.");
                eprintln!("       (Pass --force to compute a provisional proof anyway; it will carry a warning.)");
                std::process::exit(3);
            }
            match qorcap::inputproof::apply(d, if no_video { None } else { Some(ff.as_str()) }, frames) {
                Ok(p) => { println!("{}", serde_json::to_string_pretty(&p).unwrap()); if p["status"] == "ok" { 0 } else { 1 } }
                Err(e) => { eprintln!("error: {e}"); 2 }
            }
        }
        Cmd::Record(mut a) => {
            let _lock = match instance_lock(cli.port) {
                Ok(l) => l,
                Err(m) => { eprintln!("error: {m}"); std::process::exit(4); }
            };
            if let Some(b) = a.bundle.clone() {
                let d = std::path::Path::new(&b);
                if let Err(e) = std::fs::create_dir_all(d.join("clips")) { eprintln!("error: cannot create bundle dir {b}: {e}"); std::process::exit(2); }
                if a.out.is_none() { a.out = Some(d.join("video.mp4").to_string_lossy().into_owned()); }
                if a.replay.is_none() { a.replay = Some(90); }
                a.replay_dir = d.join("clips").to_string_lossy().into_owned();
            }
            let cli_log = cli.log.clone().or_else(|| a.bundle.as_ref().map(|b| std::path::Path::new(b).join("qorcap.log").to_string_lossy().into_owned()));
            let lp = init_log(cli_log.as_deref());
            info!("qorcap {} starting; log file {lp}; ffmpeg {ff}", env!("CARGO_PKG_VERSION"));
            if a.device == "testpattern" && !cfg!(feature = "test-pattern") { error!("testpattern device requires the test-pattern feature"); std::process::exit(2); }
            if a.device != "testpattern" {
                let apps = ffmpeg::capture_apps_running();
                if !apps.is_empty() {
                    eprintln!("\n  Close OBS. It's using the capture card. ({} is running)\n", apps.join(", "));
                    log::warn!("{} is running; it may hold \"{}\" exclusively", apps.join(", "), a.device);
                }
            }
            let e = Engine::new(a.clone(), ff);
            if a.no_controller { info!("controller input logging disabled (--no-controller)"); } else { let _ = e.start_input_logger(); }
            let srv = match api::serve(e.clone(), cli.port) { Ok(h) => h, Err(m) => { error!("{m}"); std::process::exit(2); } };
            { let e = e.clone(); let _ = ctrlc::set_handler(move || { info!("Ctrl+C: stopping"); e.request_stop("ctrl_c"); }); }
            ignore_parent_hangup();
            install_console_stop(e.clone());
            watch_parent(e.clone());
            if let Some(out) = &a.out {
                if let Err(m) = e.start_recording(out, a.duration) { error!("{m}"); std::process::exit(2); }
                if a.replay.is_none() { e.exit_when_recording_ends.store(true, Ordering::SeqCst); }
            } else if a.replay.is_none() {
                info!("no --out and no --replay: preview/API-only mode (use POST /record/start)");
            }
            e.run();
            let _ = srv.join();
            drop(_lock);
            info!("bye");
            0
        }
    };
    std::process::exit(code);
}
