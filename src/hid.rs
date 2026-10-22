//! DualSense / DualSense Edge HID input reports.
//!
//! Byte map ported from Qoresence `qoresence/sync/hid_report.py` (itself forked from QorTroller
//! `controller/hid_report_parser.py`, live-verified there): USB report 0x01 (64 B) and Bluetooth report
//! 0x31 (78 B, every field shifted +1). QorCap reads the pad over **USB** on the laptop while the same pad
//! talks to the PS5 over Bluetooth; that dual path is the intended setup.
//!
//! USB offsets: lx=1 ly=2 rx=3 ry=4 l2=5 r2=6 counter=7 buttons_0=8 (low nibble = d-pad hat)
//! buttons_1=9 buttons_2=10 gyro=16..22 (3 x i16 LE: pitch, yaw, roll) accel=22..28 (3 x i16 LE: x, y, z)
//! sensor_timestamp=28..32 (u32 LE, 0.33 us ticks).
//!
//! IMU offsets (since 0.3.1) follow Linux `drivers/hid/hid-playstation.c` `struct dualsense_input_report`
//! (x, y, rx, ry, z, rz, seq_number, buttons[4], reserved[4], gyro[3] __le16, accel[3] __le16,
//! sensor_timestamp __le32, ...) which starts right after the report id; DS4Windows' DualSense reader uses the
//! same layout. Identical for DualSense (0x0CE6) and DualSense Edge (0x0DF2). Values are raw, uncalibrated
//! counts (QorCap only looks at vibration *changes*, so per-pad calibration is not needed).

pub const SONY_VID: u16 = 0x054C;
pub const PID_DUALSENSE: u16 = 0x0CE6;
pub const PID_DUALSENSE_EDGE: u16 = 0x0DF2;

// Internal button bits. 0..=11 match Qoresence `lobes.controller.Buttons` / `sync.hid_report`.
pub const CROSS: u32 = 1 << 0;
pub const CIRCLE: u32 = 1 << 1;
pub const SQUARE: u32 = 1 << 2;
pub const TRIANGLE: u32 = 1 << 3;
pub const L1: u32 = 1 << 4;
pub const R1: u32 = 1 << 5;
pub const L2: u32 = 1 << 6;
pub const R2: u32 = 1 << 7;
pub const CREATE: u32 = 1 << 8;
pub const OPTIONS: u32 = 1 << 9;
pub const L3: u32 = 1 << 10;
pub const R3: u32 = 1 << 11;
// QorCap extensions (d-pad hat, system buttons, Edge Fn / back paddles).
pub const UP: u32 = 1 << 12;
pub const DOWN: u32 = 1 << 13;
pub const LEFT: u32 = 1 << 14;
pub const RIGHT: u32 = 1 << 15;
pub const PS: u32 = 1 << 16;
pub const TOUCHPAD: u32 = 1 << 17;
pub const MUTE: u32 = 1 << 18;
pub const FN_LEFT: u32 = 1 << 19;
pub const FN_RIGHT: u32 = 1 << 20;
pub const BACK_LEFT: u32 = 1 << 21;
pub const BACK_RIGHT: u32 = 1 << 22;

pub const BUTTON_NAMES: [(u32, &str); 23] = [
    (CROSS, "CROSS"), (CIRCLE, "CIRCLE"), (SQUARE, "SQUARE"), (TRIANGLE, "TRIANGLE"), (L1, "L1"), (R1, "R1"),
    (L2, "L2"), (R2, "R2"), (CREATE, "CREATE"), (OPTIONS, "OPTIONS"), (L3, "L3"), (R3, "R3"), (UP, "UP"),
    (DOWN, "DOWN"), (LEFT, "LEFT"), (RIGHT, "RIGHT"), (PS, "PS"), (TOUCHPAD, "TOUCHPAD"), (MUTE, "MUTE"),
    (FN_LEFT, "FN_L"), (FN_RIGHT, "FN_R"), (BACK_LEFT, "BACK_L"), (BACK_RIGHT, "BACK_R"),
];

/// "Key press" buttons used for video alignment: digital presses that usually change the screen.
pub const KEY_BUTTONS: u32 = CROSS | CIRCLE | SQUARE | TRIANGLE | L1 | R1 | OPTIONS | UP | DOWN | LEFT | RIGHT | TOUCHPAD;

