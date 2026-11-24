//! Read-only preflight checks for practice and tester capture sessions (`qorcap preflight`).
//!
//! Checks volume free space, writable target directory ancestry, listen-only controller
//! visibility (DualSense Edge / DualSense over wired USB; never writes to HID), and
//! user-declared DualSense Edge settings before recording begins.

use crate::disk;
use crate::hand::DeclaredEdgeSettings;
use serde::Serialize;
use serde_json::Value;
use std::fs::OpenOptions;
use std::path::{Path, PathBuf};

/// Advisory headroom floor for multi-day 1 kHz USB capture (~0.60 GB/h).
pub const ADVISORY_FREE_GB: f64 = 15.0;

#[derive(Debug, Clone)]
pub struct PreflightOpts {
    pub bundle_dir: Option<PathBuf>,
    pub hand_dir: Option<PathBuf>,
    pub min_free_gb: f64,
    pub allow_no_controller: bool,
    pub require_declared_edge: bool,
    pub declared_settings: DeclaredEdgeSettings,
}

impl Default for PreflightOpts {
    fn default() -> Self {
        Self {
            bundle_dir: None,
            hand_dir: None,
            min_free_gb: 5.0,
            allow_no_controller: false,
            require_declared_edge: false,
            declared_settings: DeclaredEdgeSettings::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CheckItem {
    pub name: String,
    pub status: String,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PreflightReport {
    pub pass: bool,
    pub controller_model: String,
    pub checks: Vec<CheckItem>,
    pub declared_settings: Value,
    pub warnings: Vec<String>,
    pub errors: Vec<String>,
}

fn nearest_existing_dir(path: &Path) -> Option<PathBuf> {
    let mut cur = if path.as_os_str().is_empty() {
        std::env::current_dir().ok()?
    } else {
        path.to_path_buf()
    };
    loop {
        if cur.exists() {
            return Some(if cur.is_dir() { cur } else { cur.parent()?.to_path_buf() });
        }
        if !cur.pop() {
            return std::env::current_dir().ok();
        }
    }
}

fn check_writable_probe(target: &Path) -> Result<PathBuf, String> {
    let existing = nearest_existing_dir(target)
        .ok_or_else(|| format!("cannot find existing ancestor for {}", target.display()))?;
    let meta = std::fs::metadata(&existing).map_err(|e| format!("cannot stat {}: {e}", existing.display()))?;
    if meta.permissions().readonly() {
        return Err(format!("{} is read-only", existing.display()));
    }
    let probe = existing.join(format!(
        ".qorcap-preflight-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let f = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&probe)
        .map_err(|e| format!("cannot write in {}: {e}", existing.display()))?;
    drop(f);
    let _ = std::fs::remove_file(&probe);
    Ok(existing)
}

fn default_bundle_probe_dir() -> PathBuf {
    #[cfg(windows)]
    if let Ok(profile) = std::env::var("USERPROFILE") {
        let docs = Path::new(&profile).join("Documents");
        if docs.is_dir() {
            return docs.join("QorCap Practice");
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn default_hand_probe_dir(bundle_target: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        if Path::new(r"D:\").is_dir() {
            return PathBuf::from(crate::hand::DEFAULT_WIN_HAND_ROOT);
        }
    }
    bundle_target.to_path_buf()
}

fn check_volume(
    name: &str,
    target: &Path,
    min_bytes: u64,
    warn_bytes: u64,
    checks: &mut Vec<CheckItem>,
    warnings: &mut Vec<String>,
    errors: &mut Vec<String>,
) {
    let existing = match check_writable_probe(target) {
        Ok(dir) => dir,
        Err(why) => {
            checks.push(CheckItem {
                name: name.into(),
                status: "FAIL".into(),
                detail: why.clone(),
            });
            errors.push(format!("{name}: {why}"));
            return;
        }
    };

    let Some(free) = disk::free_bytes(&existing) else {
        let msg = format!("cannot measure free space on {}", existing.display());
        checks.push(CheckItem {
            name: name.into(),
            status: "WARN".into(),
            detail: msg.clone(),
        });
        warnings.push(format!("{name}: {msg}"));
        return;
    };

    if min_bytes > 0 && free < min_bytes {
        let msg = format!(
            "{} writable, but {} free is below required {}",
            existing.display(),
            disk::fmt_bytes(free),
            disk::fmt_bytes(min_bytes)
        );
        checks.push(CheckItem {
            name: name.into(),
            status: "FAIL".into(),
            detail: msg.clone(),
        });
        errors.push(format!("{name}: {msg}"));
        return;
    }

    if warn_bytes > 0 && free < warn_bytes {
        let w = format!(
            "{} has {} free (below recommended {} for long multi-day 1 kHz sessions)",
            existing.display(),
            disk::fmt_bytes(free),
            disk::fmt_bytes(warn_bytes)
        );
        warnings.push(w.clone());
        checks.push(CheckItem {
            name: name.into(),
            status: "WARN".into(),
            detail: w,
        });
        return;
    }

    checks.push(CheckItem {
        name: name.into(),
        status: "PASS".into(),
        detail: format!("{} writable, {} free", existing.display(), disk::fmt_bytes(free)),
    });
}

fn probe_controller(allow_no_controller: bool) -> (CheckItem, String, Option<String>) {
    if allow_no_controller {
        return (
            CheckItem {
                name: "controller".into(),
                status: "SKIPPED".into(),
                detail: "controller check skipped (--allow-no-controller)".into(),
            },
            "SKIPPED".into(),
            None,
        );
    }

    #[cfg(windows)]
    {
        use crate::hand::source::OpenOutcome;
        match crate::hand::win::open_pad() {
            OpenOutcome::Open(src, info) => {
                drop(src);
                let model = info.model.label().to_string();
                let detail = format!(
                    "listen-only wired USB pad visible: {} ({:04x}:{:04x})",
                    model, info.vid, info.pid
                );
                (
                    CheckItem {
                        name: "controller".into(),
                        status: "PASS".into(),
                        detail,
                    },
                    model,
                    None,
                )
            }
            OpenOutcome::Rejected { state, detail } => {
                let msg = format!("{state}: {detail}");
                (
                    CheckItem {
                        name: "controller".into(),
                        status: "FAIL".into(),
                        detail: msg.clone(),
                    },
                    "UNKNOWN".into(),
                    Some(format!("controller: {msg}")),
                )
            }
            OpenOutcome::Missing(detail) => (
                CheckItem {
                    name: "controller".into(),
                    status: "FAIL".into(),
                    detail: detail.clone(),
                },
                "UNKNOWN".into(),
                Some(format!("controller: {detail}")),
            ),
        }
    }

    #[cfg(not(windows))]
    {
        let msg = "listen-only USB HID probe is Windows-only (pass --allow-no-controller outside Windows)".to_string();
        (
            CheckItem {
                name: "controller".into(),
                status: "FAIL".into(),
                detail: msg.clone(),
            },
            "UNKNOWN".into(),
            Some(format!("controller: {msg}")),
        )
    }
}

pub fn run(opts: &PreflightOpts) -> PreflightReport {
    let mut checks = Vec::new();
    let mut warnings = Vec::new();
    let mut errors = Vec::new();

    let min_bytes = if opts.min_free_gb <= 0.0 || !opts.min_free_gb.is_finite() {
        0
    } else {
        (opts.min_free_gb * 1024.0 * 1024.0 * 1024.0) as u64
    };
    let warn_bytes = (ADVISORY_FREE_GB * 1024.0 * 1024.0 * 1024.0) as u64;

    let bundle_target = opts
        .bundle_dir
        .clone()
        .unwrap_or_else(default_bundle_probe_dir);
    let hand_target = opts
        .hand_dir
        .clone()
        .unwrap_or_else(|| default_hand_probe_dir(&bundle_target));

    check_volume(
        "bundle_storage",
        &bundle_target,
        min_bytes,
        warn_bytes,
        &mut checks,
        &mut warnings,
        &mut errors,
    );

    if hand_target != bundle_target {
        check_volume(
            "hand_storage",
            &hand_target,
            min_bytes,
            warn_bytes,
            &mut checks,
            &mut warnings,
            &mut errors,
        );
    }

    let (ctrl_item, controller_model, ctrl_err) = probe_controller(opts.allow_no_controller);
    checks.push(ctrl_item);
    if let Some(e) = ctrl_err {
        errors.push(e);
    }

    let ds = &opts.declared_settings;
    let missing_edge: Vec<&'static str> = [
        ("edge_profile", ds.edge_profile.as_deref()),
        ("l2_stop", ds.l2_stop.as_deref()),
        ("r2_stop", ds.r2_stop.as_deref()),
        ("stick_curve_left", ds.stick_curve_left.as_deref()),
        ("stick_curve_right", ds.stick_curve_right.as_deref()),
        ("in_game_trigger_effect", ds.in_game_trigger_effect.as_deref()),
    ]
    .into_iter()
    .filter_map(|(k, v)| if v.is_none() { Some(k) } else { None })
    .collect();

    if opts.require_declared_edge && !missing_edge.is_empty() {
        let msg = format!("missing declared Edge setting(s): {}", missing_edge.join(", "));
        checks.push(CheckItem {
            name: "declared_edge_settings".into(),
            status: "FAIL".into(),
            detail: msg.clone(),
        });
        errors.push(format!("declared_edge_settings: {msg}"));
    } else if ds.any_set() {
        checks.push(CheckItem {
            name: "declared_edge_settings".into(),
            status: "PASS".into(),
            detail: if missing_edge.is_empty() {
                "all 6 DualSense Edge fields declared (checked: false)".into()
            } else {
                format!(
                    "partial DualSense Edge settings declared (checked: false; unset: {})",
                    missing_edge.join(", ")
                )
            },
        });
    } else {
        let w = "no DualSense Edge settings declared (fields default to UNKNOWN; checked: false)".to_string();
        warnings.push(w.clone());
        checks.push(CheckItem {
            name: "declared_edge_settings".into(),
            status: "WARN".into(),
            detail: w,
        });
    }

    PreflightReport {
        pass: errors.is_empty(),
        controller_model,
        checks,
        declared_settings: ds.to_json(),
        warnings,
        errors,
    }
}
