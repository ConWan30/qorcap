//! Local control API on 127.0.0.1 (HTTP/JSON) + a tiny std-only client.
use crate::engine::Engine;
use log::{info, warn};
use serde_json::{json, Value};
#[allow(unused_imports)]
use std::io::BufRead;
use std::io::{Read, Write};
use std::sync::atomic::Ordering;
use std::sync::Arc;

pub struct Resp { pub code: u16, pub ctype: &'static str, pub body: Vec<u8>, pub headers: Vec<(&'static str, String)> }

/// JPEG always. PNG only when the opt-in lossless snapshot is on. Anything else is "no frame yet",
/// which is what a partial atomic rewrite looks like.
fn snapshot_kind(d: &[u8], png_ok: bool) -> Option<&'static str> {
    if d.len() > 2 && d[0] == 0xFF && d[1] == 0xD8 { return Some("image/jpeg"); }
    if png_ok && d.starts_with(b"\x89PNG\r\n\x1a\n") { return Some("image/png"); }
    None
}
fn j(code: u16, v: Value) -> Resp { Resp { code, ctype: "application/json", body: v.to_string().into_bytes(), headers: vec![] } }

pub fn route(e: &Arc<Engine>, method: &str, url: &str, body: &str) -> Resp {
    let path = url.split('?').next().unwrap_or(url);
    let b: Value = serde_json::from_str(body).unwrap_or(json!({}));
    match (method, path) {
        ("GET", "/status") | ("GET", "/") => j(200, serde_json::to_value(e.status()).unwrap()),
        ("POST", "/record/start") => {
            let out = b["out"].as_str().map(String::from).or_else(|| e.args.out.clone())
                .unwrap_or_else(|| format!("qorcap-{}.mp4", crate::engine::now_stamp()));
            match e.start_recording(&out, b["duration"].as_u64()) { Ok(p) => j(200, json!({"ok": true, "recording": p})), Err(m) => j(409, json!({"ok": false, "error": m})) }
        }
        ("POST", "/record/stop") => match e.stop_recording() { Some(p) => j(200, json!({"ok": true, "file": p})), None => j(409, json!({"ok": false, "error": "not recording"})) },
        ("POST", "/replay/save") => match e.save_replay(b["out"].as_str()) { Ok(p) => j(200, json!({"ok": true, "file": p})), Err(m) => j(409, json!({"ok": false, "error": m})) },
        ("GET", "/snapshot.jpg") => match std::fs::read(&e.snapshot) {
            Ok(d) => match snapshot_kind(&d, e.args.reader_snapshot == "png") {
                Some(ctype) => {
                    // Glimpse stays on the JPEG the reader already pulled. A PNG snapshot is not offered.
                    if ctype == "image/jpeg" { e.tap.offer(&d, crate::engine::wall_ms()); }
                    Resp { code: 200, ctype, body: d, headers: vec![] }
                }
                None => j(503, json!({"ok": false, "error": "no frame yet"})),
            },
            _ => j(503, json!({"ok": false, "error": "no frame yet"})),
        },
        // Advisory Glimpse watcher: the newest frame the reader already pulled (no file read here).
        ("GET", "/frame/scorebug") => frame_scorebug(e),
        ("POST", "/challenge") => match e.challenge(&b) { Ok(v) => j(200, v), Err(m) => j(400, json!({"ok": false, "error": m})) },
        ("POST", "/events") => match e.event(&b) { Ok(v) => j(200, v), Err(m) => j(400, json!({"ok": false, "error": m})) },
        ("GET", "/checkpoints") => {
            let since: u64 = url.split_once("since=").and_then(|x| x.1.split('&').next()?.parse().ok()).unwrap_or(0);
            let st = e.ev.lock().unwrap_or_else(|e| e.into_inner());
            let cps: Vec<&Value> = st.checkpoints.iter().filter(|c| c["seq"].as_u64().unwrap_or(0) >= since).collect();
            j(200, json!({"chain_head": st.seg.head, "segments": st.seg.seq, "checkpoint_head": st.cps.prev, "checkpoints": cps}))
        }
        ("GET", "/timeline") => { let st = e.ev.lock().unwrap_or_else(|e| e.into_inner()); j(200, json!({"timeline": st.timeline, "score": st.score, "challenges": st.challenges, "clips": st.clips, "final": st.final_})) }
        ("POST", "/bundle/write") => match e.write_bundle() { Some(d) => j(200, json!({"ok": true, "dir": d})), None => j(500, json!({"ok": false})) },
        ("POST", "/shutdown") => {
            let reason = b["reason"].as_str().map(str::trim).filter(|s| {
                !s.is_empty() && s.len() <= 64 && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
            }).unwrap_or("shutdown");
            e.request_stop(reason);
            j(200, json!({"ok": true, "reason": e.stop_reason.lock().unwrap_or_else(|e| e.into_inner()).clone()}))
        }
        // Local referee progress: "I have countersigned every checkpoint through seq". Lets the stop path
        // hold the slow bundle work until the final checkpoints carry fresh referee stamps.
        ("POST", "/countersigned") => match b["seq"].as_i64() {
            Some(n) if n >= 0 => { e.countersigned.fetch_max(n, Ordering::SeqCst); j(200, json!({"ok": true, "through": e.countersigned.load(Ordering::SeqCst)})) }
            _ => j(400, json!({"ok": false, "error": "seq must be a non-negative integer"})),
        },
        _ => j(404, json!({"ok": false, "error": format!("no route {method} {path}")})),
    }
}

