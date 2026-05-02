//! Peer-to-peer transport for log replication and heartbeats.
//!
//! Per design §7: "two nodes connected by **at least one** direct
//! cable, ideally two ... each cable is a separate Rust-managed
//! transport." v0.1 implements multi-link end-to-end:
//!
//! - The operator passes any number of `--peer-listen <host:port>`
//!   addresses; each gets its own listener thread.
//! - The operator passes any number of `--peer <host:port>` addresses;
//!   each gets its own outbound transport thread.
//! - Heartbeats fan out to **every** active link.
//! - Log replication picks one healthy link as the **active replicator**
//!   and tail-pushes there. If that link errors, the next-arriving
//!   ReplicateRequest from the follower picks up on whichever link is
//!   still healthy.
//! - The whole system stays operational as long as at least one link
//!   is up. Multi-link is a convenience: 2 cables are better than 1
//!   for orthogonality (PHY/driver/connector), but never required.
//!
//! Frames are length-prefixed MessagePack, same shape as IPC.

use crate::ipc::EntryWire;
use crate::log_store::Log;
use crate::payload::Kind;
use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use socket2::{SockRef, TcpKeepalive};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

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
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Monotonic-ish wall-clock ms shared between peer.rs and witness.rs:
/// peer bumps `last_peer_seen_ms` on every received frame; the lease
/// loop reads it to decide whether the cluster is "alive via peer"
/// (witness liveness becomes optional for self-fence in that case).
pub type PeerLiveness = Arc<AtomicU64>;

pub fn new_peer_liveness() -> PeerLiveness {
    Arc::new(AtomicU64::new(0))
}

/// Per-link state snapshotted for IPC PeerStatus replies and used by
/// `_wait_replicated` (the Python verb that waits for a log entry to
/// reach all peers before declaring the cluster operation done).
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct PeerLinkInfo {
    pub address: String,
    pub direction: String,           // "inbound" | "outbound"
    pub identified_role: String,     // "leader" | "follower" | "standalone" | ""
    pub latest_index: u64,           // most recent latest_index advertised by the peer
    pub last_acked_index: u64,       // highest Ack we've received from this peer
    pub last_frame_ms_ago: u64,      // ms since the last frame on this link
}

