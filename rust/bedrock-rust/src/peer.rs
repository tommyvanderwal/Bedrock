//! Peer-to-peer transport for liveness only.
//!
//! Post-rqlite (D-22): this module no longer replicates logs. Rqlite
//! handles its own Raft replication for cluster state. What stays
//! in bedrock-rust:
//!
//!   * **TCP keepalive** — fast "is the peer alive" signal,
//!     independent of rqlite's HTTP health. The lease loop in
//!     witness.rs reads `PeerLiveness` to decide whether the cluster
//!     is alive without the witness ("if the nodes see each other
//!     they NEVER need a witness").
//!   * **Multi-link awareness** — one TCP connection per
//!     `--peer-listen` / `--peer` address; losing one doesn't stop
//!     the others, which gives orthogonality across PHY / driver /
//!     connector. Mesh routing happens above; this is the cluster-
//!     protocol-level "are you there?" check.
//!   * **PeerRegistry** — per-link state surface for the
//!     `Status` / `PeerStatus` IPC verbs (the orchestrator's
//!     `_wait_replicated` watcher reads it to know when a peer is
//!     within heartbeat-ack distance).

use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use socket2::{SockRef, TcpKeepalive};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Set aggressive TCP keepalive on a peer-link socket so iptables-style
/// silent partitions get torn down within ~15s instead of waiting for
/// Linux's 2-hour default. The 3-probe / 5s interval pattern is what
/// the design assumes — a real peer-down should be detected before
/// the witness takeover threshold (2× ttl_ms = 10s by default).
fn enable_keepalive(stream: &TcpStream) {
    let ka = TcpKeepalive::new()
        .with_time(Duration::from_secs(5))
        .with_interval(Duration::from_secs(3))
        .with_retries(3);
    if let Err(e) = SockRef::from(stream).set_tcp_keepalive(&ka) {
        log::warn!("peer: failed to enable tcp keepalive: {}", e);
    }
    let _ = stream.set_nodelay(true);
}

/// Monotonic-ish wall-clock ms shared between peer.rs and witness.rs:
/// peer bumps `last_peer_seen_ms` on every received frame; the lease
/// loop reads it to decide whether the cluster is "alive via peer"
/// (witness liveness becomes optional for self-fence in that case).
pub type PeerLiveness = Arc<AtomicU64>;

pub fn new_peer_liveness() -> PeerLiveness {
    Arc::new(AtomicU64::new(0))
}

/// Per-link state snapshotted for IPC PeerStatus replies.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct PeerLinkInfo {
    pub address: String,
    pub direction: String,           // "inbound" | "outbound"
    pub identified_role: String,     // "leader" | "follower" | "standalone" | ""
    pub last_frame_ms_ago: u64,      // ms since the last frame on this link
}

#[derive(Default)]
struct PeerRegistryInner {
    links: std::collections::HashMap<String, PeerLinkInfo>,
}

#[derive(Clone)]
pub struct PeerRegistry(Arc<Mutex<PeerRegistryInner>>);

pub fn new_peer_registry() -> PeerRegistry {
    PeerRegistry(Arc::new(Mutex::new(PeerRegistryInner::default())))
}

impl PeerRegistry {
    pub fn snapshot(&self) -> Vec<PeerLinkInfo> {
        let now = now_ms();
        let inner = self.0.lock().unwrap();
        inner
            .links
            .values()
            .map(|l| {
                let mut copy = l.clone();
                copy.last_frame_ms_ago = now.saturating_sub(l.last_frame_ms_ago);
                copy
            })
            .collect()
    }

    fn touch_frame(&self, key: &str, fill: impl FnOnce(&mut PeerLinkInfo)) {
        let mut inner = self.0.lock().unwrap();
        let entry = inner.links.entry(key.to_string()).or_default();
        entry.last_frame_ms_ago = now_ms();
        fill(entry);
    }

    pub fn link_connected(&self, key: &str, address: String, direction: &str) {
        self.touch_frame(key, |l| {
            l.address = address;
            l.direction = direction.to_string();
        });
    }

    pub fn link_disconnected(&self, key: &str) {
        let mut inner = self.0.lock().unwrap();
        inner.links.remove(key);
    }

