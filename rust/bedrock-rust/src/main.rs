//! Bedrock cluster-protocol daemon.
//!
//! v0.1 scope: append-only hash-chained log + an Echo witness client.
//! Designed per `docs/cluster-protocol-design.md` and the v1 plan.

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

mod log_store;
mod payload;
mod witness;

/// Default location for the cluster log on disk.
const DEFAULT_LOG_DIR: &str = "/var/lib/bedrock/log";

#[derive(Parser)]
#[command(name = "bedrock-rust", version, about = "Bedrock cluster-protocol daemon")]
struct Cli {
    /// Path to the log directory (segment files live here).
    #[arg(long, global = true, default_value = DEFAULT_LOG_DIR)]
    log_dir: PathBuf,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
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
                            if matches!(payload::Kind::from_u8(e.kind), Some(payload::Kind::Bootstrap | payload::Kind::Opaque)) {
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