fn frame_scorebug(e: &Arc<Engine>) -> Resp {
    use crate::glimpse::TapErr;
    match e.tap.take(crate::engine::wall_ms()) {
        Ok(f) => {
            let mut h = vec![("X-Qorcap-Frame-Wall-Ms", f.wall_ms.to_string()), ("X-Qorcap-Frame-Seq", f.seq.to_string()),
                             ("X-Qorcap-Advisory", "glimpse-shadow; read-only; never feeds locks".to_string()), ("Cache-Control", "no-store".to_string())];
            if let Some(b) = e.args.bundle.as_deref().filter(|b| b.is_ascii() && !b.contains(['\r', '\n'])) { h.push(("X-Qorcap-Bundle", b.to_string())); }
            Resp { code: 200, ctype: "image/jpeg", body: f.jpeg.as_ref().clone(), headers: h }
        }
        Err(TapErr::Off) => j(404, json!({"ok": false, "error": "glimpse frame tap is off (record --glimpse-frame or QORCAP_GLIMPSE_FRAME=1)"})),
        Err(TapErr::Throttled { retry_ms }) => {
            let mut r = j(429, json!({"ok": false, "error": "rate limited", "retry_ms": retry_ms}));
            r.headers.push(("Retry-After", retry_ms.div_ceil(1000).max(1).to_string()));
            r
        }
        Err(TapErr::NoFrame) => j(503, json!({"ok": false, "error": "no frame yet"})),
        Err(TapErr::Stale { age_ms }) => j(503, json!({"ok": false, "error": "no fresh frame", "age_ms": age_ms})),
    }
}

pub fn serve(e: Arc<Engine>, port: u16) -> Result<std::thread::JoinHandle<()>, String> {
    let srv = tiny_http::Server::http(("127.0.0.1", port)).map_err(|err| format!("cannot bind 127.0.0.1:{port} (another qorcap running?): {err}"))?;
    info!("control API on http://127.0.0.1:{port}");
    Ok(std::thread::spawn(move || {
        while !e.api_closed.load(Ordering::SeqCst) {
            let Ok(Some(rq)) = srv.recv_timeout(std::time::Duration::from_millis(200)) else { continue };
            let e = e.clone();
            std::thread::spawn(move || handle(e, rq));
        }
    }))
}

