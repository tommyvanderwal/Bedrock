//! Bedrock cluster-protocol daemon.
//!
//! v0.1 scope: append-only hash-chained log + an Echo witness client.
//! Designed per `docs/cluster-protocol-design.md` and the v1 plan.

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

mod ipc;
mod log_store;
mod payload;
mod peer;
mod witness;

/// Default location for the cluster log on disk.
const DEFAULT_LOG_DIR: &str = "/var/lib/bedrock/log";
/// Default location for the IPC socket.
const DEFAULT_IPC_SOCK: &str = "/run/bedrock-rust.sock";

#[derive(Parser)]
#[command(name = "bedrock-rust", version, about = "Bedrock cluster-protocol daemon")]
struct Cli {
    /// Path to the log directory (segment files live here).
    #[arg(long, global = true, default_value = DEFAULT_LOG_DIR)]
    log_dir: PathBuf,
    /// Path to the IPC Unix socket.
    #[arg(long, global = true, default_value = DEFAULT_IPC_SOCK)]
    ipc_sock: PathBuf,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the long-lived daemon: log + IPC + (later) lease loop + peer.
    Daemon {
        /// Peer addresses to connect to. Repeat for multiple paths
        /// (per design §7: ≥1 cable, ideally 2 — RJ45 + USB4 — for
        /// orthogonality. The system stays operational with as few as
        /// one working path).
        #[arg(long)]
        peer: Vec<String>,
        /// TCP listen addresses for incoming peer connections. Repeat
        /// for multiple paths. Defaults to a single 0.0.0.0:8200.
        #[arg(long)]
        peer_listen: Vec<String>,
        /// This node's role at startup. Phase 8 auto-elects from
        /// witness state when set to `auto`.
        #[arg(long, value_enum, default_value_t = peer::Role::Standalone)]
        role: peer::Role,
        /// 32-byte cluster key, hex (peer auth + witness AEAD).
        #[arg(long)]
        cluster_key: Option<String>,
        #[arg(long)]
        cluster_key_file: Option<PathBuf>,
        /// Optional witness host (for the lease loop).
        #[arg(long)]
        witness_host: Option<String>,
        #[arg(long, default_value_t = 12321)]
        witness_port: u16,
        /// Witness X25519 pubkey for pinning.
        #[arg(long)]
        witness_pubkey: Option<String>,
        #[arg(long)]
        witness_pubkey_file: Option<PathBuf>,
        /// This node's sender_id (0..0xFE).
        #[arg(long, default_value_t = 0)]
        sender_id: u8,
        /// The peer's sender_id, used to drive witness-based leader
        /// election (Phase 8). Omit for standalone / single-node.
        #[arg(long)]
        peer_sender_id: Option<u8>,
        /// Lease TTL in milliseconds; the leader is fenced if it can't
        /// renew within this window. Direct-cable default 5000ms.
        #[arg(long, default_value_t = 5_000)]
        lease_ttl_ms: u64,
        /// Heartbeat interval in milliseconds.
        #[arg(long, default_value_t = 1_000)]
        heartbeat_ms: u64,
        /// Cluster interfaces to bring down on self-fence (comma list).
        /// Optional in dev — empty means "log + exit, don't touch network".
        #[arg(long, default_value = "")]
        fence_interfaces: String,
    },
    /// Log management subcommands.
    Log {
        #[command(subcommand)]
        op: LogCmd,
    },
    /// Echo witness subcommands.
    Witness {
        #[command(subcommand)]
        op: WitnessCmd,
    },
}

#[derive(Subcommand)]
enum LogCmd {
    /// Initialise a fresh log with the bootstrap "Hello World!" entry.
    Init {
        /// Cluster UUID. Defaults to a fresh v4 UUID.
        #[arg(long)]
        cluster_uuid: Option<String>,
    },
    /// Append a payload entry. Reads payload from stdin if not given.
    Append {
        /// Inline payload (UTF-8). Mutually exclusive with --file and stdin.
        #[arg(long)]
        text: Option<String>,
        /// File path to read the payload from.
        #[arg(long)]
        file: Option<PathBuf>,
    },
    /// Show entries from `from` to `to` (inclusive). Defaults: full log.
    Show {
        #[arg(long, default_value_t = 1)]
        from: u64,
        #[arg(long)]
        to: Option<u64>,
    },
    /// Walk every entry in the log and verify the hash chain. Exits non-zero
    /// at the first divergence.
    Verify,
}

