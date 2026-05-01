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
use std::path::{Path, PathBuf};
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
    /// Future: subscribe to commit events. Not in v0.1's first cut.
    Subscribe,
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
    Error {
        message: String,
    },
}

#[derive(Debug, Serialize, Deserialize)]
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
mod serde_bytes_array {
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

pub struct Server {
    pub sock_path: PathBuf,
    pub log: Arc<Mutex<Log>>,
}

impl Server {
    pub fn new(sock_path: impl Into<PathBuf>, log: Log) -> Self {
        Self {
            sock_path: sock_path.into(),
            log: Arc::new(Mutex::new(log)),
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
            thread::spawn(move || {
                if let Err(e) = handle_client(stream, log) {
                    log::warn!("ipc: client error: {}", e);
                }
            });
        }
        Ok(())
    }
}

fn handle_client(mut stream: UnixStream, log: Arc<Mutex<Log>>) -> anyhow::Result<()> {
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
        let resp = process(&log, req);
        write_response(&mut stream, &resp)?;
    }
}

fn process(log: &Arc<Mutex<Log>>, req: Request) -> Response {
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
                Ok(e) => Response::Appended {
                    index: e.index,
                    hash: e.hash,
                },
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
            message: "subscribe: not implemented in v0.1".into(),
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

/// One-shot client used by the CLI shim and Python: open, send one
/// request, read one response, close.
pub fn call(sock_path: impl AsRef<Path>, req: &Request) -> anyhow::Result<Response> {
    let mut stream = UnixStream::connect(sock_path)?;
    let body = rmp_serde::to_vec_named(req)?;
    let len = (body.len() as u32).to_be_bytes();
    stream.write_all(&len)?;
    stream.write_all(&body)?;
    stream.flush()?;
    let resp_bytes = read_frame(&mut stream)?.ok_or_else(|| anyhow::anyhow!("daemon hung up"))?;
    Ok(rmp_serde::from_slice(&resp_bytes)?)
}