fn handle(e: Arc<Engine>, mut rq: tiny_http::Request) {
            if rq.url().starts_with("/checkpoints/stream") {
                let (tx, rx) = std::sync::mpsc::channel();
                e.subs.lock().unwrap_or_else(|e| e.into_inner()).push(tx);
                // Take the raw socket so every event is flushed immediately (no chunk buffering).
                let resp = tiny_http::Response::empty(200)
                    .with_header(tiny_http::Header::from_bytes("Content-Type", "text/event-stream").unwrap())
                    .with_header(tiny_http::Header::from_bytes("Cache-Control", "no-cache").unwrap());
                let mut w = rq.upgrade("sse", resp);
                let mut msg = ": qorcap checkpoints\n\n".to_string();
                loop {
                    if w.write_all(msg.as_bytes()).and_then(|_| w.flush()).is_err() { return; }
                    msg = match rx.recv_timeout(std::time::Duration::from_secs(15)) {
                        Ok(m) => m,
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => ": keepalive\n\n".into(),
                        Err(_) => return,
                    };
                }
            }
            let mut body = String::new();
            let _ = rq.as_reader().take(1 << 16).read_to_string(&mut body);
            let r = route(&e, rq.method().as_str(), rq.url(), &body);
            if r.code >= 400 && rq.url() != "/snapshot.jpg" && !rq.url().starts_with("/frame/scorebug") { warn!("API {} {} -> {} {}", rq.method(), rq.url(), r.code, String::from_utf8_lossy(&r.body)); }
            let h = tiny_http::Header::from_bytes("Content-Type", r.ctype).unwrap();
            let mut resp = tiny_http::Response::from_data(r.body).with_status_code(r.code).with_header(h);
            for (k, v) in &r.headers {
                if let Ok(x) = tiny_http::Header::from_bytes(k.as_bytes(), v.as_bytes()) { resp = resp.with_header(x); }
            }
            let _ = rq.respond(resp);
}

