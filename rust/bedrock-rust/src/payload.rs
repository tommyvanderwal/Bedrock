//! Log payload kinds. The byte tag in each `LogEntry` says which kind, but
//! the actual decode/interpretation is Python's job — Rust treats payloads
//! as opaque byte strings (per design §1: "Rust does not interpret payloads").

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    /// Bootstrap entry written by `log init`. Payload is
    /// `b"Hello World! <cluster-uuid>"`. Always at index 1.
    Bootstrap = 0x01,
    /// Free-form payload from CLI / Python. Used in v0.1 for everything
    /// non-bootstrap until the typed-entry catalogue is fleshed out.
    Opaque = 0x02,
    // 0x10..0x1F reserved for cluster-config entries
    // 0x20..0x2F reserved for tier-state entries
    // 0x30..0x3F reserved for VM/task entries
    // 0xF0..0xFF reserved for log-internal markers (snapshot pointer etc.)
}

impl Kind {
    pub fn from_u8(b: u8) -> Option<Self> {
        match b {
            0x01 => Some(Self::Bootstrap),
            0x02 => Some(Self::Opaque),
            _ => None,
        }
    }
}
