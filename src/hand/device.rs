//! Which devices the hand logger may open, and with what access. Platform-neutral so it is unit-tested on
//! every CI runner; `win.rs` calls these before any handle is opened for reading.

use crate::hid::{PID_DUALSENSE, PID_DUALSENSE_EDGE, SONY_VID};

/// Win32 `GENERIC_READ`. The only access right the capture handle ever asks for.
pub const GENERIC_READ: u32 = 0x8000_0000;
/// Query-only open (attributes, caps, serial string). No read or write access at all.
pub const QUERY_ONLY: u32 = 0;
/// USB input report 0x01 length, report id included.
pub const USB_REPORT_LEN: usize = 64;
/// HID usage page / usage of the gamepad top-level collection. Anything else is never opened for reading.
pub const USAGE_PAGE_GENERIC_DESKTOP: u16 = 0x01;
pub const USAGE_GAME_PAD: u16 = 0x05;

/// How a path is opened. There is no write variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access { QueryOnly, Read }

/// The `dwDesiredAccess` for each open. Only `0` and `GENERIC_READ` can come out of here.
pub fn access_mask(a: Access) -> u32 {
    match a { Access::QueryOnly => QUERY_ONLY, Access::Read => GENERIC_READ }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeviceModel { DualSenseEdge, DualSense }

impl DeviceModel {
    /// The label written in the header, status rows, hb rows and chunk rows.
    pub fn label(self) -> &'static str { match self { Self::DualSenseEdge => "dualsense_edge", Self::DualSense => "dualsense" } }
    /// A standard DualSense is a labelled fallback. It is never relabelled as an Edge.
    pub fn fallback(self) -> bool { matches!(self, Self::DualSense) }
    /// Edge-only fields (FN_L/R, BACK_L/R, profile-swap candidates) are MEASURED on an Edge only.
    pub fn edge_fields_measured(self) -> bool { matches!(self, Self::DualSenseEdge) }
}

/// VID/PID gate. 0x054C:0x0DF2 is primary, 0x054C:0x0CE6 is the labelled fallback, anything else fails closed.
pub fn classify(vid: u16, pid: u16) -> Option<DeviceModel> {
    if vid != SONY_VID { return None; }
    match pid { PID_DUALSENSE_EDGE => Some(DeviceModel::DualSenseEdge), PID_DUALSENSE => Some(DeviceModel::DualSense), _ => None }
}

/// Bluetooth HID paths (BTHENUM or the HID-over-Bluetooth service GUID). Wired USB only.
pub fn is_bluetooth_path(path: &str) -> bool {
    let p = path.to_ascii_lowercase();
    p.contains("bthenum") || p.contains("{00001124-") || p.contains("bthledevice")
}

/// What enumeration learned about one HID interface (metadata only, no report traffic).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub path: String,
    pub vid: u16,
    pub pid: u16,
    pub bcd_device: u16,
    pub usage_page: u16,
    pub usage: u16,
    pub input_report_len: u16,
    pub serial: Option<String>,
}

/// Why a candidate was not opened for reading.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reject {
    /// Not a Sony DualSense / DualSense Edge VID/PID (fails closed, not reported per device).
    OtherDevice,
    /// A DualSense-family pad, but over Bluetooth (or with the 78 B Bluetooth report size).
    Transport(String),
    /// A DualSense-family interface that is not the 64 B gamepad top-level collection.
    NotGamepadCollection(String),
}

/// The full open gate. Note on interface numbers: a wired DualSense / DualSense Edge exposes its HID
/// gamepad collection on USB interface 3 (`...&MI_03#...` in the HID device path; the audio interfaces are
/// 0-2 and are not HID-class devices, so HID enumeration never returns them). The gate is therefore the
/// HID-class enumeration plus UsagePage 0x01 / Usage 0x05 / 64 B input reports, not the `MI_xx` text.
pub fn gate(c: &Candidate) -> Result<DeviceModel, Reject> {
    let model = classify(c.vid, c.pid).ok_or(Reject::OtherDevice)?;
    if is_bluetooth_path(&c.path) { return Err(Reject::Transport(format!("{} over Bluetooth; wired USB only", model.label()))); }
    if c.usage_page != USAGE_PAGE_GENERIC_DESKTOP || c.usage != USAGE_GAME_PAD {
        return Err(Reject::NotGamepadCollection(format!("usage {:#06x}/{:#06x} is not the gamepad collection", c.usage_page, c.usage)));
    }
    if c.input_report_len as usize != USB_REPORT_LEN {
        return Err(Reject::Transport(format!("input report length {} (USB 0x01 is 64 B)", c.input_report_len)));
    }
    Ok(model)
}

