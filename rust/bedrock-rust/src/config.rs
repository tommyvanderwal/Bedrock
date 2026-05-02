//! Daemon configuration file at `/etc/bedrock/daemon.toml`.
//!
//! Written by `bedrock init` / `bedrock join` and consumed by the
//! systemd unit. Lives next to `cluster.json` and `state.json`. The
//! file is the single source of truth for daemon startup; CLI flags
//! exist for ad-hoc dev runs and override the file.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DaemonConfig {
    pub log_dir: Option<PathBuf>,
    pub ipc_sock: Option<PathBuf>,
    /// This node's sender_id (0..0xFE).
    pub sender_id: Option<u8>,
    /// The peer's sender_id, if known. Legacy single-peer field. Use
    /// `peer_sender_ids` for N-peer (≥3-node) clusters; this stays
    /// for backward compat and is folded into `peer_sender_ids` on
    /// load when only the single field is set.
    pub peer_sender_id: Option<u8>,
    /// Sender_ids of every other node in the cluster. Length =
    /// cluster_size - 1. Drives the weighted-vote election (see
    /// witness::compute_election). Empty for standalone.
    #[serde(default)]
    pub peer_sender_ids: Vec<u8>,
    /// Listen addresses for inbound peer connections (multi-link
    /// supported per design §7).
    #[serde(default)]
    pub peer_listen: Vec<String>,
    /// Peer addresses to dial. Multi-link supported.
    #[serde(default)]
    pub peer: Vec<String>,
    /// Cluster interfaces to bring down on self-fence (multi-link
    /// teardown per design §6).
    #[serde(default)]
    pub fence_interfaces: Vec<String>,
    /// Witness peers. v0.1 default is exactly one witness — single
    /// witness is the canonical configuration and works perfectly.
    /// Multi-witness (3-of-5) is supported by the data model for
    /// later operational use; same struct, just more entries.
    #[serde(default)]
    pub witness: Vec<WitnessConfig>,
    pub lease_ttl_ms: Option<u64>,
    pub heartbeat_ms: Option<u64>,
    /// Hex-encoded 32-byte cluster key used for AEAD with witnesses.
    pub cluster_key_hex: Option<String>,
    /// Path to the cluster_key file (binary 32 bytes). Falls back to
    /// /etc/bedrock/cluster.key when not set.
    pub cluster_key_file: Option<PathBuf>,
    /// Starting role hint. Election overrides once witness data is in.
    pub role: Option<String>,
    /// True when our peer is intentionally offline (operator put it
    /// in maintenance mode). Treat peer silence as expected; witness
    /// arbitration is NOT required to keep running solo.
    #[serde(default)]
    pub peer_in_maintenance: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WitnessConfig {
    /// Friendly id for logging. Doesn't have to be unique across
    /// witnesses, but should be.
    #[serde(default)]
    pub id: String,
    pub host: String,
    #[serde(default = "default_witness_port")]
    pub port: u16,
    /// Hex-encoded 32-byte X25519 pubkey for pinning. The witness
    /// publishes this via DISCOVER → INIT; we verify before BOOTSTRAP.
    pub pubkey_hex: String,
}

fn default_witness_port() -> u16 {
    12321
}

impl DaemonConfig {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let s = std::fs::read_to_string(path)?;
        let cfg: Self = toml::from_str(&s)?;
        Ok(cfg)
    }
}
