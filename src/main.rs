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
        Cmd::Record(mut a) => {
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
            info!("bye");
            0
        }
    };
    std::process::exit(code);
}
