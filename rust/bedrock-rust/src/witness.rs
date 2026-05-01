//! Echo witness client.
//!
//! Implements the DISCOVER → INIT → BOOTSTRAP → BOOTSTRAP_ACK → HEARTBEAT
//! sequence per PROTOCOL.md, carrying our `(epoch, last_committed_index,
//! last_committed_hash)` as the HEARTBEAT `own_payload`.
//!
//! Packing of `own_payload` (64 B = 2 blocks):
//!
//! ```
//!   offset  size  field
//!   ─────────────────────────────────────────
//!   0       8     epoch                 u64 BE
//!   8       8     last_committed_index  u64 BE
//!   16      32    last_committed_hash   SHA-256
//!   48      16    reserved (zeroed)
//!   ─────────────────────────────────────────
//! ```
//!
//! 16 reserved bytes give us room for a node_id, a generation counter,
//! or a flags field later without breaking compatibility — STATUS_DETAIL
//! returns the bytes verbatim, so any peer reading them just sees zeros
//! today.

use anyhow::{bail, Context, Result};
use bedrock_echo_proto::{
    constants::*,
    crypto,
    msg,
};
use crate::log_store::Log;
use std::net::{ToSocketAddrs, UdpSocket};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const RECV_TIMEOUT: Duration = Duration::from_secs(2);
const PAYLOAD_BLOCKS: usize = 2; // 64 bytes

fn now_ms() -> i64 {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    (nanos / 1_000_000) as i64
}

fn pack_own_payload(epoch: u64, last_index: u64, last_hash: [u8; 32]) -> [u8; 64] {
    let mut buf = [0u8; 64];
    buf[0..8].copy_from_slice(&epoch.to_be_bytes());
    buf[8..16].copy_from_slice(&last_index.to_be_bytes());
    buf[16..48].copy_from_slice(&last_hash);
    // 48..64 reserved, already zero.
    buf
}