pub fn names(mask: u32) -> Vec<&'static str> { BUTTON_NAMES.iter().filter(|(b, _)| mask & b != 0).map(|(_, n)| *n).collect() }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transport { Usb, Bluetooth }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Pad {
    pub transport: Transport,
    pub lx: u8, pub ly: u8, pub rx: u8, pub ry: u8,
    /// Analog trigger pressure 0..=255.
    pub l2: u8, pub r2: u8,
    pub buttons: u32,
    /// Report counter (byte 7), wraps at 256.
    pub counter: u8,
    /// Controller sensor timestamp (0.33 us ticks, wraps); 0 if absent.
    pub sensor_ts: u32,
    /// Raw gyro (pitch, yaw, roll) and accelerometer (x, y, z) counts; all zero when the report carries no IMU data.
    pub gyro: [i16; 3],
    pub accel: [i16; 3],
}

const B0_SQUARE: u8 = 0x10; const B0_CROSS: u8 = 0x20; const B0_CIRCLE: u8 = 0x40; const B0_TRIANGLE: u8 = 0x80;

fn hat(h: u8) -> u32 {
    match h & 0x0F { 0 => UP, 1 => UP | RIGHT, 2 => RIGHT, 3 => DOWN | RIGHT, 4 => DOWN, 5 => DOWN | LEFT, 6 => LEFT, 7 => UP | LEFT, _ => 0 }
}

/// Parse a raw input report (report id included as byte 0, as hidapi returns it).
pub fn parse(raw: &[u8]) -> Option<Pad> {
    let (transport, o) = if raw.len() >= 64 && raw[0] == 0x01 { (Transport::Usb, 0usize) }
        else if raw.len() >= 78 && raw[0] == 0x31 { (Transport::Bluetooth, 1usize) }
        else { return None; };
    let b0 = raw[8 + o]; let b1 = raw[9 + o]; let b2 = raw[10 + o];
    let mut m = hat(b0);
    if b0 & B0_SQUARE != 0 { m |= SQUARE; }
    if b0 & B0_CROSS != 0 { m |= CROSS; }
    if b0 & B0_CIRCLE != 0 { m |= CIRCLE; }
    if b0 & B0_TRIANGLE != 0 { m |= TRIANGLE; }
    for (bit, v) in [(0x01, L1), (0x02, R1), (0x04, L2), (0x08, R2), (0x10, CREATE), (0x20, OPTIONS), (0x40, L3), (0x80, R3)] {
        if b1 & bit != 0 { m |= v; }
    }
    for (bit, v) in [(0x01, PS), (0x02, TOUCHPAD), (0x04, MUTE), (0x10, FN_LEFT), (0x20, FN_RIGHT), (0x40, BACK_LEFT), (0x80, BACK_RIGHT)] {
        if b2 & bit != 0 { m |= v; }
    }
    let ts = u32::from_le_bytes([raw[28 + o], raw[29 + o], raw[30 + o], raw[31 + o]]);
    let i16at = |i: usize| i16::from_le_bytes([raw[i + o], raw[i + 1 + o]]);
    let gyro = [i16at(16), i16at(18), i16at(20)];
    let accel = [i16at(22), i16at(24), i16at(26)];
    Some(Pad { transport, lx: raw[1 + o], ly: raw[2 + o], rx: raw[3 + o], ry: raw[4 + o], l2: raw[5 + o], r2: raw[6 + o],
        buttons: m, counter: raw[7 + o], sensor_ts: ts, gyro, accel })
}

/// Build a synthetic USB 0x01 report (tests, `input-test --demo`).
pub fn pack_usb(p: &Pad) -> Vec<u8> {
    let mut r = vec![0u8; 64];
    r[0] = 0x01; r[1] = p.lx; r[2] = p.ly; r[3] = p.rx; r[4] = p.ry; r[5] = p.l2; r[6] = p.r2; r[7] = p.counter;
    let b = p.buttons;
    let h = match (b & UP != 0, b & RIGHT != 0, b & DOWN != 0, b & LEFT != 0) {
        (true, false, false, false) => 0, (true, true, _, _) => 1, (false, true, false, false) => 2, (_, true, true, _) => 3,
        (false, false, true, false) => 4, (_, _, true, true) => 5, (false, false, false, true) => 6, (true, _, _, true) => 7, _ => 8 };
    let mut b0 = h as u8;
    if b & SQUARE != 0 { b0 |= B0_SQUARE; } if b & CROSS != 0 { b0 |= B0_CROSS; }
    if b & CIRCLE != 0 { b0 |= B0_CIRCLE; } if b & TRIANGLE != 0 { b0 |= B0_TRIANGLE; }
    let mut b1 = 0u8;
    for (bit, v) in [(0x01, L1), (0x02, R1), (0x04, L2), (0x08, R2), (0x10, CREATE), (0x20, OPTIONS), (0x40, L3), (0x80, R3)] { if b & v != 0 { b1 |= bit; } }
    let mut b2 = 0u8;
    for (bit, v) in [(0x01, PS), (0x02, TOUCHPAD), (0x04, MUTE), (0x10, FN_LEFT), (0x20, FN_RIGHT), (0x40, BACK_LEFT), (0x80, BACK_RIGHT)] { if b & v != 0 { b2 |= bit; } }
    r[8] = b0; r[9] = b1; r[10] = b2;
    for k in 0..3 {
        r[16 + 2 * k..18 + 2 * k].copy_from_slice(&p.gyro[k].to_le_bytes());
        r[22 + 2 * k..24 + 2 * k].copy_from_slice(&p.accel[k].to_le_bytes());
    }
    r[28..32].copy_from_slice(&p.sensor_ts.to_le_bytes());
    r
}