/// The device facts the timeline records once a pad is open.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceInfo {
    pub model: DeviceModel,
    pub vid: u16,
    pub pid: u16,
    pub bcd_device: u16,
    pub pad_id: String,
}

impl DeviceInfo {
    pub fn from_candidate(c: &Candidate, model: DeviceModel) -> Self {
        let pad_id = crate::hid::pad_identity(c.serial.as_deref(), Some(&c.path), None);
        Self { model, vid: c.vid, pid: c.pid, bcd_device: c.bcd_device, pad_id }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cand(vid: u16, pid: u16, path: &str, up: u16, u: u16, len: u16) -> Candidate {
        Candidate { path: path.into(), vid, pid, bcd_device: 0x0408, usage_page: up, usage: u, input_report_len: len, serial: Some("S1".into()) }
    }
    const USB_PATH: &str = r"\\?\hid#vid_054c&pid_0df2&mi_03#8&1&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}";
    #[test]
    fn access_masks_are_read_only() {
        assert_eq!(access_mask(Access::Read), 0x8000_0000, "GENERIC_READ only");
        assert_eq!(access_mask(Access::QueryOnly), 0);
        for a in [Access::Read, Access::QueryOnly] {
            assert_eq!(access_mask(a) & 0x4000_0000, 0, "no write access bit");
            assert_eq!(access_mask(a) & 0x1000_0000, 0, "no all-access bit");
            assert_eq!(access_mask(a) & 0x0000_0002, 0, "no file write-data bit");
        }
    }
    #[test]
    fn edge_primary_dualsense_fallback_others_fail_closed() {
        assert_eq!(classify(0x054C, 0x0DF2), Some(DeviceModel::DualSenseEdge));
        assert_eq!(classify(0x054C, 0x0CE6), Some(DeviceModel::DualSense));
        assert_eq!(classify(0x054C, 0x09CC), None, "DualShock 4 fails closed");
        assert_eq!(classify(0x045E, 0x0B12), None, "other vendor fails closed");
        assert_eq!(DeviceModel::DualSense.label(), "dualsense");
        assert!(DeviceModel::DualSense.fallback() && !DeviceModel::DualSense.edge_fields_measured());
        assert!(!DeviceModel::DualSenseEdge.fallback() && DeviceModel::DualSenseEdge.edge_fields_measured());
    }
    #[test]
    fn gate_takes_the_wired_gamepad_collection_only() {
        assert_eq!(gate(&cand(0x054C, 0x0DF2, USB_PATH, 1, 5, 64)), Ok(DeviceModel::DualSenseEdge));
        let ds = USB_PATH.replace("pid_0df2", "pid_0ce6");
        assert_eq!(gate(&cand(0x054C, 0x0CE6, &ds, 1, 5, 64)), Ok(DeviceModel::DualSense));
        assert_eq!(gate(&cand(0x1234, 0x0DF2, USB_PATH, 1, 5, 64)), Err(Reject::OtherDevice));
        let bt = r"\\?\hid#{00001124-0000-1000-8000-00805f9b34fb}_vid&0002054c_pid&0df2#9&2&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}";
        assert!(matches!(gate(&cand(0x054C, 0x0DF2, bt, 1, 5, 64)), Err(Reject::Transport(_))));
        assert!(matches!(gate(&cand(0x054C, 0x0DF2, USB_PATH, 1, 5, 78)), Err(Reject::Transport(_))), "78 B is the Bluetooth report");
        assert!(matches!(gate(&cand(0x054C, 0x0DF2, USB_PATH, 0x0C, 1, 64)), Err(Reject::NotGamepadCollection(_))));
    }
    #[test]
    fn device_info_uses_the_hid_pad_identity_rule() {
        let c = cand(0x054C, 0x0DF2, USB_PATH, 1, 5, 64);
        assert_eq!(DeviceInfo::from_candidate(&c, DeviceModel::DualSenseEdge).pad_id, "serial:S1");
        let mut n = c.clone(); n.serial = None;
        assert!(DeviceInfo::from_candidate(&n, DeviceModel::DualSenseEdge).pad_id.starts_with("instance:"));
    }
}