/// Send one full DISCOVER → BOOTSTRAP → HEARTBEAT round-trip and print
/// the witness's reply.
///
/// This is the v0.1 smoke test: prove the Rust daemon can talk to a
/// witness on the LAN with our protocol.
pub fn heartbeat_once(
    host: &str,
    port: u16,
    cluster_key: &[u8; 32],
    witness_pubkey: &[u8; 32],
    sender_id: u8,
    query_target: u8,
    last_index: u64,
    last_hash: [u8; 32],
) -> Result<()> {
    let addr = (host, port)
        .to_socket_addrs()
        .with_context(|| format!("resolving {host}:{port}"))?
        .next()
        .context("no socket address resolved")?;

    let sock = UdpSocket::bind("0.0.0.0:0").context("bind UDP")?;
    sock.set_read_timeout(Some(RECV_TIMEOUT))?;
    sock.connect(addr).with_context(|| format!("connect {addr}"))?;
    println!("witness: {} (UDP)", addr);

    // 1. DISCOVER → INIT (gets witness pubkey + a fresh anti-spoof cookie)
    let mut buf = [0u8; MTU_CAP];
    let n = msg::encode_discover(&mut buf, sender_id, now_ms(), 0)
        .map_err(|e| anyhow::anyhow!("encode_discover: {:?}", e))?;
    sock.send(&buf[..n]).context("send DISCOVER")?;

    let mut reply = [0u8; MTU_CAP];
    let n = sock.recv(&mut reply).context("recv INIT")?;
    let init = msg::decode_init(&reply[..n])
        .map_err(|e| anyhow::anyhow!("decode_init: {:?}", e))?;
    println!(
        "INIT: witness_pubkey={} cookie={} caps={:#06x}",
        hex::encode(&init.witness_pubkey[..]),
        hex::encode(&init.cookie[..]),
        init.capability_flags,
    );
    if init.witness_pubkey != witness_pubkey {
        bail!(
            "witness_pubkey mismatch — expected {} got {} (possible MITM, refusing to BOOTSTRAP)",
            hex::encode(witness_pubkey),
            hex::encode(&init.witness_pubkey[..]),
        );
    }
    let cookie = *init.cookie;

    // 2. BOOTSTRAP → BOOTSTRAP_ACK (delivers our cluster_key under fresh ECDH key)
    let (eph_priv, _eph_pub) = generate_eph_keypair();
    let mut buf = [0u8; MTU_CAP];
    let n = msg::encode_bootstrap(
        &mut buf,
        sender_id,
        now_ms(),
        cluster_key,
        witness_pubkey,
        &eph_priv,
        &cookie,
    )
    .map_err(|e| anyhow::anyhow!("encode_bootstrap: {:?}", e))?;
    sock.send(&buf[..n]).context("send BOOTSTRAP")?;

    let mut reply = [0u8; MTU_CAP];
    let n = sock.recv(&mut reply).context("recv BOOTSTRAP_ACK")?;
    let ack = msg::decode_bootstrap_ack(&mut reply[..n], cluster_key)
        .map_err(|e| anyhow::anyhow!("decode_bootstrap_ack: {:?}", e))?;
    println!(
        "BOOTSTRAP_ACK: status={:#04x} ({}) witness_uptime={}s",
        ack.status,
        bootstrap_status_str(ack.status),
        ack.witness_uptime_seconds,
    );

    // 3. HEARTBEAT carrying our log tail → STATUS_LIST or STATUS_DETAIL
    let payload = pack_own_payload(/*epoch=*/ 1, last_index, last_hash);
    let mut buf = [0u8; MTU_CAP];
    let n = msg::encode_heartbeat(
        &mut buf,
        sender_id,
        now_ms(),
        query_target,
        &payload,
        cluster_key,
    )
    .map_err(|e| anyhow::anyhow!("encode_heartbeat: {:?}", e))?;
    sock.send(&buf[..n]).context("send HEARTBEAT")?;
    println!(
        "HEARTBEAT sent: sender_id={:#04x} target={:#04x} payload_blocks={}",
        sender_id, query_target, PAYLOAD_BLOCKS
    );

    let mut reply = [0u8; MTU_CAP];
    let n = sock.recv(&mut reply).context("recv STATUS_*")?;
    let raw = &reply[..n];
    if raw.len() < HEADER_LEN {
        bail!("reply too short ({} bytes)", raw.len());
    }
    match raw[4] {
        MSG_STATUS_LIST => print_status_list(raw, cluster_key)?,
        MSG_STATUS_DETAIL => print_status_detail(raw, cluster_key)?,
        MSG_INIT => {
            // Witness lost our cluster — would need re-bootstrap. Phase 4.
            let init = msg::decode_init(raw)
                .map_err(|e| anyhow::anyhow!("decode_init (re-init): {:?}", e))?;
            println!(
                "WITNESS REPLIED INIT instead of STATUS — needs re-bootstrap. \
                 cookie={} (re-bootstrap not implemented in v0.1 smoke test)",
                hex::encode(&init.cookie[..])
            );
        }
        other => bail!("unexpected reply msg_type {:#04x}", other),
    }
    Ok(())
}

fn bootstrap_status_str(s: u8) -> &'static str {
    match s {
        0x00 => "OK_NEW",
        0x01 => "OK_IDEMPOTENT",
        0x02 => "OK_REPLACED",
        _ => "?",
    }
}

fn generate_eph_keypair() -> ([u8; 32], [u8; 32]) {
    use rand_core::{OsRng, RngCore};
    let mut secret = [0u8; 32];
    OsRng.fill_bytes(&mut secret);
    // X25519 clamping per RFC 7748.
    secret[0] &= 0xF8;
    secret[31] &= 0x7F;
    secret[31] |= 0x40;
    let public = crypto::x25519_pub_from_priv(&secret);
    (secret, public)
}