impl Pad {
    pub fn neutral() -> Self { Pad { transport: Transport::Usb, lx: 128, ly: 128, rx: 128, ry: 128, l2: 0, r2: 0, buttons: 0, counter: 0, sensor_ts: 0, gyro: [0; 3], accel: [0; 3] } }
    /// True when the report carries any non-zero IMU value (a real pad at rest still reports gravity on accel).
    pub fn has_imu(&self) -> bool { self.gyro != [0; 3] || self.accel != [0; 3] }
}

// ------------------------------------------------------------------------------------------- devices

/// Something that yields raw HID input reports.
///
/// Implementations read input reports only. They must not write an output report or a feature report.
pub trait ReportSource: Send {
    /// Read one input report; Ok(None) on timeout; Err when the device is gone.
    fn read(&mut self, buf: &mut [u8], timeout_ms: i32) -> Result<Option<usize>, String>;
    fn name(&self) -> String;
    /// Identity recorded in the sealed pad log on connect. USB serial when the device has one,
    /// otherwise a device instance id or a hidraw id. Empty when none of those is available.
    fn pad_id(&self) -> String;
}

/// `serial:<usb serial>`, else `instance:<stable id>`, else `hidraw:<node>`.
/// Enumeration metadata only: this does not touch the HID report channel.
pub fn pad_identity(serial: Option<&str>, instance: Option<&str>, hidraw: Option<&str>) -> String {
    fn clean(v: Option<&str>) -> Option<&str> { v.map(str::trim).filter(|s| !s.is_empty()) }
    if let Some(s) = clean(serial) { return format!("serial:{s}"); }
    if let Some(s) = clean(instance) { return format!("instance:{s}"); }
    if let Some(s) = clean(hidraw) { return format!("hidraw:{s}"); }
    String::new()
}

/// Linux identity from a hidraw `uevent` plus an optional sysfs `serial` file.
/// `HID_UNIQ` and the USB `serial` file win over `HID_PHYS`, which wins over the hidraw node name.
pub fn linux_pad_id(uevent: &str, serial_file: Option<&str>, hidraw_name: &str) -> String {
    let line = |key: &str| uevent.lines().find_map(|l| l.strip_prefix(key)).map(str::trim).filter(|s| !s.is_empty());
    let serial = line("HID_UNIQ=").or(serial_file.map(str::trim).filter(|s| !s.is_empty()));
    pad_identity(serial, line("HID_PHYS="), Some(hidraw_name))
}

/// Open the first USB DualSense / DualSense Edge. Err(reason) when none is present.
pub fn open_dualsense() -> Result<Box<dyn ReportSource>, String> { os::open() }

pub fn model_name(pid: u16) -> &'static str { if pid == PID_DUALSENSE_EDGE { "DualSense Edge" } else { "DualSense" } }

