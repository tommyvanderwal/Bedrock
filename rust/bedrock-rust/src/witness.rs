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
use crate::peer::PeerLiveness;
use std::net::{ToSocketAddrs, UdpSocket};
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn peer_seen_ago_ms(liveness: &PeerLiveness) -> u64 {
    let last = liveness.load(Ordering::Relaxed);
    if last == 0 {
        return u64::MAX;
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    now.saturating_sub(last)
}

/// UDP recv timeout. Tuned for hairpin LAN delay + a stalled IRQ tick:
/// 4 s gives enough slack for one missed scheduling round without the
/// session declaring itself dead. (We saw transient 2.x s recv-stalls
/// on a loaded testbed during scheduled DRBD I/O bursts; 4 s leaves
/// headroom without dragging the lease-loop tick noticeably.)
const RECV_TIMEOUT: Duration = Duration::from_secs(4);
const PAYLOAD_BLOCKS: usize = 2; // 64 bytes

/// Retry-once helper for UDP recv. UDP packets can drop on a busy
/// interface or get delayed past a single timeout window; one retry
/// catches that case without the lease loop tearing down the whole
/// witness session (which would force a fresh DISCOVER + BOOTSTRAP).
fn recv_retry_once(sock: &UdpSocket, buf: &mut [u8]) -> std::io::Result<usize> {
    match sock.recv(buf) {
        Ok(n) => Ok(n),
        Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {
            log::debug!("witness: recv timeout, retrying once");
            sock.recv(buf)
        }
        Err(e) => Err(e),
    }
}

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
///
/// `/tmp` is tmpfs, cleared on reboot — exactly the clean recovery
/// point we want. Reboot is also where DRBD + VMs should NOT auto-
/// start at boot today; bedrock-mgmt must come up first and bring
/// resources online in a safe order. (Documented in the v1 plan;
/// concrete boot-order work is a follow-up.)
pub const FENCE_MARKER_PATH: &str = "/tmp/bedrock-rust.fence";

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
    /// All OTHER nodes' sender_ids (excluding self). Length = cluster
    /// size - 1. Used to compute the weighted-vote total at election
    /// time and to know who must be alive for a smaller-id node to
    /// have priority over us.
    ///
    /// Empty Vec is the standalone case (sender_id 1 alone, no peers
    /// configured) — election trivially returns Leader.
    pub peer_sender_ids: Vec<u8>,
    pub ttl_ms: u64,
    pub heartbeat_ms: u64,
    pub fence_interfaces: Vec<String>,
    /// Shared with peer.rs: monotonic ms timestamp of the most recent
    /// frame received from any peer link. The lease loop uses this to
    /// decide whether the cluster is "alive via peer" — when peer is
    /// recent, witness loss is NOT a self-fence trigger. Per the
    /// design discussion: "If the nodes see each other they NEVER
    /// need a witness; witness is only critical for unplanned downtime."
    pub peer_liveness: crate::peer::PeerLiveness,
    /// Shared with peer.rs / IPC: per-link state. The lease loop counts
    /// distinct peer hosts visible via TCP to compute its quorum vote.
    pub peer_registry: crate::peer::PeerRegistry,
    /// Peer is intentionally offline (operator-marked maintenance
    /// mode). Witness silence + peer silence is now expected; we
    /// keep running solo and never self-fence on lease TTL alone.
    pub peer_in_maintenance: bool,
}

/// Latched current-role view. The lease loop resolves the
/// weighted-vote outcome on every tick and updates this — the only
/// purpose is concise journal output ("election: X → Y") when it
/// changes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Election {
    Leader,
    Follower,
    /// Initial state before any witness reply has been processed.
    Unknown,
}

/// One vote per node = 10; one vote for "I see a witness" = 1. So a
/// 2-node cluster has 2×10+1 = 21 total votes, threshold = 11. A 4-node
/// cluster has 4×10+1 = 41, threshold = 21. The +1 from the witness
/// breaks symmetric splits (2-2, 1-1) — the side with the witness has
/// quorum, the side without does not.
pub const VOTES_PER_NODE: u32 = 10;
pub const VOTE_PER_WITNESS: u32 = 1;