fn print_status_list(raw: &[u8], cluster_key: &[u8; 32]) -> Result<()> {
    let mut buf = raw.to_vec();
    let r = msg::decode_status_list_into(&mut buf, cluster_key)
        .map_err(|e| anyhow::anyhow!("decode_status_list: {:?}", e))?;
    println!(
        "STATUS_LIST: witness_uptime={}s entries={}",
        r.witness_uptime_seconds, r.num_entries
    );
    for i in 0..(r.num_entries as usize) {
        if let Some(e) = r.entry(i) {
            println!(
                "  [{}] sender_id={:#04x} last_seen_ms={}",
                i, e.peer_sender_id, e.last_seen_ms
            );
        }
    }
    Ok(())
}

// ── Lease loop + self-fence (Phase 4) ────────────────────────────────────

/// Default location for the fence marker.
pub const FENCE_MARKER_PATH: &str = "/run/bedrock-rust.fence";

/// One witness peer. Multiple of these can be configured (Phase 9):
/// single-witness (Vec of length 1) is the canonical setup and works
/// perfectly; multi-witness gives operational hygiene at internet scale
/// without changing protocol guarantees.
#[derive(Clone, Debug)]
pub struct WitnessSpec {
    pub id: String,
    pub host: String,
    pub port: u16,
    /// Per-cluster shared secret used as the AEAD key with this
    /// witness. v0.1 reuses the same cluster key across all witnesses;
    /// design §10 calls for per-witness keys (each encrypted under
    /// the cluster's shared key) and that lands in v1.1.
    pub cluster_key: [u8; 32],
    /// X25519 pubkey, pinned via DISCOVER → INIT verification.
    pub witness_pubkey: [u8; 32],
}

#[derive(Clone)]
pub struct LeaseConfig {
    pub witnesses: Vec<WitnessSpec>,
    pub sender_id: u8,
    /// If set: query each witness's STATUS_DETAIL on this sender_id
    /// every heartbeat to inform leader election. When None, the
    /// daemon uses the role passed on the CLI as-is.
    pub peer_sender_id: Option<u8>,
    pub ttl_ms: u64,
    pub heartbeat_ms: u64,
    pub fence_interfaces: Vec<String>,
}

/// Election rules at a glance (design §6 / §9):
///
/// ```
///   fence marker present? → ELECTED_FOLLOWER (refuse leader regardless)
///   no peer entry at witness? → ELECTED_LEADER  (we're the only one talking)
///   our last_index > peer's? → ELECTED_LEADER
///   our last_index < peer's? → ELECTED_FOLLOWER
///   equal? → lower sender_id wins → ELECTED_LEADER (us) / ELECTED_FOLLOWER (peer)
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Election {
    Leader,
    Follower,
    /// Peer can be reached / hasn't been heard from yet — keep current role.
    Unknown,
}

/// Pure decision function — easy to unit test.
pub fn elect(
    fence_marker: bool,
    our_last_index: u64,
    our_sender_id: u8,
    peer_last_index: Option<u64>,
    peer_sender_id: u8,
) -> Election {
    if fence_marker {
        return Election::Follower;
    }
    match peer_last_index {
        None => Election::Leader,
        Some(theirs) if our_last_index > theirs => Election::Leader,
        Some(theirs) if our_last_index < theirs => Election::Follower,
        Some(_) => {
            // Tie — deterministic by sender_id.
            if our_sender_id <= peer_sender_id {
                Election::Leader
            } else {
                Election::Follower
            }
        }
    }
}

/// Spawn the lease loop. The thread heartbeats to the witness every
/// `heartbeat_ms`; when it can't successfully heartbeat for `ttl_ms`
/// total, it kicks self_fence().
pub fn start_lease_loop(cfg: LeaseConfig, log: Arc<Mutex<Log>>) -> JoinHandle<()> {
    thread::spawn(move || {
        if let Err(e) = run_lease_loop(cfg, log) {
            log::error!("lease loop terminated: {}", e);
        }
    })
}