#[derive(Default)]
struct PeerRegistryInner {
    /// Keyed by stable id (address). Each link gets one entry.
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
                // last_frame_ms_ago is computed on read.
                copy.last_frame_ms_ago = now.saturating_sub(l.last_frame_ms_ago);
                copy
            })
            .collect()
    }

    fn touch_frame(&self, key: &str, fill: impl FnOnce(&mut PeerLinkInfo)) {
        let mut inner = self.0.lock().unwrap();
        let entry = inner.links.entry(key.to_string()).or_default();
        entry.last_frame_ms_ago = now_ms(); // store the timestamp; snapshot() converts to ago
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

    pub fn observed_latest(&self, key: &str, idx: u64) {
        self.touch_frame(key, |l| l.latest_index = l.latest_index.max(idx));
    }

    pub fn observed_ack(&self, key: &str, up_to_index: u64) {
        self.touch_frame(key, |l| l.last_acked_index = l.last_acked_index.max(up_to_index));
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
    pub log: Arc<Mutex<Log>>,
    pub listen_addrs: Vec<String>,
    pub connect_to: Vec<String>,
    pub role: Role,
    /// Shared with witness.rs lease loop. Bumped on every received
    /// peer frame so the lease loop can decide whether the cluster
    /// is alive without the witness. See design discussion §6:
    /// "If the nodes see each other they NEVER need a witness."
    pub liveness: PeerLiveness,
    /// Per-link state surface for IPC PeerStatus + `_wait_replicated`.
    pub registry: PeerRegistry,
    /// Subscriber channel for committed entries — peer-replicated
    /// appends notify the IPC subscribers via this hook so the
    /// per-node watchers wake on the same commit event the local
    /// IPC append path uses.
    pub on_commit: crate::ipc::CommitNotifier,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum PeerFrame {
    Identify {
        node_role: String,
    },
    /// Follower → leader: "send me everything from this index onward".
    ReplicateRequest {
        from_index: u64,
    },
    /// Leader → follower: one log entry, raw on-disk format.
    ReplicateEntry {
        entry: EntryWire,
    },
    /// Bidirectional liveness — small enough to send constantly.
    Heartbeat {
        latest_index: u64,
        #[serde(with = "crate::ipc::serde_bytes_array")]
        latest_hash: [u8; 32],
    },
    Ack {
        up_to_index: u64,
    },
}

const FRAME_LEN_BYTES: usize = 4;
const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;

/// Spawn a listener thread per `listen_addrs` and an outbound thread per
/// `connect_to`. Each thread is independent — losing one doesn't stop
/// the others. Returns the join handles so the daemon's main thread
/// can keep them alive (we don't actually join — they run for the
/// process lifetime).
pub fn start(cfg: Config) -> anyhow::Result<Vec<JoinHandle<()>>> {
    let mut handles = Vec::new();
    for addr in &cfg.listen_addrs {
        let listener = TcpListener::bind(addr)
            .map_err(|e| anyhow::anyhow!("peer: bind {}: {}", addr, e))?;
        log::info!("peer: listening on {}", addr);
        let log_handle = Arc::clone(&cfg.log);
        let role = cfg.role;
        let liveness = Arc::clone(&cfg.liveness);
        let registry = cfg.registry.clone();
        let on_commit = cfg.on_commit.clone();
        let addr = addr.clone();
        handles.push(thread::spawn(move || {
            for stream in listener.incoming() {
                match stream {
                    Ok(s) => {
                        let lg = Arc::clone(&log_handle);
                        let lv = Arc::clone(&liveness);
                        let reg = registry.clone();
                        let oc = on_commit.clone();
                        let listen_addr = addr.clone();
                        thread::spawn(move || {
                            let peer_addr = s
                                .peer_addr()
                                .map(|a| a.to_string())
                                .unwrap_or_else(|_| "<unknown>".to_string());
                            log::info!("peer: link[{}] inbound from {}", listen_addr, peer_addr);
                            let key = format!("in:{}", peer_addr);
                            reg.link_connected(&key, peer_addr.clone(), "inbound");
                            let res = handle_stream(s, lg, role, true, lv, &reg, &key, &oc);
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
        let log_handle = Arc::clone(&cfg.log);
        let role = cfg.role;
        let liveness = Arc::clone(&cfg.liveness);
        let registry = cfg.registry.clone();
        let on_commit = cfg.on_commit.clone();
        let target = target.clone();
        handles.push(thread::spawn(move || loop {
            let key = format!("out:{}", target);
            match TcpStream::connect(&target) {
                Ok(s) => {
                    log::info!("peer: link[{}] outbound connected", target);
                    registry.link_connected(&key, target.clone(), "outbound");
                    let res = handle_stream(s, Arc::clone(&log_handle), role, false, Arc::clone(&liveness), &registry, &key, &on_commit);
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

/// Drive a single TCP transport (one link). Both directions run the
/// same protocol after sending Identify; what each side does after
/// that depends on its `role`.
///
/// The read timeout is short (TICK_MS) so each tick a leader can
/// opportunistically push newly-appended entries even if the follower
/// hasn't sent anything to wake the loop. (Without this, the leader
/// blocks on read_frame and only pushes new entries when the follower
/// sends a frame it never sends.)
fn handle_stream(
    mut stream: TcpStream,
    log: Arc<Mutex<Log>>,
    role: Role,
    inbound: bool,
    liveness: PeerLiveness,
    registry: &PeerRegistry,
    link_key: &str,
    on_commit: &crate::ipc::CommitNotifier,
) -> anyhow::Result<()> {
    enable_keepalive(&stream);
    stream.set_read_timeout(Some(Duration::from_millis(TICK_MS)))?;
    write_frame(
        &mut stream,
        &PeerFrame::Identify {
            node_role: role_str(role).to_string(),
        },
    )?;

    if !inbound && matches!(role, Role::Follower) {
        let from_index = log.lock().unwrap().latest().0 + 1;
        log::info!("peer: link asking leader for entries from {}", from_index);
        write_frame(&mut stream, &PeerFrame::ReplicateRequest { from_index })?;
    }

    let mut tail_state: Option<TailState> = None;
    let mut last_idle_hb = Instant::now();

    loop {
        match read_frame(&mut stream) {
            Ok(None) => return Ok(()), // peer closed
            Ok(Some(frame)) => {
                liveness.store(now_ms(), Ordering::Relaxed);
                match frame {
                PeerFrame::Identify { node_role } => {
                    log::info!("peer: link identified peer as {}", node_role);
                    registry.observed_role(link_key, node_role);
                }
                PeerFrame::ReplicateRequest { from_index } => {
                    if !matches!(role, Role::Leader) {
                        log::warn!("peer: replicate request to non-leader; ignoring");
                        continue;
                    }
                    tail_state = Some(TailState { next_index: from_index });
                    if let Some(ts) = tail_state.as_mut() {
                        ts.next_index = push_range(&log, &mut stream, ts.next_index)?;
                    }
                }
                PeerFrame::ReplicateEntry { entry } => {
                    if !matches!(role, Role::Follower) {
                        log::warn!("peer: replicate entry to non-follower; ignoring");
                        continue;
                    }
                    apply_replicated_entry(&log, &mut stream, entry, on_commit)?;
                }
                PeerFrame::Heartbeat { latest_index, .. } => {
                    log::debug!("peer: link heartbeat latest={}", latest_index);
                    registry.observed_latest(link_key, latest_index);
                }
                PeerFrame::Ack { up_to_index } => {
                    log::debug!("peer: link ack up_to={}", up_to_index);
                    registry.observed_ack(link_key, up_to_index);
                }
            }},
            Err(e) if is_timeout(&e) => {
                // No inbound frame this tick — that's fine, fall through
                // to the leader-tail-push below.
            }
            Err(e) => return Err(e),
        }

        // Leader: tail-push any new entries every tick, plus an idle
        // heartbeat so the follower's read timeout never fires on a
        // quiet link.
        if let (Role::Leader, Some(ts)) = (role, tail_state.as_mut()) {
            ts.next_index = push_range(&log, &mut stream, ts.next_index)?;
            if last_idle_hb.elapsed() > Duration::from_millis(IDLE_HB_MS) {
                let (idx, hash) = log.lock().unwrap().latest();
                write_frame(
                    &mut stream,
                    &PeerFrame::Heartbeat {
                        latest_index: idx,
                        latest_hash: hash,
                    },
                )?;
                last_idle_hb = Instant::now();
            }
        }
    }
}

const TICK_MS: u64 = 200;
const IDLE_HB_MS: u64 = 2000;

fn is_timeout(e: &anyhow::Error) -> bool {
    if let Some(io_err) = e.downcast_ref::<std::io::Error>() {
        return matches!(
            io_err.kind(),
            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
        );
    }
    false
}

struct TailState {
    next_index: u64,
}

fn push_range(
    log: &Arc<Mutex<Log>>,
    stream: &mut TcpStream,
    from_index: u64,
) -> anyhow::Result<u64> {
    let snapshot: Vec<EntryWire> = {
        let lg = log.lock().unwrap();
        let to = lg.latest().0;
        if to < from_index {
            return Ok(from_index);
        }
        let mut out = Vec::new();
        for idx in from_index..=to {
            if let Some(e) = lg.read(idx)? {
                out.push(EntryWire {
                    index: e.index,
                    epoch: e.epoch,
                    prev_hash: e.prev_hash,
                    kind: e.kind,
                    payload: e.payload,
                    hash: e.hash,
                });
            }
        }
        out
    };
    if !snapshot.is_empty() {
        log::info!("peer: pushing {} entries from {}", snapshot.len(), from_index);
    }
    let last_pushed = snapshot.last().map(|e| e.index).unwrap_or(from_index - 1);
    for entry in snapshot {
        write_frame(stream, &PeerFrame::ReplicateEntry { entry })?;
    }
    Ok(last_pushed + 1)
}

fn apply_replicated_entry(
    log: &Arc<Mutex<Log>>,
    stream: &mut TcpStream,
    entry: EntryWire,
    on_commit: &crate::ipc::CommitNotifier,
) -> anyhow::Result<()> {
    let mut lg = log.lock().unwrap();
    let (latest_index, latest_hash) = lg.latest();

    if entry.index <= latest_index {
        // We already have this entry — verify byte-for-byte agreement
        // (design §4: hash-chain divergence detection at every step).
        match lg.read(entry.index)? {
            Some(local) => {
                if local.hash != entry.hash {
                    anyhow::bail!(
                        "peer: DIVERGENCE at index {}: local hash {} ≠ leader hash {}",
                        entry.index,
                        hex::encode(local.hash),
                        hex::encode(entry.hash),
                    );
                }
            }
            None => log::warn!("peer: missing entry {} for verify", entry.index),
        }
        return Ok(());
    }
    if entry.index != latest_index + 1 {
        anyhow::bail!(
            "peer: gap — got index {}, our latest is {}",
            entry.index,
            latest_index
        );
    }
    if entry.prev_hash != latest_hash {
        anyhow::bail!(
            "peer: chain break — entry {}'s prev_hash {} ≠ our latest hash {}",
            entry.index,
            hex::encode(entry.prev_hash),
            hex::encode(latest_hash),
        );
    }

    let kind = Kind::from_u8(entry.kind)
        .ok_or_else(|| anyhow::anyhow!("unknown payload kind 0x{:02x}", entry.kind))?;
    let appended = lg.append(kind, &entry.payload)?;
    if appended.hash != entry.hash {
        anyhow::bail!(
            "peer: post-append hash mismatch (we got {}, leader got {})",
            hex::encode(appended.hash),
            hex::encode(entry.hash),
        );
    }
    log::info!("peer: applied entry {} hash={}", appended.index, hex::encode(&appended.hash[..6]));
    // Notify any local IPC subscribers that this entry was committed
    // — same hook the IPC append path uses, so the per-node watcher
    // wakes on replicated commits as well as locally-originated ones.
    let wire = crate::ipc::EntryWire {
        index: appended.index,
        epoch: appended.epoch,
        prev_hash: appended.prev_hash,
        kind: appended.kind,
        payload: appended.payload.clone(),
        hash: appended.hash,
    };
    on_commit.notify(&wire);
    write_frame(
        stream,
        &PeerFrame::Ack {
            up_to_index: appended.index,
        },
    )?;
    Ok(())
}

fn role_str(r: Role) -> &'static str {
    match r {
        Role::Standalone => "standalone",
        Role::Leader => "leader",
        Role::Follower => "follower",
    }
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