/// Result of the weighted-vote election. `Leader` only when we have
/// quorum AND no smaller-sender_id node is alive in our partition.
/// `Follower` when we have quorum but a smaller-id leader is up, OR
/// the fence marker is present. `NoQuorum` when we lack the votes to
/// be a leader at all — caller decides whether that triggers self-fence
/// (it does, after TTL, unless peer is in maintenance / fresh on the
/// peer wire).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoteOutcome {
    Leader,
    Follower,
    NoQuorum,
}

/// Compute the weighted-vote outcome for this node.
///
/// Two sources inform the answer because they answer two different
/// questions:
///
/// - `n_visible_peers`: peers I can talk to **in my partition** — TCP-
///   link-visible. This drives the quorum count, since the witness
///   can see peers across both sides of a network split.
/// - `smaller_id_alive_anywhere`: a node with sender_id < mine is alive
///   *somewhere* in the cluster, by witness STATUS_LIST. Drives the
///   tiebreak — if a smaller-id node is alive, I yield. (Once that
///   node fences, the witness stops reporting it after takeover-
///   threshold and I become eligible to lead.)
/// - `total_peers`: total OTHER nodes in the cluster (peer_sender_ids
///   length from LeaseConfig).
/// - `witness_reachable`: at least one configured witness responded
///   to our last heartbeat tick → +1 vote.
/// - `fence_marker`: present → forced Follower regardless of votes.
///
/// Worked example (4-node, total_peers=3):
///   total_votes = (1 + 3) * 10 + 1 = 41
///   threshold   = 41 / 2 + 1       = 21    (strict-majority)
///
/// Fully connected, witness up:
///   me (10) + 3 peers (30) + W (1) = 41 ≥ 21 → quorum
/// 2-2 split, our side has witness:
///   me (10) + 1 peer  (10) + W (1) = 21 ≥ 21 → quorum
/// 2-2 split, our side without witness:
///   me (10) + 1 peer  (10)         = 20 <  21 → no quorum, self-fence
/// 1-3 split, alone + witness:
///   me (10) +              W   (1) = 11 <  21 → no quorum, self-fence
pub fn compute_election(
    fence_marker: bool,
    n_visible_peers: usize,
    smaller_id_alive_anywhere: bool,
    total_peers: usize,
    witness_reachable: bool,
) -> VoteOutcome {
    if fence_marker {
        return VoteOutcome::Follower;
    }
    let total_votes = (1 + total_peers as u32) * VOTES_PER_NODE + VOTE_PER_WITNESS;
    let threshold = total_votes / 2 + 1; // strict majority
    let mut my_score = VOTES_PER_NODE; // self
    my_score += VOTES_PER_NODE * (n_visible_peers as u32);
    if witness_reachable {
        my_score += VOTE_PER_WITNESS;
    }
    if my_score < threshold {
        return VoteOutcome::NoQuorum;
    }
    if smaller_id_alive_anywhere {
        VoteOutcome::Follower
    } else {
        VoteOutcome::Leader
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
        "lease: {} witness(es) ttl={}ms heartbeat={}ms peer_sender_ids={:?}",
        cfg.witnesses.len(), cfg.ttl_ms, cfg.heartbeat_ms, cfg.peer_sender_ids,
    );
    for w in &cfg.witnesses {
        log::info!("  witness[{}]: {}:{}", w.id, w.host, w.port);
    }

    // One session slot per witness. None means "needs re-establishing"
    // — happens on first iteration and after any I/O error.
    let mut sessions: Vec<Option<WitnessSession>> = cfg.witnesses.iter().map(|_| None).collect();
    let mut last_ok = Instant::now();
    let mut last_election = Election::Unknown;
    // L49: don't trip TTL until we've had at least one successful
    // heartbeat OR seen the peer alive. A daemon that starts up
    // during a witness blip would otherwise self-fence within ttl_ms
    // even on a healthy cluster — the TTL clock should only count
    // *gaps* between successes, not the startup-to-first-success
    // gap.
    let mut had_success = false;
    // Fence-marker auto-clear (L44). If the daemon comes up with the
    // marker present, election forces Follower regardless of state —
    // safe under the design. We track consecutive healthy ticks and
    // clear the marker once the cluster has been stably-OK for a
    // recovery window. Real faults keep the marker; transient blips
    // that already healed don't lock the operator out forever.
    const HEALTHY_TICKS_TO_CLEAR_MARKER: u32 = 30; // ~30s at 1Hz heartbeat
    let mut healthy_ticks: u32 = 0;

    loop {
        let (latest_index, latest_hash) = {
            let lg = log.lock().unwrap();
            lg.latest()
        };
        let payload = pack_own_payload(/*epoch=*/ 1, latest_index, latest_hash);

        // Always query STATUS_LIST: gives us per-tick visibility into
        // every other peer's witness-recency in one round-trip. (For
        // a 1-peer cluster the list has 1 entry — no extra cost vs
        // the legacy STATUS_DETAIL targeted query.)
        let target = QUERY_LIST_SENTINEL;
        let mut any_ok = false;
        // Map sender_id → seen_ms_ago, freshest reading from any
        // witness that responded this tick.
        let mut witness_seen: std::collections::HashMap<u8, u32> =
            std::collections::HashMap::new();
        for (i, w) in cfg.witnesses.iter().enumerate() {
            let result = (|| -> Result<Vec<(u8, u32)>> {
                if sessions[i].is_none() {
                    sessions[i] = Some(WitnessSession::establish(w, cfg.sender_id)?);
                }
                let sess = sessions[i].as_mut().unwrap();
                let reply = sess.heartbeat_full(&payload, target)?;
                Ok(parse_status_list_entries(&reply, &w.cluster_key))
            })();
            match result {
                Ok(entries) => {
                    any_ok = true;
                    for (sid, ms) in entries {
                        // Keep the freshest reading across witnesses.
                        let entry = witness_seen.entry(sid).or_insert(u32::MAX);
                        if ms < *entry {
                            *entry = ms;
                        }
                    }
                }
                Err(e) => {
                    sessions[i] = None;
                    log::warn!("lease: witness[{}] heartbeat failed: {}", w.id, e);
                }
            }
        }

        if any_ok {
            had_success = true;
            last_ok = Instant::now();
            // Track healthy-tick count for fence auto-clear (L44).
            let peer_recent = peer_seen_ago_ms(&cfg.peer_liveness) < cfg.ttl_ms;
            if peer_recent {
                healthy_ticks = healthy_ticks.saturating_add(1);
                if healthy_ticks >= HEALTHY_TICKS_TO_CLEAR_MARKER && fence_marker_present() {
                    if let Err(e) = std::fs::remove_file(FENCE_MARKER_PATH) {
                        log::warn!("lease: failed to auto-clear fence marker: {}", e);
                    } else {
                        log::warn!(
                            "lease: cluster healthy for {} ticks (witness+peer both OK) — auto-cleared fence marker {}",
                            healthy_ticks, FENCE_MARKER_PATH
                        );
                    }
                }
            } else {
                healthy_ticks = 0;
            }
            // Weighted-vote election. Combine TCP-visible peer count
            // (from peer.rs registry — peers in MY partition) with
            // witness STATUS_LIST sender_ids (alive-anywhere) for the
            // smaller_id-priority tiebreak.
            let takeover_threshold_ms = cfg.ttl_ms.saturating_mul(2) as u32;
            let n_visible_peers = count_distinct_peer_hosts(&cfg.peer_registry);
            let smaller_id_alive_anywhere = cfg
                .peer_sender_ids
                .iter()
                .any(|&sid| {
                    sid < cfg.sender_id
                        && witness_seen
                            .get(&sid)
                            .copied()
                            .map_or(false, |ms| ms <= takeover_threshold_ms)
                });
            let outcome = compute_election(
                fence_marker_present(),
                n_visible_peers,
                smaller_id_alive_anywhere,
                cfg.peer_sender_ids.len(),
                /*witness_reachable=*/ any_ok,
            );
            let next = match outcome {
                VoteOutcome::Leader => Election::Leader,
                VoteOutcome::Follower => Election::Follower,
                VoteOutcome::NoQuorum => Election::Follower,
            };
            if next != last_election {
                log::info!(
                    "election: {:?} → {:?} (idx={} tcp_peers={} witness_seen={:?} smaller_alive={})",
                    last_election, next, latest_index,
                    n_visible_peers, witness_seen, smaller_id_alive_anywhere,
                );
                last_election = next;
            }
            // NoQuorum is a soft signal — the loop's existing TTL
            // logic in the else-branch handles real fencing.
        } else {
            let elapsed = last_ok.elapsed();
            let peer_seen_ms_ago = peer_seen_ago_ms(&cfg.peer_liveness);
            let peer_fresh = peer_seen_ms_ago < cfg.ttl_ms;
            // Quorum check: when there's no witness, peer-freshness alone
            // is insufficient at N≥3. A 2-2 split with no witness has
            // 20/41 votes — peers see each other but cluster has no
            // authority. Only "I have quorum from TCP-visible peers
            // alone" lets us keep running without a witness.
            let n_visible_peers = count_distinct_peer_hosts(&cfg.peer_registry);
            let total_votes =
                (1 + cfg.peer_sender_ids.len() as u32) * VOTES_PER_NODE + VOTE_PER_WITNESS;
            let threshold = total_votes / 2 + 1;
            let my_score_no_w = VOTES_PER_NODE + VOTES_PER_NODE * (n_visible_peers as u32);
            let have_quorum_without_witness = my_score_no_w >= threshold;
            log::warn!(
                "lease: no witness reachable (witness-elapsed {}ms / {}ms; peer last seen {}ms ago, fresh={}; tcp_peers={} score={}/{} quorum={})",
                elapsed.as_millis(), cfg.ttl_ms, peer_seen_ms_ago, peer_fresh,
                n_visible_peers, my_score_no_w, threshold, have_quorum_without_witness,
            );
            if cfg.peer_sender_ids.is_empty() {
                // Standalone (1-node): no peers to wait for. The witness
                // adds the +1 vote in normal operation; without it,
                // 10/11 still meets threshold (single-node majority).
                last_ok = Instant::now();
            } else if cfg.peer_in_maintenance && peer_fresh {
                // Peer is intentionally offline; surviving partition
                // keeps running. (This wins over the quorum check —
                // a planned outage shouldn't fence the survivor even if
                // the math says no quorum.)
                if elapsed >= Duration::from_millis(cfg.ttl_ms) {
                    log::warn!(
                        "lease: witness TTL exhausted but peer in maintenance — keeping running solo"
                    );
                }
                last_ok = Instant::now();
            } else if cfg.peer_in_maintenance {
                last_ok = Instant::now();
            } else if have_quorum_without_witness {
                // Cluster is healthy via peer cables; witness is just
                // a tiebreaker we don't currently need.
                last_ok = Instant::now();
            } else if !had_success {
                // L49: daemon hasn't had a single successful heartbeat
                // yet — startup-during-witness-blip is not fatal.
                log::info!("lease: still waiting for first witness contact (startup grace)");
            } else if elapsed >= Duration::from_millis(cfg.ttl_ms) {
                log::error!(
                    "lease: TTL exhausted, no witness, and no peer-quorum (score {}/{}); self-fence",
                    my_score_no_w, threshold,
                );
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
        })
    }

    /// Send a HEARTBEAT, return the raw reply bytes for the caller to
    /// decode (lease loop parses STATUS_LIST entries from this).
    fn heartbeat_full(&mut self, payload: &[u8], query_target: u8) -> Result<Vec<u8>> {
        let mut buf = [0u8; MTU_CAP];
        let n = msg::encode_heartbeat(
            &mut buf, self.sender_id, now_ms(),
            query_target, payload, &self.cluster_key,
        )
        .map_err(|e| anyhow::anyhow!("encode_heartbeat: {:?}", e))?;
        self.sock.send(&buf[..n])?;
        let mut reply = [0u8; MTU_CAP];
        let n = recv_retry_once(&self.sock, &mut reply)?;
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

fn parse_status_list_entries(raw: &[u8], cluster_key: &[u8; 32]) -> Vec<(u8, u32)> {
    if raw.len() < HEADER_LEN || raw[4] != MSG_STATUS_LIST {
        return Vec::new();
    }
    let mut buf = raw.to_vec();
    let r = match msg::decode_status_list_into(&mut buf, cluster_key) {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };
    let mut out = Vec::with_capacity(r.num_entries as usize);
    for i in 0..(r.num_entries as usize) {
        if let Some(e) = r.entry(i) {
            out.push((e.peer_sender_id, e.last_seen_ms));
        }
    }
    out
}

fn count_distinct_peer_hosts(reg: &crate::peer::PeerRegistry) -> usize {
    use std::collections::HashSet;
    let snap = reg.snapshot();
    let hosts: HashSet<String> = snap
        .iter()
        .filter_map(|l| {
            // address looks like "10.99.0.11:8200" or "10.99.0.11:38656".
            l.address.rsplit_once(':').map(|(h, _)| h.to_string())
        })
        .collect();
    hosts.len()
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

    // ── Weighted-vote (compute_election) ───────────────────────────────

    #[test]
    fn fence_marker_forces_follower_under_quorum() {
        // Even with full quorum, fence marker overrides — the node
        // refuses leader regardless of vote count.
        assert_eq!(
            compute_election(true, 3, false, 3, true),
            VoteOutcome::Follower
        );
    }

    #[test]
    fn standalone_one_node_always_leader() {
        // 1 node, no peers. total_votes = 11, threshold = 6, score = 10 (+1 W).
        assert_eq!(compute_election(false, 0, false, 0, false), VoteOutcome::Leader);
        assert_eq!(compute_election(false, 0, false, 0, true), VoteOutcome::Leader);
    }

    #[test]
    fn two_node_partition_witness_breaks_tie() {
        // 2-node, total=21, threshold=11.
        // Both peers visible, witness up: 10 + 10 + 1 = 21 → quorum.
        assert_eq!(
            compute_election(false, 1, true, 1, true),
            VoteOutcome::Follower // smaller-id peer is alive, we're the larger-id node
        );
        // Peer-down (no smaller-id alive), witness up: 10 + 1 = 11 → quorum, leader.
        assert_eq!(
            compute_election(false, 0, false, 1, true),
            VoteOutcome::Leader
        );
        // Peer-down, no witness: 10 → no quorum.
        assert_eq!(
            compute_election(false, 0, false, 1, false),
            VoteOutcome::NoQuorum
        );
    }

    #[test]
    fn four_node_full_visibility_lowest_id_leads() {
        // 4-node, total = 4*10+1 = 41, threshold = 21.
        // Lowest-id node sees all peers, no smaller-id alive (no one is smaller).
        assert_eq!(
            compute_election(false, 3, false, 3, true),
            VoteOutcome::Leader
        );
        // Higher-id node sees all peers, smaller-id is alive → follower.
        assert_eq!(
            compute_election(false, 3, true, 3, true),
            VoteOutcome::Follower
        );
    }

    #[test]
    fn four_node_two_two_split_witness_decides() {
        // 4-node, total=41, threshold=21. 2-2 split: I see 1 peer.
        // With witness: 10 + 10 + 1 = 21 → quorum.
        assert_eq!(
            compute_election(false, 1, false, 3, true),
            VoteOutcome::Leader
        );
        // Without witness: 10 + 10 = 20 → no quorum, must self-fence.
        assert_eq!(
            compute_election(false, 1, false, 3, false),
            VoteOutcome::NoQuorum
        );
    }

    #[test]
    fn four_node_one_three_split_singleton_fences() {
        // I'm alone (3 peers in the other partition). With witness: 11 < 21.
        assert_eq!(
            compute_election(false, 0, true, 3, true),
            VoteOutcome::NoQuorum
        );
        // No witness: 10 < 21.
        assert_eq!(
            compute_election(false, 0, true, 3, false),
            VoteOutcome::NoQuorum
        );
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