fn run_lease_loop(cfg: LeaseConfig, log: Arc<Mutex<Log>>) -> Result<()> {
    log::info!(
        "lease: {} witness(es) ttl={}ms heartbeat={}ms peer_sender_id={:?}",
        cfg.witnesses.len(), cfg.ttl_ms, cfg.heartbeat_ms, cfg.peer_sender_id,
    );
    for w in &cfg.witnesses {
        log::info!("  witness[{}]: {}:{}", w.id, w.host, w.port);
    }

    // One session slot per witness. None means "needs re-establishing"
    // — happens on first iteration and after any I/O error.
    let mut sessions: Vec<Option<WitnessSession>> = cfg.witnesses.iter().map(|_| None).collect();
    let mut last_ok = Instant::now();
    let mut last_election = Election::Unknown;

    loop {
        let (latest_index, latest_hash) = {
            let lg = log.lock().unwrap();
            lg.latest()
        };
        let payload = pack_own_payload(/*epoch=*/ 1, latest_index, latest_hash);
        let target = cfg.peer_sender_id.unwrap_or(QUERY_LIST_SENTINEL);

        // Heartbeat + (optionally) detail-query each witness this tick.
        // The freshest peer-state across witnesses wins for election —
        // a peer is "seen at last_index L" if ANY witness reports it.
        let mut any_ok = false;
        let mut max_peer_idx: Option<u64> = None;
        for (i, w) in cfg.witnesses.iter().enumerate() {
            let result = (|| -> Result<Option<u64>> {
                if sessions[i].is_none() {
                    sessions[i] = Some(WitnessSession::establish(w, cfg.sender_id)?);
                }
                let sess = sessions[i].as_mut().unwrap();
                let reply = sess.heartbeat_full(&payload, target)?;
                if cfg.peer_sender_id.is_some() {
                    Ok(parse_peer_last_index(&reply, &w.cluster_key))
                } else {
                    Ok(None)
                }
            })();
            match result {
                Ok(peer_last) => {
                    any_ok = true;
                    if let Some(p) = peer_last {
                        max_peer_idx = Some(max_peer_idx.map_or(p, |m| m.max(p)));
                    }
                }
                Err(e) => {
                    sessions[i] = None;
                    log::warn!("lease: witness[{}] heartbeat failed: {}", w.id, e);
                }
            }
        }

        if any_ok {
            last_ok = Instant::now();
            if let Some(peer_id) = cfg.peer_sender_id {
                let fence = fence_marker_present();
                let next = elect(fence, latest_index, cfg.sender_id, max_peer_idx, peer_id);
                if next != last_election {
                    log::info!(
                        "election: {:?} → {:?} (us=idx{}, peer_max={:?})",
                        last_election, next, latest_index, max_peer_idx
                    );
                    last_election = next;
                }
            }
        } else {
            let elapsed = last_ok.elapsed();
            log::warn!(
                "lease: no witness reachable (ttl elapsed {}ms / {}ms)",
                elapsed.as_millis(), cfg.ttl_ms
            );
            if elapsed >= Duration::from_millis(cfg.ttl_ms) {
                log::error!("lease: TTL exhausted on every witness; entering self-fence");
                self_fence(&cfg.fence_interfaces)?;
                return Ok(());
            }
        }

        thread::sleep(Duration::from_millis(cfg.heartbeat_ms));
    }
}

/// One witness UDP session: socket + cookie. Re-establishes from scratch
/// after any I/O error (next heartbeat re-runs DISCOVER → BOOTSTRAP).
struct WitnessSession {
    sock: UdpSocket,
    sender_id: u8,
    cluster_key: [u8; 32],
    witness_pubkey: [u8; 32],
}

