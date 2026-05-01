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
use std::net::{ToSocketAddrs, UdpSocket};
use std::time::Duration;

const RECV_TIMEOUT: Duration = Duration::from_secs(2);
const PAYLOAD_BLOCKS: usize = 2; // 64 bytes

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
