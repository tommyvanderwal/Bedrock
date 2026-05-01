//! Peer-to-peer transport for log replication and heartbeats.
//!
//! v0.1 single-link, single-leader, single-follower. Multi-link
//! transport (design §7) is phase 7. Frames are length-prefixed
//! MessagePack, same shape as IPC frames so we have one wire format
//! to reason about.
//!
//! Configured leader pushes new entries to the configured follower.
//! Phase 4 will replace this static role with witness-based election.

use crate::ipc::EntryWire;
use crate::log_store::Log;
use crate::payload::Kind;
use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Role {
    /// No peer — solo node, IPC only.
    Standalone,
    /// Push new appends to the configured follower.
    Leader,
    /// Accept replication from the configured leader.
    Follower,
}

pub struct Config {
    pub log: Arc<Mutex<Log>>,
    pub listen_addr: String,
    pub connect_to: Option<String>,
    pub role: Role,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum PeerFrame {
    Identify {
        node_role: String,
        peer_listen: String,
    },
    /// Follower → leader: "send me everything from this index onward".
    ReplicateRequest { from_index: u64 },
    /// Leader → follower: one log entry, raw on-disk format.
    ReplicateEntry { entry: EntryWire },
    /// Bidirectional liveness — small enough to send constantly.
    Heartbeat { latest_index: u64, latest_hash: [u8; 32] },
    Ack { up_to_index: u64 },
}

const FRAME_LEN_BYTES: usize = 4;
const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;

pub fn start(cfg: Config) -> anyhow::Result<(JoinHandle<()>, JoinHandle<()>)> {
    let listener = TcpListener::bind(&cfg.listen_addr)?;
    log::info!("peer: listening on {}", cfg.listen_addr);
    let listen_handle = {
        let log = Arc::clone(&cfg.log);
        let role = cfg.role;
        thread::spawn(move || {
            for stream in listener.incoming() {
                match stream {
                    Ok(s) => {
                        let log = Arc::clone(&log);
                        thread::spawn(move || {
                            if let Err(e) = handle_inbound(s, log, role) {
                                log_warn(&format!("inbound: {e}"));
                            }
                        });
                    }
                    Err(e) => log_warn(&format!("accept: {e}")),
                }
            }
        })
    };

    let connect_handle = {
        let log = Arc::clone(&cfg.log);
        let connect_to = cfg.connect_to.clone();
        let role = cfg.role;
        thread::spawn(move || {
            if let Some(addr) = connect_to {
                loop {
                    match TcpStream::connect(&addr) {
                        Ok(s) => {
                            log::info!("peer: connected to {}", addr);
                            if let Err(e) = drive_outbound(s, Arc::clone(&log), role) {
                                log_warn(&format!("outbound to {addr}: {e}"));
                            }
                        }
                        Err(e) => log_warn(&format!("connect {addr}: {e}")),
                    }
                    // Reconnect with a small backoff on link error / peer
                    // restart. The followers' replicate loop is built to
                    // be idempotent on resume.
                    thread::sleep(Duration::from_secs(2));
                }
            }
        })
    };

    Ok((listen_handle, connect_handle))
}

fn handle_inbound(mut stream: TcpStream, log: Arc<Mutex<Log>>, role: Role) -> anyhow::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    // Send Identify.
    let our_role = role_str(role);
    write_frame(
        &mut stream,
        &PeerFrame::Identify {
            node_role: our_role.to_string(),
            peer_listen: String::new(),
        },
    )?;

    loop {
        let frame = match read_frame(&mut stream)? {
            Some(f) => f,
            None => return Ok(()),
        };
        match frame {
            PeerFrame::Identify { node_role, .. } => {
                log::info!("peer (in): identified as {}", node_role);
            }
            PeerFrame::ReplicateRequest { from_index } => {
                if !matches!(role, Role::Leader) {
                    log_warn("peer (in): replicate request to non-leader; ignoring");
                    continue;
                }
                replicate_from(&log, &mut stream, from_index)?;
            }
            PeerFrame::ReplicateEntry { entry } => {
                if !matches!(role, Role::Follower) {
                    log_warn("peer (in): replicate entry to non-follower; ignoring");
                    continue;
                }
                apply_replicated_entry(&log, &mut stream, entry)?;
            }
            PeerFrame::Heartbeat { latest_index, .. } => {
                log::debug!("peer (in): hb latest={}", latest_index);
            }
            PeerFrame::Ack { up_to_index } => {
                log::debug!("peer (in): ack up_to={}", up_to_index);
            }
        }
    }
}