impl WitnessSession {
    fn establish(w: &WitnessSpec, sender_id: u8) -> Result<Self> {
        let addr = (w.host.as_str(), w.port)
            .to_socket_addrs()
            .with_context(|| format!("resolving {}:{}", w.host, w.port))?
            .next()
            .context("no socket address")?;
        let sock = UdpSocket::bind("0.0.0.0:0").context("bind")?;
        sock.set_read_timeout(Some(RECV_TIMEOUT))?;
        sock.connect(addr)?;

        // DISCOVER → INIT
        let mut buf = [0u8; MTU_CAP];
        let n = msg::encode_discover(&mut buf, sender_id, now_ms(), 0)
            .map_err(|e| anyhow::anyhow!("encode_discover: {:?}", e))?;
        sock.send(&buf[..n])?;
        let mut reply = [0u8; MTU_CAP];
        let n = sock.recv(&mut reply)?;
        let init = msg::decode_init(&reply[..n])
            .map_err(|e| anyhow::anyhow!("decode_init: {:?}", e))?;
        if init.witness_pubkey != &w.witness_pubkey {
            bail!("witness[{}]: pubkey mismatch — possible MITM", w.id);
        }
        let cookie = *init.cookie;

        // BOOTSTRAP → BOOTSTRAP_ACK
        let (eph_priv, _) = generate_eph_keypair();
        let mut buf = [0u8; MTU_CAP];
        let n = msg::encode_bootstrap(
            &mut buf, sender_id, now_ms(),
            &w.cluster_key, &w.witness_pubkey, &eph_priv, &cookie,
        )
        .map_err(|e| anyhow::anyhow!("encode_bootstrap: {:?}", e))?;
        sock.send(&buf[..n])?;
        let mut reply = [0u8; MTU_CAP];
        let n = sock.recv(&mut reply)?;
        let _ack = msg::decode_bootstrap_ack(&mut reply[..n], &w.cluster_key)
            .map_err(|e| anyhow::anyhow!("decode_bootstrap_ack: {:?}", e))?;

        Ok(Self {
            sock,
            sender_id,
            cluster_key: w.cluster_key,
            witness_pubkey: w.witness_pubkey,
        })
    }

    fn heartbeat(&mut self, payload: &[u8], query_target: u8) -> Result<()> {
        self.heartbeat_full(payload, query_target).map(|_| ())
    }

    /// Send a HEARTBEAT, return the raw reply bytes for the caller to
    /// decode (used by leader election to read peer's STATUS_DETAIL).
    fn heartbeat_full(&mut self, payload: &[u8], query_target: u8) -> Result<Vec<u8>> {
        let mut buf = [0u8; MTU_CAP];
        let n = msg::encode_heartbeat(
            &mut buf, self.sender_id, now_ms(),
            query_target, payload, &self.cluster_key,
        )
        .map_err(|e| anyhow::anyhow!("encode_heartbeat: {:?}", e))?;
        self.sock.send(&buf[..n])?;
        let mut reply = [0u8; MTU_CAP];
        let n = self.sock.recv(&mut reply)?;
        let raw = reply[..n].to_vec();
        if raw.len() < HEADER_LEN {
            bail!("heartbeat reply too short");
        }
        // INIT reply means the witness lost our cluster (RAM-only state
        // after a witness restart) — re-establish on next iteration.
        if raw[4] == MSG_INIT {
            bail!("witness sent INIT (lost cluster); re-bootstrap needed");
        }
        Ok(raw)
    }
}

/// Decode the witness's STATUS_DETAIL reply for our peer and pull out
/// the last_committed_index field from `peer_payload`. Returns None if:
///  - reply is STATUS_LIST (we asked LIST), or
///  - status_and_blocks says peer not found, or
///  - peer_payload is shorter than 16 bytes.
fn parse_peer_last_index(raw: &[u8], cluster_key: &[u8; 32]) -> Option<u64> {
    if raw.len() < HEADER_LEN {
        return None;
    }
    if raw[4] != MSG_STATUS_DETAIL {
        return None;
    }
    let mut buf = raw.to_vec();
    let r = msg::decode_status_detail_into(&mut buf, cluster_key).ok()?;
    if !r.found || r.peer_payload.len() < 16 {
        return None;
    }
    let last_index = u64::from_be_bytes(r.peer_payload[8..16].try_into().ok()?);
    Some(last_index)
}

