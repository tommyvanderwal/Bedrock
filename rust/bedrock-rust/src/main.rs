//! Bedrock cluster-protocol daemon — post-rewrite (D-22) edition.
//!
//! Scope after the rqlite migration:
//!   * Witness lease loop + `compute_election` + self-fence
//!   * Peer-liveness TCP heartbeat (NOT log replication)
//!   * Status / role IPC for Python orchestrator + CLI
//!
//! What we used to do (and don't anymore):
//!   * Hash-chained log store → replaced by rqlite (see
//!     docs/post-alpha-rewrite-notes.md D-01..D-22)
//!   * Log replication over multi-link TCP → rqlite Raft does this
//!   * Append / Read / Subscribe IPC verbs → replaced by direct
//!     SQL writes via installer/lib/bedrock_state.py and reads via
//!     installer/lib/view_builder.py
//!
//! What stays in Rust (the actual Bedrock value-add):
//!   * Witness-aware election with weighted voting (10/node +
//!     1/witness) — no off-the-shelf consensus speaks this.
//!   * Self-fence: bring NICs down on lease loss, write
//!     /tmp/bedrock-rust.fence for the Python fence-responder.
//!   * Peer TCP keepalive — fast "is the peer alive" signal,
//!     independent of rqlite's HTTP health.

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

mod config;
mod ipc;
mod peer;
mod witness;

/// Default location for the IPC socket.
const DEFAULT_IPC_SOCK: &str = "/run/bedrock-rust.sock";

#[derive(Parser)]
#[command(name = "bedrock-rust", version, about = "Bedrock cluster-protocol daemon")]
struct Cli {
    /// Path to the IPC Unix socket.
    #[arg(long, global = true, default_value = DEFAULT_IPC_SOCK)]
    ipc_sock: PathBuf,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the long-lived daemon: IPC + peer heartbeat + witness lease.
    Daemon {
        /// Read settings from a TOML config file. Any CLI flag still
        /// overrides the file. The systemd unit uses
        /// `--config /etc/bedrock/daemon.toml`.
        #[arg(long)]
        config: Option<PathBuf>,
        /// Peer addresses to connect to. Repeat for multiple paths
        /// (per design: ≥1 cable, ideally 2 for orthogonality).
        #[arg(long)]
        peer: Vec<String>,
        /// TCP listen addresses for incoming peer heartbeats. Repeat
        /// for multiple paths. Defaults to a single 0.0.0.0:8200.
        #[arg(long)]
        peer_listen: Vec<String>,
        /// This node's role at startup. Election overrides once
        /// witness data is in.
        #[arg(long, value_enum, default_value_t = peer::Role::Standalone)]
        role: peer::Role,
        /// 32-byte cluster key, hex (peer auth + witness AEAD).
        #[arg(long)]
        cluster_key: Option<String>,
        #[arg(long)]
        cluster_key_file: Option<PathBuf>,
        /// Optional witness host (legacy single-witness flag — prefer
        /// witnesses list in daemon.toml).
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
    /// Echo witness subcommands.
    Witness {
        #[command(subcommand)]
        op: WitnessCmd,
    },
}

#[derive(Subcommand)]
enum WitnessCmd {
    /// Send a single HEARTBEAT and print the witness's reply.
    /// Useful for diagnosing a witness from the CLI without spinning
    /// up the full daemon.
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
            config,
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
            lease_ttl_ms,
            heartbeat_ms,
            fence_interfaces,
        } => run_daemon(
            config,
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
            lease_ttl_ms,
            heartbeat_ms,
            fence_interfaces,
        ),
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
                // Post-rewrite (D-16): the witness's per-node payload
                // morphs from log-state echo to DRBD-state echo
                // (uuid + generation + last_man_standing_marker). For
                // the CLI heartbeat invocation we send zeros — the
                // call is for diagnostics, not for actually steering
                // the election from the command line.
                witness::heartbeat_once(
                    &host, port, &cluster_key, &witness_pubkey,
                    sender_id, query_target, 0, [0u8; 32],
                )?;
                Ok(())
            }
        },
    }
}

