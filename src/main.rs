use clap::Parser;
use log::{error, info, LevelFilter};
use qorcap::{api, cli::{Cli, Cmd}, engine::Engine, ffmpeg};
use std::sync::atomic::Ordering;

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
fn finalize(bundle: Option<String>, python: Option<String>, rest: Vec<String>, ff: &str) -> i32 {
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
    let mut args = vec![script.to_string_lossy().into_owned(), "finalize".into()];
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
                for e in &r.errors { println!("FAIL: {e}"); }
                println!("{}", if r.errors.is_empty() { "OK: video bytes match the hash chain; checkpoints link. (Integrity + timing only; does not prove who played.)" } else { "FAILED" });
                if r.errors.is_empty() { 0 } else { 1 }
            }
            Err(e) => { eprintln!("error: {e}"); 2 }
        },
        Cmd::Finalize { bundle, python, rest } => finalize(bundle, python, rest, &ff),
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
            let srv = match api::serve(e.clone(), cli.port) { Ok(h) => h, Err(m) => { error!("{m}"); std::process::exit(2); } };
            { let e = e.clone(); let _ = ctrlc::set_handler(move || { info!("Ctrl+C: stopping"); e.shutdown.store(true, Ordering::SeqCst); }); }
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