/// Minimal HTTP/1.0 client for the CLI subcommands.
pub fn call(port: u16, method: &str, path: &str, body: &str) -> Result<(u16, String), String> {
    let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).map_err(|e| format!("no qorcap running on 127.0.0.1:{port} ({e})"))?;
    s.set_read_timeout(Some(std::time::Duration::from_secs(60))).ok();
    write!(s, "{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len()).map_err(|e| e.to_string())?;
    let mut out = String::new();
    s.read_to_string(&mut out).map_err(|e| e.to_string())?;
    let code = out.split_whitespace().nth(1).and_then(|c| c.parse().ok()).unwrap_or(0);
    Ok((code, out.split_once("\r\n\r\n").map(|x| x.1.to_string()).unwrap_or_default()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ring::tests::{pat, pkt};
    use clap::Parser;
    fn engine(extra: &[&str]) -> Arc<Engine> {
        static N: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!("qorcap-api-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)))
            .to_string_lossy().into_owned();
        let mut a = vec!["q", "record", "--device", "testpattern", "--bundle", &dir]; a.extend_from_slice(extra);
        let crate::cli::Cmd::Record(r) = crate::cli::Cli::try_parse_from(a).unwrap().cmd else { panic!() };
        Engine::new(r, "ffmpeg-does-not-exist".into())
    }
    #[test]
    fn status_and_404() {
        let e = engine(&["--replay", "10"]);
        let r = route(&e, "GET", "/status", "");
        assert_eq!(r.code, 200);
        let v: Value = serde_json::from_slice(&r.body).unwrap();
        assert_eq!(v["state"], "starting"); assert_eq!(v["fps_target"], 30); assert_eq!(v["replay_window"], 10);
        assert_eq!(route(&e, "GET", "/nope", "").code, 404);
        assert_eq!(route(&e, "POST", "/record/stop", "").code, 409);
        assert_eq!(route(&e, "GET", "/snapshot.jpg", "").code, 503);
        let body = String::from_utf8(r.body.clone()).unwrap();
        assert!(!body.contains("buffer_full_warnings") && !body.contains("no_signal") && !body.contains("usb_link"), "{body}");
        let shut = route(&e, "POST", "/shutdown", r#"{"reason":"enter"}"#);
        assert_eq!(shut.code, 200);
        assert_eq!(e.stop_reason.lock().unwrap_or_else(|e| e.into_inner()).as_deref(), Some("enter"));
        assert!(e.bundle_dir().unwrap().join("receipt.draft.json").is_file());
        assert!(e.bundle_dir().unwrap().join("bundle.json").is_file());
        route(&e, "POST", "/shutdown", r#"{"reason":"ctrl_c"}"#);
        assert_eq!(e.stop_reason.lock().unwrap_or_else(|e| e.into_inner()).as_deref(), Some("enter"));
    }
    #[test]
    fn replay_rules_and_ts_written() {
        let e = engine(&[]);
        assert_eq!(route(&e, "POST", "/replay/save", "").code, 409); // disabled
        let e = engine(&["--replay", "10"]);
        assert_eq!(route(&e, "POST", "/replay/save", "").code, 409); // empty
        let mut pmt = pkt(0x1000, false); pmt[1] |= 0x40;
        e.ingest(&[pat(0x1000), pmt, pkt(0x100, true), pkt(0x100, false)].concat());
        let dir = std::env::temp_dir().join(format!("qorcap-test-{}", std::process::id()));
        let out = dir.join("r.mp4").to_string_lossy().into_owned();
        let r = route(&e, "POST", "/replay/save", &json!({"out": out}).to_string());
        assert_eq!(r.code, 200);
        let v: Value = serde_json::from_slice(&r.body).unwrap();
        let f = v["file"].as_str().unwrap();
        assert!(f.ends_with(".ts"), "ffmpeg missing -> raw TS kept: {f}");
        assert_eq!(std::fs::metadata(f).unwrap().len(), 4 * 188);
        let _ = std::fs::remove_dir_all(dir);
    }
    #[test]
    fn qortime_events_challenge_checkpoints() {
        let e = engine(&["--match-id", "m42"]);
        let bad = route(&e, "POST", "/challenge", r#"{"nonce":"zz"}"#); assert_eq!(bad.code, 400);
        let r = route(&e, "POST", "/challenge", r#"{"nonce":"00112233445566778899aabbccddeeff","phase":"kickoff"}"#);
        assert_eq!(r.code, 200);
        let v: Value = serde_json::from_slice(&r.body).unwrap();
        assert_eq!(v["challenge"]["pattern"][1], "CROSS");
        assert_eq!(v["checkpoint"]["qorcap"]["nonce"], "00112233445566778899aabbccddeeff");
        let ev = |b: &str| { let r = route(&e, "POST", "/events", b); (r.code, serde_json::from_slice::<Value>(&r.body).unwrap()) };
        assert!(ev(r#"{"type":"score","home":0,"away":0,"quarter":1,"clock_s":900}"#).1["checkpoint"].is_object()); // first read
        assert!(ev(r#"{"type":"score","home":0,"away":0,"quarter":1,"clock_s":880}"#).1.get("checkpoint").is_none()); // clock tick only
        let sc = ev(r#"{"type":"score","home":7,"away":0,"quarter":1,"clock_s":800}"#).1;
        assert_eq!(sc["checkpoint"]["qorcap"]["trigger"], "score_change");
        assert_eq!(sc["checkpoint"]["qorcap"]["score"]["home"], 7);
        assert_eq!(ev(r#"{"type":"score","home":7,"away":0,"quarter":4,"clock_s":0,"clock_red":true}"#).1["checkpoint"]["qorcap"]["trigger"], "score_change");
        assert_eq!(ev(r#"{"type":"final","basis":"bogus","home":7,"away":0,"quarter":4}"#).0, 400);
        assert_eq!(ev(r#"{"type":"final","basis":"clock_zero","home":7,"away":7,"quarter":4}"#).0, 400);
        assert_eq!(ev(r#"{"type":"score","home":"7","away":0,"quarter":4}"#).0, 400);
        let r = route(&e, "GET", "/checkpoints?since=1", "");
        let v: Value = serde_json::from_slice(&r.body).unwrap();
        let cps = v["checkpoints"].as_array().unwrap();
        assert_eq!(cps[0]["seq"], 1);
        // the chain verifies end to end
        let all: Value = serde_json::from_slice(&route(&e, "GET", "/checkpoints", "").body).unwrap();
        let mut prev = crate::evidence::GENESIS.to_string();
        for c in all["checkpoints"].as_array().unwrap() { prev = crate::evidence::check_checkpoint(c, &prev).unwrap(); }
    }
    #[test]
    fn glimpse_frame_tap_is_opt_in_memory_only_and_rate_limited() {
        // Off by default: 404, and the reader's /snapshot.jpg path is unchanged.
        let off = engine(&[]);
        assert_eq!(route(&off, "GET", "/frame/scorebug", "").code, 404);
        std::fs::write(&off.snapshot, [0xFF, 0xD8, 0xFF, 0xD9]).unwrap();
        assert_eq!(route(&off, "GET", "/snapshot.jpg", "").code, 200);
        assert_eq!(route(&off, "GET", "/frame/scorebug", "").code, 404);
        // On: nothing until the reader has pulled a frame; the tap itself never opens the snapshot file.
        let e = engine(&["--glimpse-frame"]);
        std::fs::write(&e.snapshot, [0xFF, 0xD8, 7, 0xFF, 0xD9]).unwrap();
        assert_eq!(route(&e, "GET", "/frame/scorebug", "").code, 503);
        assert_eq!(route(&e, "GET", "/snapshot.jpg", "").code, 200); // the reader's own poll
        std::fs::remove_file(&e.snapshot).unwrap(); // gone from disk: the tap serves memory only
        let r = route(&e, "GET", "/frame/scorebug", "");
        assert_eq!((r.code, r.ctype, r.body.as_slice()), (200, "image/jpeg", &[0xFF, 0xD8, 7, 0xFF, 0xD9][..]));
        assert!(r.headers.iter().any(|(k, v)| *k == "X-Qorcap-Frame-Seq" && v == "1"));
        assert!(r.headers.iter().any(|(k, _)| *k == "X-Qorcap-Bundle"));
        assert!(!e.snapshot.exists(), "the tap must not write the snapshot file");
        let again = route(&e, "GET", "/frame/scorebug", "");
        assert_eq!(again.code, 429);
        assert!(again.headers.iter().any(|(k, _)| *k == "Retry-After"));
        // POST is not a route: nothing can be sent back through the tap.
        assert_eq!(route(&e, "POST", "/frame/scorebug", "{}").code, 404);
        // The engine's match state is untouched by the tap.
        assert!(e.ev.lock().unwrap_or_else(|e| e.into_inner()).timeline.is_empty());
    }
    #[test]
    fn png_snapshot_is_opt_in_and_jpeg_stays_the_default() {
        let off = engine(&[]);
        assert!(off.snapshot.ends_with("snapshot.jpg"));
        std::fs::write(&off.snapshot, b"\x89PNG\r\n\x1a\nrest").unwrap();
        assert_eq!(route(&off, "GET", "/snapshot.jpg", "").code, 503);
        std::fs::write(&off.snapshot, [0xFF, 0xD8, 0xFF, 0xD9]).unwrap();
        assert_eq!(route(&off, "GET", "/snapshot.jpg", "").ctype, "image/jpeg");
        let on = engine(&["--reader-snapshot", "png", "--glimpse-frame"]);
        assert!(on.snapshot.ends_with("snapshot.png"));
        assert_eq!(on.args.fps, 30);
        std::fs::write(&on.snapshot, b"\x89PNG\r\n\x1a\nrest").unwrap();
        let r = route(&on, "GET", "/snapshot.jpg", "");
        assert_eq!((r.code, r.ctype), (200, "image/png"));
        // Glimpse is not handed a PNG: the tap stays empty.
        assert_eq!(route(&on, "GET", "/frame/scorebug", "").code, 503);
        let yuy = engine(&["--yuy2-720p60"]);
        assert_eq!((yuy.args.res, yuy.args.fps, yuy.args.input_format.as_str()), ((1280, 720), 60, "yuyv422"));
    }
    #[test]
    fn snapshot_and_http_roundtrip() {
        let e = engine(&[]);
        std::fs::write(&e.snapshot, [0xFF, 0xD8, 0xFF, 0xD9]).unwrap();
        let r = route(&e, "GET", "/snapshot.jpg", ""); assert_eq!((r.code, r.ctype), (200, "image/jpeg"));
        let port = 18000 + (std::process::id() % 2000) as u16;
        let h = serve(e.clone(), port).unwrap();
        let (c, b) = call(port, "GET", "/status", "").unwrap();
        assert_eq!(c, 200); assert!(b.contains("\"device\":\"testpattern\""));
        // Off by default over HTTP too.
        assert_eq!(call(port, "GET", "/frame/scorebug", "").unwrap().0, 404);
        // SSE stream receives a checkpoint
        let mut sse = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
        write!(sse, "GET /checkpoints/stream HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(300));
        e.checkpoint("test", json!({}));
        sse.set_read_timeout(Some(std::time::Duration::from_secs(3))).unwrap();
        let mut got = String::new(); let mut b = [0u8; 4096];
        while !got.contains("event: checkpoint") { let n = sse.read(&mut b).unwrap(); assert!(n > 0); got.push_str(&String::from_utf8_lossy(&b[..n])); }
        assert_eq!(call(port, "POST", "/shutdown", "").unwrap().0, 200);
        h.join().unwrap();
        assert!(call(port, "GET", "/status", "").is_err());
    }
    #[test]
    fn stop_seals_chain_and_waits_for_referee() {
        let e = engine(&["--match-id", "m7"]);
        e.checkpoint("start", json!({}));
        // no referee attached: seal returns at once
        let t = std::time::Instant::now();
        let last = e.seal_and_wait_countersign(std::time::Duration::from_secs(5)).unwrap();
        assert!(t.elapsed() < std::time::Duration::from_secs(1));
        assert_eq!(last, 1);
        let all: Value = serde_json::from_slice(&route(&e, "GET", "/checkpoints", "").body).unwrap();
        assert_eq!(all["checkpoints"].as_array().unwrap().last().unwrap()["qorcap"]["trigger"], "stop");
        // nothing may be appended after the stop checkpoint (input proof, late scorebug reads)
        assert!(e.checkpoint("late", json!({})).is_null());
        let r = route(&e, "POST", "/events", r#"{"type":"score","home":3,"away":0,"quarter":4,"clock_s":0}"#);
        assert_eq!(r.code, 200);
        let all: Value = serde_json::from_slice(&route(&e, "GET", "/checkpoints", "").body).unwrap();
        assert_eq!(all["checkpoints"].as_array().unwrap().len(), 2);
        assert!(e.seal_and_wait_countersign(std::time::Duration::from_secs(1)).is_none());

        // referee attached: waits until it reports the stop checkpoint countersigned
        let e = engine(&[]);
        e.checkpoint("start", json!({}));
        assert_eq!(route(&e, "POST", "/countersigned", r#"{"seq":0}"#).code, 200);
        assert_eq!(route(&e, "POST", "/countersigned", r#"{"seq":-1}"#).code, 400);
        let e2 = e.clone();
        let h = std::thread::spawn(move || { std::thread::sleep(std::time::Duration::from_millis(300)); route(&e2, "POST", "/countersigned", r#"{"seq":1}"#).code });
        let t = std::time::Instant::now();
        assert_eq!(e.seal_and_wait_countersign(std::time::Duration::from_secs(10)), Some(1));
        let waited = t.elapsed();
        assert!(waited >= std::time::Duration::from_millis(250) && waited < std::time::Duration::from_secs(5), "{waited:?}");
        assert_eq!(h.join().unwrap(), 200);
        assert_eq!(e.status().final_checkpoint_seq, Some(1));
    }
    #[test]
    fn recording_marker_guards_input_proof() {
        let d = std::env::temp_dir().join(format!("qorcap-marker-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        assert!(!crate::inputproof::recorder_running(&d));
        crate::inputproof::touch_recording_marker(Some(&d));
        assert!(crate::inputproof::recorder_running(&d));
        let p = crate::inputproof::apply(&d, None, 15).unwrap();
        assert_eq!(p["recorder_running"], true);
        assert!(p["warnings"].as_array().unwrap().iter().any(|w| w.as_str().unwrap().contains("recorder was still running")));
        crate::inputproof::clear_recording_marker(Some(&d));
        assert!(!crate::inputproof::recorder_running(&d));
        let _ = std::fs::remove_dir_all(&d);
    }
}