#[derive(Subcommand)]
enum WitnessCmd {
    /// Send a single HEARTBEAT carrying the current log tail and print the witness's reply.
    Heartbeat {
        /// Witness host (IPv4 or hostname). Default: localhost.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        /// Witness UDP port (Echo default).
        #[arg(long, default_value_t = 12321)]
        port: u16,
        /// Cluster key hex (64 chars / 32 bytes). Use `--cluster-key-file` for prod.
        #[arg(long)]
        cluster_key: Option<String>,
        /// File path to the cluster_key (32 raw bytes).
        #[arg(long)]
        cluster_key_file: Option<PathBuf>,
        /// Witness X25519 public key, hex (64 chars / 32 bytes).
        #[arg(long)]
        witness_pubkey: Option<String>,
        /// File path to the witness_pubkey (32 raw bytes).
        #[arg(long)]
        witness_pubkey_file: Option<PathBuf>,
        /// This node's sender_id (0..0xFE).
        #[arg(long, default_value_t = 0)]
        sender_id: u8,
        /// Query target_id (0..0xFE for STATUS_DETAIL on that peer; 0xFF for STATUS_LIST).
        #[arg(long, default_value_t = 0xFF)]
        query_target: u8,
    },
}

fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let cli = Cli::parse();

    match cli.cmd {
        Cmd::Daemon {
            peer: peer_addr,
            peer_listen,
            role,
            cluster_key,
            cluster_key_file,
            witness_host,
            witness_port,
            witness_pubkey,
            witness_pubkey_file,
            sender_id,
            peer_sender_id,
            lease_ttl_ms,
            heartbeat_ms,
            fence_interfaces,
        } => run_daemon(
            cli.log_dir,
            cli.ipc_sock,
            peer_addr,
            peer_listen,
            role,
            cluster_key,
            cluster_key_file,
            witness_host,
            witness_port,
            witness_pubkey,
            witness_pubkey_file,
            sender_id,
            peer_sender_id,
            lease_ttl_ms,
            heartbeat_ms,
            fence_interfaces,
        ),
        Cmd::Log { op } => match op {
            LogCmd::Init { cluster_uuid } => {
                let uuid = cluster_uuid
                    .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
                let log = log_store::Log::init(&cli.log_dir, &uuid)
                    .context("log init failed")?;
                let (idx, hash) = log.latest();
                println!("Initialised log at {}", cli.log_dir.display());
                println!("  cluster_uuid: {}", uuid);
                println!("  index: {}, hash: {}", idx, hex::encode(hash));
                Ok(())
            }
            LogCmd::Append { text, file } => {
                let payload = match (text, file) {
                    (Some(t), None) => t.into_bytes(),
                    (None, Some(p)) => std::fs::read(&p)
                        .with_context(|| format!("reading {}", p.display()))?,
                    (None, None) => {
                        use std::io::Read;
                        let mut buf = Vec::new();
                        std::io::stdin().read_to_end(&mut buf)?;
                        buf
                    }
                    _ => anyhow::bail!("--text and --file are mutually exclusive"),
                };
                let mut log = log_store::Log::open(&cli.log_dir).context("log open failed")?;
                let entry = log.append(payload::Kind::Opaque, &payload)
                    .context("append failed")?;
                println!("appended index={} hash={}", entry.index, hex::encode(entry.hash));
                Ok(())
            }
            LogCmd::Show { from, to } => {
                let log = log_store::Log::open(&cli.log_dir).context("log open failed")?;
                let to = to.unwrap_or_else(|| log.latest().0);
                for idx in from..=to {
                    match log.read(idx) {
                        Ok(Some(e)) => {
                            println!(
                                "[{}] kind={:?} prev={} hash={} payload={} bytes",
                                e.index,
                                payload::Kind::from_u8(e.kind),
                                hex::encode(&e.prev_hash[..6]),
                                hex::encode(&e.hash[..6]),
                                e.payload.len()
                            );
                            // Print the payload as text only if it's
                            // free-form — typed (msgpack) entries are
                            // best inspected via the Python view-builder.
                            if matches!(payload::Kind::from_u8(e.kind), Some(payload::Kind::Opaque)) {
                                if let Ok(s) = std::str::from_utf8(&e.payload) {
                                    if !s.is_empty() {
                                        println!("    text: {}", s);
                                    }
                                }
                            }
                        }
                        Ok(None) => {
                            eprintln!("(index {} not found — log truncated?)", idx);
                            break;
                        }
                        Err(e) => {
                            eprintln!("error reading index {}: {}", idx, e);
                            std::process::exit(1);
                        }
                    }
                }
                Ok(())
            }
            LogCmd::Verify => {
                let log = log_store::Log::open(&cli.log_dir).context("log open failed")?;
                match log.verify() {
                    Ok(n) => {
                        println!("OK — {} entries verified, hash chain intact", n);
                        Ok(())
                    }
                    Err(e) => {
                        eprintln!("DIVERGENCE: {}", e);
                        std::process::exit(2);
                    }
                }
            }
        },
        Cmd::Witness { op } => match op {
            WitnessCmd::Heartbeat {
                host,
                port,
                cluster_key,
                cluster_key_file,
                witness_pubkey,
                witness_pubkey_file,
                sender_id,
                query_target,
            } => {
                let cluster_key = read_key32(cluster_key, cluster_key_file, "cluster_key")?;
                let witness_pubkey = read_key32(witness_pubkey, witness_pubkey_file, "witness_pubkey")?;
                let log = log_store::Log::open(&cli.log_dir).context("log open failed")?;
                let (idx, hash) = log.latest();
                witness::heartbeat_once(
                    &host, port, &cluster_key, &witness_pubkey,
                    sender_id, query_target, idx, hash,
                )?;
                Ok(())
            }
        },
    }
}