#[allow(clippy::too_many_arguments)]
fn run_daemon(
    config_path: Option<PathBuf>,
    ipc_sock: PathBuf,
    cli_peer: Vec<String>,
    cli_peer_listen: Vec<String>,
    cli_role: peer::Role,
    cli_cluster_key: Option<String>,
    cli_cluster_key_file: Option<PathBuf>,
    cli_witness_host: Option<String>,
    cli_witness_port: u16,
    cli_witness_pubkey: Option<String>,
    cli_witness_pubkey_file: Option<PathBuf>,
    cli_sender_id: u8,
    cli_lease_ttl_ms: u64,
    cli_heartbeat_ms: u64,
    cli_fence_interfaces: String,
) -> Result<()> {
    let cfg_file = if let Some(p) = config_path.as_ref() {
        Some(config::DaemonConfig::load(p).with_context(|| format!("read {}", p.display()))?)
    } else {
        None
    };

    let ipc_sock = cfg_file.as_ref()
        .and_then(|c| c.ipc_sock.clone())
        .filter(|_| ipc_sock == PathBuf::from(DEFAULT_IPC_SOCK))
        .unwrap_or(ipc_sock);

    let peer_addr = if !cli_peer.is_empty() {
        cli_peer
    } else {
        cfg_file.as_ref().map(|c| c.peer.clone()).unwrap_or_default()
    };
    let peer_listen = if !cli_peer_listen.is_empty() {
        cli_peer_listen
    } else {
        cfg_file.as_ref().map(|c| c.peer_listen.clone()).unwrap_or_default()
    };
    let sender_id = cfg_file.as_ref()
        .and_then(|c| c.sender_id)
        .filter(|_| cli_sender_id == 0)
        .unwrap_or(cli_sender_id);
    let peer_sender_ids: Vec<u8> = cfg_file
        .as_ref()
        .map(|c| c.peer_sender_ids.clone())
        .unwrap_or_default();
    let lease_ttl_ms = cfg_file.as_ref()
        .and_then(|c| c.lease_ttl_ms)
        .filter(|_| cli_lease_ttl_ms == 5_000)
        .unwrap_or(cli_lease_ttl_ms);
    let heartbeat_ms = cfg_file.as_ref()
        .and_then(|c| c.heartbeat_ms)
        .filter(|_| cli_heartbeat_ms == 1_000)
        .unwrap_or(cli_heartbeat_ms);
    let fence_interfaces: Vec<String> = if cli_fence_interfaces.is_empty() {
        cfg_file.as_ref().map(|c| c.fence_interfaces.clone()).unwrap_or_default()
    } else {
        cli_fence_interfaces.split(',').filter(|s| !s.is_empty()).map(|s| s.to_string()).collect()
    };

    // Shared between IPC (PeerStatus surface) and peer transport
    // (per-link bookkeeping). Same registry instance for both so
    // status reads reflect live peer state.
    let registry = peer::new_peer_registry();
    let server = ipc::Server::new(ipc_sock.clone(), registry.clone());

    let listen_addrs = if peer_listen.is_empty() {
        vec!["0.0.0.0:8200".to_string()]
    } else {
        peer_listen
    };
    // Resolve role: CLI value wins if it's anything other than the
    // default Standalone; otherwise use daemon.toml.
    let role = if cli_role != peer::Role::Standalone {
        cli_role
    } else if let Some(r) = cfg_file.as_ref().and_then(|c| c.role.as_ref()) {
        match r.as_str() {
            "leader" => peer::Role::Leader,
            "follower" => peer::Role::Follower,
            _ => peer::Role::Standalone,
        }
    } else {
        cli_role
    };
    log::info!("role: {:?}", role);
    let peer_liveness = peer::new_peer_liveness();
    let _peer = peer::start(peer::Config {
        listen_addrs,
        connect_to: peer_addr,
        role,
        liveness: std::sync::Arc::clone(&peer_liveness),
        registry: registry.clone(),
    })?;

    // Witness configuration. Resolve from the config file when present;
    // fall back to the legacy single-witness CLI flags otherwise.
    let mut witnesses: Vec<witness::WitnessSpec> = Vec::new();
    if let Some(c) = cfg_file.as_ref() {
        let cluster_key = read_key32_from_cfg(c)?;
        for w in &c.witness {
            witnesses.push(witness::WitnessSpec {
                id: w.id.clone(),
                host: w.host.clone(),
                port: w.port,
                cluster_key,
                witness_pubkey: hex::decode(&w.pubkey_hex)
                    .with_context(|| format!("witness {}: bad pubkey hex", w.id))?
                    .try_into()
                    .map_err(|_| anyhow::anyhow!("witness {}: pubkey must be 32 bytes", w.id))?,
            });
        }
    }
    if let Some(host) = cli_witness_host {
        let cluster_key = read_key32(cli_cluster_key, cli_cluster_key_file, "cluster-key")?;
        let witness_pubkey = read_key32(cli_witness_pubkey, cli_witness_pubkey_file, "witness-pubkey")?;
        witnesses.push(witness::WitnessSpec {
            id: format!("{}:{}", host, cli_witness_port),
            host,
            port: cli_witness_port,
            cluster_key,
            witness_pubkey,
        });
    }

    let lease_handle = if !witnesses.is_empty() {
        let peer_in_maintenance = cfg_file.as_ref()
            .map(|c| c.peer_in_maintenance)
            .unwrap_or(false);
        let cfg = witness::LeaseConfig {
            witnesses,
            sender_id,
            peer_sender_ids,
            ttl_ms: lease_ttl_ms,
            heartbeat_ms,
            fence_interfaces,
            peer_liveness: std::sync::Arc::clone(&peer_liveness),
            peer_registry: registry.clone(),
            peer_in_maintenance,
        };
        Some(witness::start_lease_loop(cfg))
    } else {
        log::info!("daemon: no witnesses configured; lease loop disabled (standalone mode)");
        None
    };
    let _lease = lease_handle;

    log::info!("bedrock-rust daemon: ipc={}", ipc_sock.display());
    server.serve()
}

fn read_key32_from_cfg(c: &config::DaemonConfig) -> Result<[u8; 32]> {
    if let Some(h) = c.cluster_key_hex.as_ref() {
        return read_key32(Some(h.clone()), None, "cluster-key");
    }
    let path = c.cluster_key_file.clone()
        .unwrap_or_else(|| PathBuf::from("/etc/bedrock/cluster.key"));
    let bytes = std::fs::read(&path)
        .with_context(|| format!("reading cluster-key file {}", path.display()))?;
    bytes.try_into()
        .map_err(|_| anyhow::anyhow!("cluster-key file must be exactly 32 bytes"))
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