fn drive_outbound(mut stream: TcpStream, log: Arc<Mutex<Log>>, role: Role) -> anyhow::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    write_frame(
        &mut stream,
        &PeerFrame::Identify {
            node_role: role_str(role).to_string(),
            peer_listen: String::new(),
        },
    )?;

    match role {
        Role::Follower => {
            // Ask the leader to start replicating from one past our latest.
            let from_index = {
                let lg = log.lock().unwrap();
                lg.latest().0 + 1
            };
            log::info!("peer (out): asking leader for entries from {}", from_index);
            write_frame(&mut stream, &PeerFrame::ReplicateRequest { from_index })?;

            loop {
                let frame = match read_frame(&mut stream)? {
                    Some(f) => f,
                    None => anyhow::bail!("leader closed connection"),
                };
                match frame {
                    PeerFrame::ReplicateEntry { entry } => {
                        apply_replicated_entry(&log, &mut stream, entry)?;
                    }
                    PeerFrame::Heartbeat { .. } => {}
                    PeerFrame::Identify { node_role, .. } => {
                        log::info!("peer (out): leader identified as {}", node_role);
                    }
                    PeerFrame::Ack { .. } => {}
                    PeerFrame::ReplicateRequest { .. } => {
                        log_warn("peer (out): leader sent us a ReplicateRequest? ignoring");
                    }
                }
            }
        }
        Role::Leader => {
            // Leader on the connect side waits to be asked. Heartbeat
            // every couple of seconds so the follower side knows we're
            // alive even when no entries are flying.
            loop {
                {
                    let lg = log.lock().unwrap();
                    let (idx, hash) = lg.latest();
                    write_frame(
                        &mut stream,
                        &PeerFrame::Heartbeat {
                            latest_index: idx,
                            latest_hash: hash,
                        },
                    )?;
                }
                thread::sleep(Duration::from_secs(2));
            }
        }
        Role::Standalone => Ok(()),
    }
}

fn replicate_from(
    log: &Arc<Mutex<Log>>,
    stream: &mut TcpStream,
    from_index: u64,
) -> anyhow::Result<()> {
    // Phase 1: push the current snapshot.
    let mut next = from_index;
    next = push_range(log, stream, next)?;

    // Phase 2: keep the connection open and tail-push new entries as they
    // appear in the log. The follower disconnects on shutdown, which
    // cleanly ends this loop. v0.1 polls; phase 7 will switch to a
    // commit-event subscription so pushes are zero-delay.
    log::info!("peer: tail-replicating from {}", next);
    loop {
        thread::sleep(Duration::from_millis(150));
        next = push_range(log, stream, next)?;
        // Also send a heartbeat on each tail tick so the follower's read
        // timeout never trips on an idle leader.
        let (idx, hash) = log.lock().unwrap().latest();
        write_frame(stream, &PeerFrame::Heartbeat { latest_index: idx, latest_hash: hash })?;
    }
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
) -> anyhow::Result<()> {
    let mut lg = log.lock().unwrap();
    let (latest_index, latest_hash) = lg.latest();

    if entry.index <= latest_index {
        // Already have it. Verify byte-for-byte that what we have on disk
        // matches what the leader is sending — that's the divergence
        // detection design §4 calls for.
        match lg.read(entry.index)? {
            Some(local) => {
                if local.hash != entry.hash {
                    log_warn(&format!(
                        "peer: DIVERGENCE at index {}: local hash {} ≠ leader hash {}",
                        entry.index,
                        hex::encode(local.hash),
                        hex::encode(entry.hash),
                    ));
                    anyhow::bail!("hash divergence at index {}", entry.index);
                }
            }
            None => log_warn(&format!("peer: missing entry {} for verify", entry.index)),
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
        // Should never happen — both sides hash the same frame layout.
        anyhow::bail!(
            "peer: post-append hash mismatch (we got {}, leader got {})",
            hex::encode(appended.hash),
            hex::encode(entry.hash),
        );
    }
    log::info!("peer: applied entry {} hash={}", appended.index, hex::encode(&appended.hash[..6]));
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

fn log_warn(msg: &str) {
    log::warn!("{}", msg);
}

#[allow(dead_code)]
pub fn _unused(_x: PathBuf) {}