#[cfg(windows)]
mod os {
    use super::*;
    pub struct Dev { d: hidapi::HidDevice, name: String, pad_id: String }
    impl ReportSource for Dev {
        fn read(&mut self, buf: &mut [u8], t: i32) -> Result<Option<usize>, String> {
            // Input reports only. Do not call write() or send_feature_report().
            match self.d.read_timeout(buf, t) { Ok(0) => Ok(None), Ok(n) => Ok(Some(n)), Err(e) => Err(e.to_string()) }
        }
        fn name(&self) -> String { self.name.clone() }
        fn pad_id(&self) -> String { self.pad_id.clone() }
    }
    fn device_pad_id(info: &hidapi::DeviceInfo) -> String {
        let path = info.path().to_string_lossy();
        let instance = if path.is_empty() { None } else { Some(path.as_ref()) };
        // serial_number() is the string from device enumeration, not a feature report.
        pad_identity(info.serial_number(), instance, None)
    }
    pub fn open() -> Result<Box<dyn ReportSource>, String> {
        let api = hidapi::HidApi::new().map_err(|e| format!("HID init failed: {e}"))?;
        let mut cands: Vec<&hidapi::DeviceInfo> = api.device_list()
            .filter(|d| d.vendor_id() == SONY_VID && [PID_DUALSENSE, PID_DUALSENSE_EDGE].contains(&d.product_id())).collect();
        if cands.is_empty() { return Err("no DualSense found on USB".into()); }
        // Prefer the gamepad collection (usage page 1, usage 5) and the Edge.
        cands.sort_by_key(|d| (!(d.usage_page() == 1 && d.usage() == 5), d.product_id() != PID_DUALSENSE_EDGE));
        let mut last = String::new();
        for c in cands {
            let pad_id = device_pad_id(c);
            let name = format!("{} (USB)", model_name(c.product_id()));
            match c.open_device(&api) {
                Ok(d) => return Ok(Box::new(Dev { d, name, pad_id })),
                Err(e) => last = e.to_string(),
            }
        }
        Err(format!("DualSense found but could not be opened: {last}"))
    }
}

#[cfg(target_os = "linux")]
mod os {
    use super::*;
    use std::io::Read;
    pub struct Dev { f: std::fs::File, name: String, pad_id: String }
    impl ReportSource for Dev {
        fn read(&mut self, buf: &mut [u8], t: i32) -> Result<Option<usize>, String> {
            // Read-only file. No HIDIOC output or feature ioctl.
            use std::os::unix::io::AsRawFd;
            let mut p = libc::pollfd { fd: self.f.as_raw_fd(), events: libc::POLLIN, revents: 0 };
            let r = unsafe { libc::poll(&mut p, 1, t) };
            if r == 0 { return Ok(None); }
            if r < 0 || p.revents & (libc::POLLERR | libc::POLLHUP) != 0 { return Err("device gone".into()); }
            self.f.read(buf).map(Some).map_err(|e| e.to_string())
        }
        fn name(&self) -> String { self.name.clone() }
        fn pad_id(&self) -> String { self.pad_id.clone() }
    }
    /// USB serial from sysfs, walking up from the hidraw device node. Read-only.
    fn sysfs_serial(hidraw: &std::path::Path) -> Option<String> {
        let mut dir = hidraw.join("device");
        for _ in 0..8 {
            if let Ok(s) = std::fs::read_to_string(dir.join("serial")) {
                let s = s.trim();
                if !s.is_empty() { return Some(s.to_string()); }
            }
            dir = dir.parent()?.to_path_buf();
        }
        None
    }
    pub fn open() -> Result<Box<dyn ReportSource>, String> {
        let rd = std::fs::read_dir("/sys/class/hidraw").map_err(|_| "no hidraw devices".to_string())?;
        for e in rd.flatten() {
            let uevent = std::fs::read_to_string(e.path().join("device/uevent")).unwrap_or_default();
            // HID_ID=0003:0000054C:00000CE6  (bus 0003 = USB)
            let Some(id) = uevent.lines().find_map(|l| l.strip_prefix("HID_ID=")) else { continue };
            let p: Vec<&str> = id.split(':').collect();
            if p.len() != 3 || p[0] != "0003" { continue; }
            let (vid, pid) = (u32::from_str_radix(p[1], 16).unwrap_or(0), u32::from_str_radix(p[2], 16).unwrap_or(0));
            if vid != SONY_VID as u32 || !(pid == PID_DUALSENSE as u32 || pid == PID_DUALSENSE_EDGE as u32) { continue; }
            let node = e.file_name().to_string_lossy().into_owned();
            let serial = sysfs_serial(&e.path());
            let pad_id = linux_pad_id(&uevent, serial.as_deref(), &node);
            let dev = format!("/dev/{node}");
            // File::open is read-only. The pad log records pad_id; nothing is written to the controller.
            return std::fs::File::open(&dev).map(|f| Box::new(Dev {
                f, name: format!("{} (USB)", model_name(pid as u16)), pad_id,
            }) as Box<dyn ReportSource>).map_err(|e| format!("{dev}: {e}"));
        }
        Err("no DualSense found on USB".into())
    }
}

