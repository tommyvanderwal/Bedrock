//! Unix-domain-socket IPC between bedrock-rust and the Python
//! orchestrator / CLI helpers.
//!
//! Post-rqlite (D-22): the verbs are intentionally tiny.
//! `Status` exposes the daemon's own view of its role + witness
//! state for diagnostic reads; `PeerStatus` returns the per-link
//! TCP-heartbeat surface from `peer.rs` so the CLI can answer
//! "is the peer still talking to me?"
//!
//! Removed in the rewrite: `Append` / `Read` / `Subscribe` —
//! state mutations go directly to rqlite via the Python
//! `bedrock_state` module; subscription is rqlite watch via
//! `view_builder.build_snapshot`. There is no longer a hash-chained
//! log on disk under bedrock-rust.
//!
//! Wire format: length-prefixed MessagePack frames over a Unix
//! socket bound at `/run/bedrock-rust.sock` (mode 0600, root-owned).

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::thread;

const FRAME_LEN_BYTES: usize = 4;
const MAX_FRAME_BYTES: usize = 64 * 1024;

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Request {
    /// Return basic daemon state: socket path, peer count, etc.
    Status,
    /// Return the per-link PeerStatus snapshot from peer.rs.
    PeerStatus,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Response {
    Ok {},
    Status {
        /// Sub-second wall-clock so callers can sanity-check the
        /// liveness of the daemon itself.
        now_ms: u64,
    },
    PeerStatus {
        links: Vec<crate::peer::PeerLinkInfo>,
    },
    Error { message: String },
}

pub struct Server {
    pub sock_path: PathBuf,
    pub registry: crate::peer::PeerRegistry,
}

impl Server {
    pub fn new(
        sock_path: impl Into<PathBuf>,
        registry: crate::peer::PeerRegistry,
    ) -> Self {
        Self {
            sock_path: sock_path.into(),
            registry,
        }
    }

    /// Bind, listen, accept clients in their own threads. Blocks forever.
    pub fn serve(&self) -> anyhow::Result<()> {
        if self.sock_path.exists() {
            std::fs::remove_file(&self.sock_path)?;
        }
        if let Some(parent) = self.sock_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let listener = UnixListener::bind(&self.sock_path)?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&self.sock_path, std::fs::Permissions::from_mode(0o600))?;
        log::info!("ipc: listening on {}", self.sock_path.display());

        for stream in listener.incoming() {
            let stream = stream?;
            let registry = self.registry.clone();
            thread::spawn(move || {
                if let Err(e) = handle_client(stream, registry) {
                    log::warn!("ipc: client error: {}", e);
                }
            });
        }
        Ok(())
    }
}

fn handle_client(
    mut stream: UnixStream,
    registry: crate::peer::PeerRegistry,
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
        let resp = process(&registry, req);
        write_response(&mut stream, &resp)?;
    }
}

fn process(
    registry: &crate::peer::PeerRegistry,
    req: Request,
) -> Response {
    match req {
        Request::Status => {
            use std::time::{SystemTime, UNIX_EPOCH};
            let now_ms = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0);
            Response::Status { now_ms }
        }
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