/// Self-fence: bring all configured cluster interfaces down, write the
/// fence marker, log the reason. Caller decides whether to proceed to
/// reboot. v0.1 dev-mode just exits the process — Phase 4.5 wires the
/// real reboot.
pub fn self_fence(interfaces: &[String]) -> Result<()> {
    log::error!("self-fence: bringing down {} cluster interface(s)", interfaces.len());
    for iface in interfaces {
        match Command::new("ip").args(["link", "set", iface, "down"]).status() {
            Ok(s) if s.success() => log::error!("self-fence: {} DOWN", iface),
            Ok(s) => log::warn!("self-fence: ip link {} down exited {}", iface, s),
            Err(e) => log::warn!("self-fence: failed to spawn `ip link` for {}: {}", iface, e),
        }
    }
    // Persist the fence marker so a post-reboot daemon refuses to
    // claim leadership without operator inspection.
    let marker = PathBuf::from(FENCE_MARKER_PATH);
    if let Err(e) = std::fs::write(&marker, format!("{}\n", chrono::Utc::now().to_rfc3339())) {
        log::warn!("self-fence: marker write failed at {}: {}",
                   marker.display(), e);
    } else {
        log::error!("self-fence: marker written at {}", marker.display());
    }
    log::error!("self-fence: dev mode — exiting the daemon process \
                 (production: 300s python cleanup window then `systemctl reboot`)");
    // In production we'd: signal Python via IPC, wait up to 300s for
    // FenceComplete or timeout, `systemctl reboot`. v0.1 just exits.
    std::process::exit(2);
}

/// Returns true if the fence marker is present from a prior fence event.
/// Boot recovery uses this to refuse leader claims until the operator
/// (or Python) clears the marker.
pub fn fence_marker_present() -> bool {
    std::path::Path::new(FENCE_MARKER_PATH).exists()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fence_marker_forces_follower() {
        // Even if our log is ahead and peer is gone, fence marker
        // wins and we refuse to claim leader.
        assert_eq!(elect(true, 100, 1, None, 2), Election::Follower);
    }

    #[test]
    fn peer_absent_means_we_lead() {
        assert_eq!(elect(false, 5, 1, None, 2), Election::Leader);
    }

    #[test]
    fn higher_index_wins() {
        assert_eq!(elect(false, 10, 1, Some(5), 2), Election::Leader);
        assert_eq!(elect(false, 5, 1, Some(10), 2), Election::Follower);
    }

    #[test]
    fn tie_resolved_by_lower_sender_id() {
        // sender_id 1 wins over 2.
        assert_eq!(elect(false, 7, 1, Some(7), 2), Election::Leader);
        // sender_id 2 loses to 1.
        assert_eq!(elect(false, 7, 2, Some(7), 1), Election::Follower);
    }
}

fn print_status_detail(raw: &[u8], cluster_key: &[u8; 32]) -> Result<()> {
    let mut buf = raw.to_vec();
    let r = msg::decode_status_detail_into(&mut buf, cluster_key)
        .map_err(|e| anyhow::anyhow!("decode_status_detail: {:?}", e))?;
    println!(
        "STATUS_DETAIL: target={:#04x} witness_uptime={}s",
        r.target_sender_id, r.witness_uptime_seconds,
    );
    if r.found {
        let ip = r.peer_ipv4;
        let blocks = r.peer_payload.len() / PAYLOAD_BLOCK_SIZE;
        println!(
            "  found: peer_ipv4={}.{}.{}.{} seen_ms_ago={} blocks={}",
            ip[0], ip[1], ip[2], ip[3], r.peer_seen_ms_ago, blocks
        );
        // Decode our own payload format if 2 blocks: (epoch, last_index, hash)
        let p = r.peer_payload;
        if p.len() >= 48 {
            let epoch = u64::from_be_bytes(p[0..8].try_into().unwrap());
            let last_index = u64::from_be_bytes(p[8..16].try_into().unwrap());
            let last_hash = &p[16..48];
            println!(
                "    decoded: epoch={} last_index={} last_hash={}",
                epoch,
                last_index,
                hex::encode(last_hash)
            );
        }
    } else {
        println!("  not found");
    }
    Ok(())
}