#[cfg(not(any(windows, target_os = "linux")))]
mod os {
    use super::*;
    pub fn open() -> Result<Box<dyn ReportSource>, String> { Err("HID reading is only implemented on Windows and Linux".into()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn usb_report_round_trip_and_offsets() {
        // Hand-built USB report: CROSS + R1 + d-pad UP-RIGHT, R2 half-pressed, left stick right.
        let mut r = [0u8; 64];
        r[0] = 0x01; r[1] = 200; r[2] = 128; r[3] = 127; r[4] = 129; r[5] = 0; r[6] = 130; r[7] = 9;
        r[8] = 0x20 | 0x01; r[9] = 0x02; r[10] = 0x00;
        r[28..32].copy_from_slice(&123456u32.to_le_bytes());
        let p = parse(&r).unwrap();
        assert_eq!(p.transport, Transport::Usb);
        assert_eq!((p.lx, p.ly, p.rx, p.ry, p.l2, p.r2, p.counter, p.sensor_ts), (200, 128, 127, 129, 0, 130, 9, 123456));
        assert_eq!(p.buttons, CROSS | R1 | UP | RIGHT);
        assert_eq!(names(p.buttons), vec!["CROSS", "R1", "UP", "RIGHT"]);
        assert_eq!(parse(&pack_usb(&p)).unwrap(), p);
    }
    #[test]
    fn imu_offsets_usb_and_bt() {
        let mut r = [0u8; 64]; r[0] = 0x01; r[8] = 0x08;
        r[16..18].copy_from_slice(&(-3i16).to_le_bytes()); r[20..22].copy_from_slice(&1234i16.to_le_bytes());
        r[22..24].copy_from_slice(&(-8192i16).to_le_bytes()); r[26..28].copy_from_slice(&300i16.to_le_bytes());
        let p = parse(&r).unwrap();
        assert_eq!((p.gyro, p.accel), ([-3, 0, 1234], [-8192, 0, 300]));
        assert!(p.has_imu() && !Pad::neutral().has_imu());
        assert_eq!(parse(&pack_usb(&p)).unwrap(), p);
        let mut b = [0u8; 78]; b[0] = 0x31; b[9] = 0x08; b[23..25].copy_from_slice(&777i16.to_le_bytes());
        assert_eq!(parse(&b).unwrap().accel[0], 777);
    }
    #[test]
    fn neutral_hat_and_edge_buttons() {
        let mut r = [0u8; 64]; r[0] = 0x01; r[8] = 0x08; r[10] = 0x01 | 0x40 | 0x80 | 0x10;
        let p = parse(&r).unwrap();
        assert_eq!(p.buttons, PS | BACK_LEFT | BACK_RIGHT | FN_LEFT);
    }
    #[test]
    fn bluetooth_report_shifted_by_one() {
        let mut r = [0u8; 78]; r[0] = 0x31; r[2] = 10; r[6] = 77; r[9] = 0x80 | 0x08; r[10] = 0x20;
        let p = parse(&r).unwrap();
        assert_eq!(p.transport, Transport::Bluetooth);
        assert_eq!((p.lx, p.l2), (10, 77));
        assert_eq!(p.buttons, TRIANGLE | OPTIONS);
    }
    #[test]
    fn rejects_other_reports() {
        assert!(parse(&[0x05; 64]).is_none());
        assert!(parse(&[0x01; 10]).is_none());
    }
    #[test]
    fn pad_identity_prefers_usb_serial_then_instance_then_hidraw() {
        assert_eq!(pad_identity(Some("ABC123"), Some("usb-1"), Some("hidraw0")), "serial:ABC123");
        assert_eq!(pad_identity(Some("  "), Some("usb-1"), Some("hidraw0")), "instance:usb-1");
        assert_eq!(pad_identity(None, None, Some("hidraw3")), "hidraw:hidraw3");
        assert!(pad_identity(None, Some(""), None).is_empty());
        let uevent = "HID_ID=0003:0000054C:00000DF2\nHID_UNIQ=AAAA1111\nHID_PHYS=usb-0000:00:14.0-1/input0\n";
        assert_eq!(linux_pad_id(uevent, None, "hidraw0"), "serial:AAAA1111");
        let bare = "HID_ID=0003:0000054C:00000CE6\nHID_UNIQ=\nHID_PHYS=usb-0000:00:14.0-2/input0\n";
        assert_eq!(linux_pad_id(bare, Some("\n"), "hidraw1"), "instance:usb-0000:00:14.0-2/input0");
        assert_eq!(linux_pad_id("HID_ID=0003:0000054C:00000CE6\n", Some("BBBB2222"), "hidraw2"), "serial:BBBB2222");
        assert_eq!(linux_pad_id("", None, "hidraw4"), "hidraw:hidraw4");
    }
}
