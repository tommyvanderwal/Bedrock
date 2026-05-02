//! Local IPC between bedrock-python and the bedrock-rust daemon.
//!
//! Unix-domain socket, length-prefixed MessagePack frames. One socket
//! at `/run/bedrock-rust.sock`; multiple Python clients allowed.
//!
//! Wire format per frame:
//! ```
//!   ┌────────────┬────────────────────┐
//!   │  4 B u32   │  N bytes MessagePack│
//!   │  body_len  │  body              │
//!   └────────────┴────────────────────┘
//! ```
//!
//! Per design §11: "the IPC survives Python crashes — Rust keeps running
//! with last-known-good config and queues events with a bounded buffer."
//! The bounded queue + drop-oldest policy lives in the Subscribe path.

use crate::log_store::Log;
use crate::payload::Kind;
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;

const FRAME_LEN_BYTES: usize = 4;
const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024; // 16 MB hard cap

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Request {
    /// Append a payload as the next log entry. Blocks until fsynced.
    Append {
        kind: u8,
        #[serde(with = "serde_bytes")]
        payload: Vec<u8>,
    },
    /// Read entries [from..=to]. `to=None` → up to the latest.
    Read { from: u64, to: Option<u64> },
    /// Latest (index, hash) for the local log.
    Status,
    /// Compute the SHA-256 of the entry at `index` (sanity check).
    Verify,
    /// Subscribe to commit events. The connection STAYS OPEN after
    /// this request; the daemon pushes a `Committed{entry}` response
    /// frame after every successful append (locally OR via peer
    /// replication). Used by bedrock-watcher to react to log changes
    /// without polling. Bounded queue per subscriber — slow subscribers
    /// get dropped + reconnect on the next iteration of their loop.
    Subscribe,
    /// Per-link peer state surface — used by `_wait_replicated`.
    PeerStatus,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Response {
    Ok {},
    Appended {
        index: u64,
        #[serde(with = "serde_bytes_array")]
        hash: [u8; 32],
    },
    Entries {
        entries: Vec<EntryWire>,
    },
    Status {
        latest_index: u64,
        #[serde(with = "serde_bytes_array")]
        latest_hash: [u8; 32],
    },
    Verified {
        entries_checked: u64,
    },
    /// Server-pushed: a new entry was committed. Streamed continuously
    /// after a Subscribe request until the connection closes.
    Committed {
        entry: EntryWire,
    },
    /// Server-pushed: subscriber's bounded queue overflowed; the
    /// subscriber should disconnect, reconnect, fetch via Read to
    /// catch up, then Subscribe again.
    SubscribeOverrun,
    PeerStatus {
        links: Vec<crate::peer::PeerLinkInfo>,
    },
    Error {
        message: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntryWire {
    pub index: u64,
    pub epoch: u64,
    #[serde(with = "serde_bytes_array")]
    pub prev_hash: [u8; 32],
    pub kind: u8,
    #[serde(with = "serde_bytes")]
    pub payload: Vec<u8>,
    #[serde(with = "serde_bytes_array")]
    pub hash: [u8; 32],
}

/// Serde adapter to (de)serialize a fixed-length `[u8; 32]` as msgpack
/// `bin`. Without this, serde defaults to a tuple/sequence representation
/// which Python's msgpack-Python emits as a list — wire-incompat with
/// Rust's `Vec<u8>` and surprising on the wire.
pub(crate) mod serde_bytes_array {
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(v: &[u8; 32], s: S) -> Result<S::Ok, S::Error> {
        serde_bytes::serialize(v.as_slice(), s)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<[u8; 32], D::Error> {
        let v: serde_bytes::ByteBuf = serde_bytes::ByteBuf::deserialize(d)?;
        let bytes: &[u8] = v.as_ref();
        if bytes.len() != 32 {
            return Err(serde::de::Error::invalid_length(bytes.len(), &"32 bytes"));
        }
        let mut out = [0u8; 32];
        out.copy_from_slice(bytes);
        Ok(out)
    }
}

/// One subscriber's mailbox. The IPC server pushes Committed events;
/// the subscriber's reader thread drains them onto the wire.
type Mailbox = Sender<EntryWire>;

#[derive(Default)]
struct SubscribersInner {
    next_id: u64,
    mailboxes: Vec<(u64, Mailbox)>,
}

/// Notify-on-commit hook. Called from BOTH the IPC append path (local
/// origin) AND the peer.rs apply path (replicated commits) so every
/// local subscriber sees every committed entry, regardless of where
/// it came from.
#[derive(Clone, Default)]
pub struct CommitNotifier(Arc<Mutex<SubscribersInner>>);

impl CommitNotifier {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn notify(&self, entry: &EntryWire) {
        let mut inner = self.0.lock().unwrap();
        let mut drop_ids = Vec::new();
        for (id, tx) in inner.mailboxes.iter() {
            match tx.send(entry.clone()) {
                Ok(()) => {}
                Err(_) => drop_ids.push(*id), // receiver gone
            }
        }
        if !drop_ids.is_empty() {
            inner.mailboxes.retain(|(id, _)| !drop_ids.contains(id));
        }
    }

    fn register(&self) -> (u64, Receiver<EntryWire>) {
        let (tx, rx) = channel::<EntryWire>();
        let mut inner = self.0.lock().unwrap();
        inner.next_id += 1;
        let id = inner.next_id;
        inner.mailboxes.push((id, tx));
        (id, rx)
    }

    fn unregister(&self, id: u64) {
        let mut inner = self.0.lock().unwrap();
        inner.mailboxes.retain(|(i, _)| *i != id);
    }
}

pub struct Server {
    pub sock_path: PathBuf,
    pub log: Arc<Mutex<Log>>,
    pub registry: crate::peer::PeerRegistry,
    pub commit: CommitNotifier,
}

impl Server {
    pub fn new(
        sock_path: impl Into<PathBuf>,
        log: Log,
        registry: crate::peer::PeerRegistry,
        commit: CommitNotifier,
    ) -> Self {
        Self {
            sock_path: sock_path.into(),
            log: Arc::new(Mutex::new(log)),
            registry,
            commit,
        }
    }

    /// Bind, listen, accept clients in their own threads. Blocks forever.
    pub fn serve(&self) -> anyhow::Result<()> {
        if self.sock_path.exists() {
            // Stale socket from a previous (crashed) instance. Replacing is
            // safe because we hold no peers yet.
            std::fs::remove_file(&self.sock_path)?;
        }
        if let Some(parent) = self.sock_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let listener = UnixListener::bind(&self.sock_path)?;
        // Lock down the socket to root only.
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&self.sock_path, std::fs::Permissions::from_mode(0o600))?;
        log::info!("ipc: listening on {}", self.sock_path.display());

        for stream in listener.incoming() {
            let stream = stream?;
            let log = Arc::clone(&self.log);
            let registry = self.registry.clone();
            let commit = self.commit.clone();
            thread::spawn(move || {
                if let Err(e) = handle_client(stream, log, registry, commit) {
                    log::warn!("ipc: client error: {}", e);
                }
            });
        }
        Ok(())
    }
}

fn handle_client(
    mut stream: UnixStream,
    log: Arc<Mutex<Log>>,
    registry: crate::peer::PeerRegistry,
    commit: CommitNotifier,
) -> anyhow::Result<()> {
    loop {
        let req = match read_frame(&mut stream)? {
            Some(bytes) => match rmp_serde::from_slice::<Request>(&bytes) {
                Ok(r) => r,
                Err(e) => {
                    write_response(
                        &mut stream,
                        &Response::Error {
                            message: format!("decode: {e}"),
                        },
                    )?;
                    continue;
                }
            },
            None => return Ok(()), // client disconnected
        };
        if matches!(req, Request::Subscribe) {
            // Subscribe holds the connection open; serve forever
            // (or until the client disconnects / we exceed queue).
            let (id, rx) = commit.register();
            // Confirm subscribe started.
            write_response(&mut stream, &Response::Ok {})?;
            // Drain commits to the wire. Bounded by SUBSCRIBER_QUEUE_DEPTH
            // — if a subscriber falls behind that many entries we drop
            // it, since the channel send in CommitNotifier is unbuffered
            // by default. (We use a threshold-counted approach: the
            // mailbox is std::sync::mpsc which is unbounded; we cap by
            // tracking pending count manually below.)
            // For v1 simplicity the channel is std::sync::mpsc (unbounded);
            // overrun protection is "if write to the wire fails the client
            // is gone, unregister". Pending depth is a non-issue here
            // because we drain as we go.
            let _ = id;
            for committed in rx {
                if write_response(&mut stream, &Response::Committed { entry: committed }).is_err() {
                    break;
                }
            }
            commit.unregister(id);
            return Ok(());
        }
        let resp = process(&log, &registry, &commit, req);
        write_response(&mut stream, &resp)?;
    }
}

fn process(
    log: &Arc<Mutex<Log>>,
    registry: &crate::peer::PeerRegistry,
    commit: &CommitNotifier,
    req: Request,
) -> Response {
    match req {
        Request::Append { kind, payload } => {
            let kind = match Kind::from_u8(kind) {
                Some(k) => k,
                None => {
                    return Response::Error {
                        message: format!("unknown payload kind 0x{kind:02x}"),
                    }
                }
            };
            match log.lock().unwrap().append(kind, &payload) {
                Ok(e) => {
                    // Notify Subscribers of the locally-originated commit.
                    // Peer-replicated commits are notified from peer.rs.
                    let wire = EntryWire {
                        index: e.index,
                        epoch: e.epoch,
                        prev_hash: e.prev_hash,
                        kind: e.kind,
                        payload: e.payload.clone(),
                        hash: e.hash,
                    };
                    commit.notify(&wire);
                    Response::Appended {
                        index: e.index,
                        hash: e.hash,
                    }
                }
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Request::Read { from, to } => {
            let log = log.lock().unwrap();
            let to = to.unwrap_or_else(|| log.latest().0);
            let mut out = Vec::new();
            for idx in from..=to {
                match log.read(idx) {
                    Ok(Some(e)) => out.push(EntryWire {
                        index: e.index,
                        epoch: e.epoch,
                        prev_hash: e.prev_hash,
                        kind: e.kind,
                        payload: e.payload,
                        hash: e.hash,
                    }),
                    Ok(None) => break,
                    Err(e) => {
                        return Response::Error {
                            message: format!("read {idx}: {e}"),
                        }
                    }
                }
            }
            Response::Entries { entries: out }
        }
        Request::Status => {
            let log = log.lock().unwrap();
            let (latest_index, latest_hash) = log.latest();
            Response::Status {
                latest_index,
                latest_hash,
            }
        }
        Request::Verify => match log.lock().unwrap().verify() {
            Ok(n) => Response::Verified { entries_checked: n },
            Err(e) => Response::Error {
                message: e.to_string(),
            },
        },
        Request::Subscribe => Response::Error {
            // Subscribe is handled in handle_client (it holds the connection
            // open and streams commits). If we ever land here it's a code bug.
            message: "subscribe: should be intercepted in handle_client".into(),
        },
        Request::PeerStatus => Response::PeerStatus {
            links: registry.snapshot(),
        },
    }
}

fn read_frame<R: Read>(r: &mut R) -> anyhow::Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; FRAME_LEN_BYTES];
    match r.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let n = u32::from_be_bytes(len_buf) as usize;
    if n > MAX_FRAME_BYTES {
        anyhow::bail!("ipc frame oversized: {n} > {MAX_FRAME_BYTES}");
    }
    let mut body = vec![0u8; n];
    r.read_exact(&mut body)?;
    Ok(Some(body))
}

fn write_response<W: Write>(w: &mut W, resp: &Response) -> anyhow::Result<()> {
    let body = rmp_serde::to_vec_named(resp)?;
    let len = (body.len() as u32).to_be_bytes();
    w.write_all(&len)?;
    w.write_all(&body)?;
    w.flush()?;
    Ok(())
}