#[allow(clippy::too_many_arguments)]
fn run_daemon(
    log_dir: PathBuf,
    ipc_sock: PathBuf,
    peer_addr: Vec<String>,
    peer_listen: Vec<String>,
    role: peer::Role,
    cluster_key: Option<String>,
    cluster_key_file: Option<PathBuf>,
    witness_host: Option<String>,
    witness_port: u16,
    witness_pubkey: Option<String>,
    witness_pubkey_file: Option<PathBuf>,
    sender_id: u8,
    peer_sender_id: Option<u8>,
    lease_ttl_ms: u64,
    heartbeat_ms: u64,
    fence_interfaces: String,
) -> Result<()> {
    let log = log_store::Log::open(&log_dir).context("log open failed")?;
    let server = ipc::Server::new(ipc_sock.clone(), log);
    let log_handle = std::sync::Arc::clone(&server.log);

    // Peer transport (Phases 3 + 7). Default to one listener if the
    // operator didn't specify any.
    let listen_addrs = if peer_listen.is_empty() {
        vec!["0.0.0.0:8200".to_string()]
    } else {
        peer_listen
    };
    let _peer = peer::start(peer::Config {
        log: std::sync::Arc::clone(&log_handle),
        listen_addrs,
        connect_to: peer_addr,
        role,
    })?;

    // Lease loop (Phase 4) — only spins up if we have a witness configured.
    let lease_handle = if let Some(host) = witness_host {
        let cluster_key = read_key32(cluster_key, cluster_key_file, "cluster-key")?;
        let witness_pubkey = read_key32(witness_pubkey, witness_pubkey_file, "witness-pubkey")?;
        let cfg = witness::LeaseConfig {
            host,
            port: witness_port,
            cluster_key,
            witness_pubkey,
            sender_id,
            peer_sender_id,
            ttl_ms: lease_ttl_ms,
            heartbeat_ms,
            fence_interfaces: fence_interfaces
                .split(',')
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string())
                .collect(),
        };
        Some(witness::start_lease_loop(cfg, std::sync::Arc::clone(&log_handle)))
    } else {
        log::info!("daemon: no --witness-host; lease loop disabled (standalone mode)");
        None
    };

    let _lease = lease_handle;

    log::info!("bedrock-rust daemon: log_dir={} ipc={}",
               log_dir.display(), ipc_sock.display());
    server.serve()
}

fn read_key32(
    inline: Option<String>,
    file: Option<PathBuf>,
    name: &str,
) -> Result<[u8; 32]> {
    let bytes = match (inline, file) {
        (Some(h), None) => hex::decode(h.trim()).with_context(|| format!("{name}: bad hex"))?,
        (None, Some(p)) => std::fs::read(&p)
            .with_context(|| format!("reading {name} file {}", p.display()))?,
        (None, None) => anyhow::bail!("{name}: provide --{name} or --{name}-file"),
        _ => anyhow::bail!("{name}: --{name} and --{name}-file are mutually exclusive"),
    };
    bytes
        .try_into()
        .map_err(|_| anyhow::anyhow!("{name}: must be exactly 32 bytes"))
}