    pub fn observed_role(&self, key: &str, role: String) {
        self.touch_frame(key, |l| l.identified_role = role);
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Role {
    Standalone,
    Leader,
    Follower,
}

pub struct Config {
    pub listen_addrs: Vec<String>,
    pub connect_to: Vec<String>,
    pub role: Role,
    /// Bumped on every received peer frame so the witness lease loop
    /// can decide whether the cluster is alive without the witness.
    pub liveness: PeerLiveness,
    pub registry: PeerRegistry,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum PeerFrame {
    /// Sent once at connection setup so each side knows what role its
    /// peer thinks it has. Useful for logging and for `_wait_replicated`
    /// to short-circuit on standalone peers.
    Identify { node_role: String },
    /// Bidirectional liveness — small enough to send constantly. The
    /// payload carries no consensus information anymore; rqlite owns
    /// state. This frame exists only so the receiver can stamp
    /// last_peer_seen_ms and the keepalive socket has actual traffic
    /// to keep its kernel state warm.
    Heartbeat,
}

const FRAME_LEN_BYTES: usize = 4;
const MAX_FRAME_BYTES: usize = 64 * 1024;
const TICK_MS: u64 = 200;
const IDLE_HB_MS: u64 = 2_000;

/// Spawn a listener thread per `listen_addrs` and an outbound thread
/// per `connect_to`. Each thread is independent. Returns the join
/// handles so the daemon's main thread can keep them alive (we don't
/// actually join — they run for the process lifetime).
pub fn start(cfg: Config) -> anyhow::Result<Vec<JoinHandle<()>>> {
    let mut handles = Vec::new();
    for addr in &cfg.listen_addrs {
        let listener = TcpListener::bind(addr)
            .map_err(|e| anyhow::anyhow!("peer: bind {}: {}", addr, e))?;
        log::info!("peer: listening on {}", addr);
        let role = cfg.role;
        let liveness = Arc::clone(&cfg.liveness);
        let registry = cfg.registry.clone();
        let addr = addr.clone();
        handles.push(thread::spawn(move || {
            for stream in listener.incoming() {
                match stream {
                    Ok(s) => {
                        let lv = Arc::clone(&liveness);
                        let reg = registry.clone();
                        let listen_addr = addr.clone();
                        thread::spawn(move || {
                            let peer_addr = s
                                .peer_addr()
                                .map(|a| a.to_string())
                                .unwrap_or_else(|_| "<unknown>".to_string());
                            log::info!("peer: link[{}] inbound from {}", listen_addr, peer_addr);
                            let key = format!("in:{}", peer_addr);
                            reg.link_connected(&key, peer_addr.clone(), "inbound");
                            let res = handle_stream(s, role, lv, &reg, &key);
                            reg.link_disconnected(&key);
                            if let Err(e) = res {
                                log::warn!("peer: link[{}] inbound from {}: {}", listen_addr, peer_addr, e);
                            }
                        });
                    }
                    Err(e) => log::warn!("peer: accept on {}: {}", addr, e),
                }
            }
        }));
    }

    for target in &cfg.connect_to {
        let role = cfg.role;
        let liveness = Arc::clone(&cfg.liveness);
        let registry = cfg.registry.clone();
        let target = target.clone();
        handles.push(thread::spawn(move || loop {
            let key = format!("out:{}", target);
            match TcpStream::connect(&target) {
                Ok(s) => {
                    log::info!("peer: link[{}] outbound connected", target);
                    registry.link_connected(&key, target.clone(), "outbound");
                    let res = handle_stream(
                        s, role, Arc::clone(&liveness), &registry, &key,
                    );
                    registry.link_disconnected(&key);
                    if let Err(e) = res {
                        log::warn!("peer: link[{}] outbound: {}", target, e);
                    }
                }
                Err(e) => log::debug!("peer: link[{}] connect: {}", target, e),
            }
            thread::sleep(Duration::from_secs(2));
        }));
    }

    if cfg.connect_to.is_empty() && cfg.listen_addrs.is_empty() {
        log::info!("peer: no listen + no connect — running headless");
    }
    Ok(handles)
}

/// Drive a single TCP transport (one link). Both directions speak
/// the same protocol: an initial Identify, then idle Heartbeats.
fn handle_stream(
    mut stream: TcpStream,
    role: Role,
    liveness: PeerLiveness,
    registry: &PeerRegistry,
    link_key: &str,
) -> anyhow::Result<()> {
    enable_keepalive(&stream);
    stream.set_read_timeout(Some(Duration::from_millis(TICK_MS)))?;
    write_frame(
        &mut stream,
        &PeerFrame::Identify {
            node_role: role_str(role).to_string(),
        },
    )?;

    let mut last_idle_hb = Instant::now();
    loop {
        match read_frame(&mut stream) {
            Ok(None) => return Ok(()),   // peer closed
            Ok(Some(frame)) => {
                liveness.store(now_ms(), Ordering::Relaxed);
                match frame {
                    PeerFrame::Identify { node_role } => {
                        log::info!("peer: link identified peer as {}", node_role);
                        registry.observed_role(link_key, node_role);
                    }
                    PeerFrame::Heartbeat => {
                        log::trace!("peer: heartbeat received");
                    }
                }
            }
            Err(e) if is_timeout(&e) => { /* fall through to idle HB */ }
            Err(e) => return Err(e),
        }

        // Idle heartbeat so the keepalive socket has actual traffic
        // and the peer's read timeout never fires on a quiet link.
        if last_idle_hb.elapsed() > Duration::from_millis(IDLE_HB_MS) {
            write_frame(&mut stream, &PeerFrame::Heartbeat)?;
            last_idle_hb = Instant::now();
        }
    }
}

fn role_str(r: Role) -> &'static str {
    match r {
        Role::Standalone => "standalone",
        Role::Leader => "leader",
        Role::Follower => "follower",
    }
}

fn is_timeout(e: &anyhow::Error) -> bool {
    if let Some(io_err) = e.downcast_ref::<std::io::Error>() {
        return matches!(
            io_err.kind(),
            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
        );
    }
    false
}

fn read_frame<R: Read>(r: &mut R) -> anyhow::Result<Option<PeerFrame>> {
    let mut len_buf = [0u8; FRAME_LEN_BYTES];
    match r.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let n = u32::from_be_bytes(len_buf) as usize;
    if n > MAX_FRAME_BYTES {
        anyhow::bail!("peer frame oversized: {n}");
    }
    let mut body = vec![0u8; n];
    r.read_exact(&mut body)?;
    Ok(Some(rmp_serde::from_slice(&body)?))
}

fn write_frame<W: Write>(w: &mut W, frame: &PeerFrame) -> anyhow::Result<()> {
    let body = rmp_serde::to_vec_named(frame)?;
    let len = (body.len() as u32).to_be_bytes();
    w.write_all(&len)?;
    w.write_all(&body)?;
    w.flush()?;
    Ok(())
}
