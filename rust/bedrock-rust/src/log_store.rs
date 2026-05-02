//! Append-only hash-chained log.
//!
//! Per the design doc §3 / §4: a single log per cluster, replicated by Rust,
//! every entry SHA-256-chained to the previous one. v0.1 keeps it simple:
//! a single segment file `<dir>/00000001.log` (no rolling yet — phase 7),
//! every append fsync'd, no concurrency.
//!
//! Wire format of one entry on disk:
//!
//! ```
//!   ┌────────────────┬──────────────────────────────┐
//!   │  4 B frame_len │  frame_len bytes (the entry) │
//!   └────────────────┴──────────────────────────────┘
//! ```
//!
//! The frame contains a fixed-layout header followed by the payload bytes:
//!
//! ```
//!   offset  size  field
//!   ──────────────────────────────────────────────────────
//!   0       8     index            u64, big-endian
//!   8       8     epoch            u64, big-endian
//!   16      32    prev_hash        SHA-256 of the previous entry's frame,
//!                                  zero for index 1
//!   48      1     payload_kind     u8 (see payload::Kind)
//!   49      4     payload_len      u32, big-endian
//!   53      <pl>  payload          payload_len bytes
//!   ──────────────────────────────────────────────────────
//! ```
//!
//! `entry.hash` is `SHA-256(frame_bytes)` — the on-disk frame is what gets
//! hashed, so the next entry's `prev_hash` is reproducible by anyone who
//! reads the file. This is what the witness's STATUS_DETAIL `peer_payload`
//! later carries (§5 of the Echo protocol): a node says "I'm at
//! (epoch=N, last_index=M, last_hash=H)" and the witness records H so
//! peers can query for it.

use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use thiserror::Error;

use crate::payload::Kind;

const HEADER_LEN: usize = 8 + 8 + 32 + 1 + 4;
const FRAME_LEN_BYTES: usize = 4;

#[derive(Debug, Error)]
pub enum LogError {
    #[error("log dir already initialised at {0}")]
    AlreadyInitialised(PathBuf),
    #[error("log dir not initialised — run `bedrock-rust log init` first")]
    NotInitialised,
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("payload too large ({size} bytes; max {})", u32::MAX)]
    PayloadTooLarge { size: usize },
    #[error("frame at offset {offset} truncated (got {got} of {expected} bytes)")]
    Truncated { offset: u64, got: usize, expected: usize },
    #[error("entry {index}: prev_hash mismatch (chain says {expected}, frame has {actual})")]
    ChainBreak {
        index: u64,
        expected: String,
        actual: String,
    },
    #[error("entry indices not contiguous (saw {prev} then {got})")]
    IndexGap { prev: u64, got: u64 },
}

pub type Result<T> = std::result::Result<T, LogError>;

/// One log entry, in memory.
pub struct Entry {
    pub index: u64,
    pub epoch: u64,
    pub prev_hash: [u8; 32],
    pub kind: u8,
    pub payload: Vec<u8>,
    /// Hash of this entry's on-disk frame.
    pub hash: [u8; 32],
}

/// The log on disk. Single segment file in v0.1.
pub struct Log {
    file: File,
    dir: PathBuf,
    /// (latest index, latest entry hash) — what new appends chain off of.
    latest: (u64, [u8; 32]),
}

fn segment_path(dir: &Path) -> PathBuf {
    dir.join("00000001.log")
}

/// Encode the bootstrap entry payload as MessagePack: a 2-key map
/// `{"t": "bootstrap", "uuid": <cluster_uuid>}`. Hand-rolled (rather
/// than going through serde) because we want this byte-for-byte
/// stable and the only fields involved are tiny strings.
fn encode_bootstrap_payload(uuid: &str) -> Vec<u8> {
    let mut buf = Vec::with_capacity(64);
    // map of 2 entries → fixmap header 0x82
    buf.push(0x82);
    write_msgpack_str(&mut buf, "t");
    write_msgpack_str(&mut buf, "bootstrap");
    write_msgpack_str(&mut buf, "uuid");
    write_msgpack_str(&mut buf, uuid);
    buf
}

fn write_msgpack_str(buf: &mut Vec<u8>, s: &str) {
    let bytes = s.as_bytes();
    let n = bytes.len();
    if n <= 31 {
        // fixstr 0xa0..0xbf
        buf.push(0xa0 | (n as u8));
    } else if n <= u8::MAX as usize {
        buf.push(0xd9);
        buf.push(n as u8);
    } else if n <= u16::MAX as usize {
        buf.push(0xda);
        buf.extend_from_slice(&(n as u16).to_be_bytes());
    } else {
        buf.push(0xdb);
        buf.extend_from_slice(&(n as u32).to_be_bytes());
    }
    buf.extend_from_slice(bytes);
}

impl Log {
    /// Initialise a fresh log at `dir`. Writes the bootstrap entry at
    /// index 1 with the typed payload `{"t": "bootstrap", "uuid":
    /// <cluster_uuid>}` (msgpack), so the view builder can fold it
    /// like any other entry. The hash of this entry chains the rest of
    /// the cluster's history — re-initialising with a different uuid
    /// produces a different hash, distinguishing a re-init from a
    /// continuation (design §4). Refuses if the segment file already
    /// exists.
    pub fn init(dir: &Path, cluster_uuid: &str) -> Result<Self> {
        std::fs::create_dir_all(dir)?;
        let segment = segment_path(dir);
        if segment.exists() {
            return Err(LogError::AlreadyInitialised(dir.to_path_buf()));
        }
        let file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .open(&segment)?;
        let mut log = Self {
            file,
            dir: dir.to_path_buf(),
            latest: (0, [0u8; 32]),
        };
        let payload = encode_bootstrap_payload(cluster_uuid);
        log.append(Kind::Bootstrap, &payload)?;
        Ok(log)
    }

    /// Open an existing log. Replays the segment to recover `latest`.
    pub fn open(dir: &Path) -> Result<Self> {
        let segment = segment_path(dir);
        if !segment.exists() {
            return Err(LogError::NotInitialised);
        }
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .open(&segment)?;
        let mut log = Self {
            file,
            dir: dir.to_path_buf(),
            latest: (0, [0u8; 32]),
        };
        log.scan_for_latest()?;
        Ok(log)
    }

    /// Append a payload as the next entry. Returns the materialised entry.
    pub fn append(&mut self, kind: Kind, payload: &[u8]) -> Result<Entry> {
        if payload.len() > u32::MAX as usize {
            return Err(LogError::PayloadTooLarge { size: payload.len() });
        }
        let next_index: u64 = self.latest.0 + 1;
        let prev_hash = self.latest.1;
        let epoch: u64 = 1; // v0.1: always 1; epoch bumps on leader change (phase 4)

        let mut frame = Vec::with_capacity(HEADER_LEN + payload.len());
        frame.extend_from_slice(&next_index.to_be_bytes());
        frame.extend_from_slice(&epoch.to_be_bytes());
        frame.extend_from_slice(&prev_hash);
        frame.push(kind as u8);
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload);

        let mut hasher = Sha256::new();
        hasher.update(&frame);
        let hash: [u8; 32] = hasher.finalize().into();

        let frame_len_bytes = (frame.len() as u32).to_be_bytes();
        // Buffer both writes locally so the file write is one shot.
        let mut record = Vec::with_capacity(FRAME_LEN_BYTES + frame.len());
        record.extend_from_slice(&frame_len_bytes);
        record.extend_from_slice(&frame);
        self.file.write_all(&record)?;
        self.file.sync_data()?;

        self.latest = (next_index, hash);

        Ok(Entry {
            index: next_index,
            epoch,
            prev_hash,
            kind: kind as u8,
            payload: payload.to_vec(),
            hash,
        })
    }

    /// (latest_index, latest_hash). For an empty log returns (0, [0u8;32]).
    pub fn latest(&self) -> (u64, [u8; 32]) {
        self.latest
    }

    /// Read the entry at the given index (1-based). None if past the tail.
    pub fn read(&self, index: u64) -> Result<Option<Entry>> {
        for entry_result in self.iter()? {
            let entry = entry_result?;
            if entry.index == index {
                return Ok(Some(entry));
            }
            if entry.index > index {
                return Ok(None);
            }
        }
        Ok(None)
    }

    /// Iterate every entry from the start of the log.
    pub fn iter(&self) -> Result<EntryIter> {
        let segment = segment_path(&self.dir);
        let f = OpenOptions::new().read(true).open(&segment)?;
        Ok(EntryIter {
            r: BufReader::new(f),
            offset: 0,
        })
    }

    /// Walk the whole log, recompute every hash, and verify the chain.
    /// Returns the number of entries on success.
    pub fn verify(&self) -> Result<u64> {
        let mut prev_hash = [0u8; 32];
        let mut prev_index = 0u64;
        let mut count = 0u64;
        for entry_result in self.iter()? {
            let entry = entry_result?;
            if prev_index != 0 && entry.index != prev_index + 1 {
                return Err(LogError::IndexGap {
                    prev: prev_index,
                    got: entry.index,
                });
            }
            if entry.prev_hash != prev_hash {
                return Err(LogError::ChainBreak {
                    index: entry.index,
                    expected: hex::encode(prev_hash),
                    actual: hex::encode(entry.prev_hash),
                });
            }
            prev_hash = entry.hash;
            prev_index = entry.index;
            count += 1;
        }
        Ok(count)
    }

    fn scan_for_latest(&mut self) -> Result<()> {
        let mut last_index = 0u64;
        let mut last_hash = [0u8; 32];
        for entry_result in self.iter()? {
            let entry = entry_result?;
            last_index = entry.index;
            last_hash = entry.hash;
        }
        self.latest = (last_index, last_hash);
        // Position the file cursor at the end so the next append-mode write
        // continues correctly (append-mode handles this on most platforms,
        // but be explicit).
        self.file.seek(SeekFrom::End(0))?;
        Ok(())
    }
}

pub struct EntryIter {
    r: BufReader<File>,
    offset: u64,
}

impl Iterator for EntryIter {
    type Item = Result<Entry>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut len_buf = [0u8; FRAME_LEN_BYTES];
        match self.r.read_exact(&mut len_buf) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return None,
            Err(e) => return Some(Err(e.into())),
        }
        let frame_len = u32::from_be_bytes(len_buf) as usize;
        if frame_len < HEADER_LEN {
            return Some(Err(LogError::Truncated {
                offset: self.offset,
                got: frame_len,
                expected: HEADER_LEN,
            }));
        }

        let mut frame = vec![0u8; frame_len];
        if let Err(e) = self.r.read_exact(&mut frame) {
            return Some(Err(LogError::Io(e)));
        }

        // Hash the frame as-is — that's what the chain commits to.
        let mut hasher = Sha256::new();
        hasher.update(&frame);
        let hash: [u8; 32] = hasher.finalize().into();

        // Parse fields.
        let index = u64::from_be_bytes(frame[0..8].try_into().unwrap());
        let epoch = u64::from_be_bytes(frame[8..16].try_into().unwrap());
        let mut prev_hash = [0u8; 32];
        prev_hash.copy_from_slice(&frame[16..48]);
        let kind = frame[48];
        let payload_len = u32::from_be_bytes(frame[49..53].try_into().unwrap()) as usize;
        if HEADER_LEN + payload_len != frame_len {
            return Some(Err(LogError::Truncated {
                offset: self.offset,
                got: frame_len,
                expected: HEADER_LEN + payload_len,
            }));
        }
        let payload = frame[HEADER_LEN..HEADER_LEN + payload_len].to_vec();

        self.offset += FRAME_LEN_BYTES as u64 + frame_len as u64;

        Some(Ok(Entry {
            index,
            epoch,
            prev_hash,
            kind,
            payload,
            hash,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    #[test]
    fn init_writes_typed_bootstrap_entry() {
        let d = tmpdir();
        let log = Log::init(d.path(), "test-uuid").unwrap();
        let (idx, _) = log.latest();
        assert_eq!(idx, 1);
        let e = log.read(1).unwrap().unwrap();
        assert_eq!(e.kind, Kind::Bootstrap as u8);
        assert_eq!(e.prev_hash, [0u8; 32]);
        assert_eq!(e.epoch, 1);
        // Payload is MessagePack {"t":"bootstrap","uuid":"test-uuid"}.
        // Spot-check the structure rather than the exact bytes — the
        // view builder is what unfolds it.
        assert_eq!(e.payload[0], 0x82, "expected fixmap of 2 entries");
        // "t" → "bootstrap"
        assert_eq!(e.payload[1], 0xa1); // fixstr len 1
        assert_eq!(e.payload[2], b't');
        assert_eq!(e.payload[3], 0xa9); // fixstr len 9 ("bootstrap")
        assert_eq!(&e.payload[4..13], b"bootstrap");
        // "uuid" → "test-uuid"
        assert_eq!(e.payload[13], 0xa4); // fixstr len 4
        assert_eq!(&e.payload[14..18], b"uuid");
        assert_eq!(e.payload[18], 0xa9); // fixstr len 9 ("test-uuid")
        assert_eq!(&e.payload[19..28], b"test-uuid");
    }

    #[test]
    fn append_chains_correctly() {
        let d = tmpdir();
        let mut log = Log::init(d.path(), "u").unwrap();
        let h1 = log.latest().1;
        let e2 = log.append(Kind::Opaque, b"hello").unwrap();
        assert_eq!(e2.prev_hash, h1);
        assert_eq!(e2.index, 2);
        let e3 = log.append(Kind::Opaque, b"world").unwrap();
        assert_eq!(e3.prev_hash, e2.hash);
        assert_eq!(e3.index, 3);
    }

    #[test]
    fn verify_passes_clean_log() {
        let d = tmpdir();
        let mut log = Log::init(d.path(), "u").unwrap();
        for i in 0..50 {
            log.append(Kind::Opaque, format!("payload-{i}").as_bytes())
                .unwrap();
        }
        assert_eq!(log.verify().unwrap(), 51);
    }

    #[test]
    fn verify_detects_byte_flip() {
        let d = tmpdir();
        {
            let mut log = Log::init(d.path(), "u").unwrap();
            log.append(Kind::Opaque, b"alice").unwrap();
            log.append(Kind::Opaque, b"bob").unwrap();
            log.append(Kind::Opaque, b"carol").unwrap();
        }
        // Flip one byte inside the prev_hash field of entry 3 (a region
        // the iterator parses successfully — bytes 16..48 of the frame
        // are pure data — so we can deterministically test that the
        // *chain-walk* in `verify` catches the divergence rather than
        // the parse-time length checks.
        //
        // Layout: 4 (frame_len) + 8 (index) + 8 (epoch) + 32 (prev_hash) ...
        // Entry 1's frame_len header is at offset 0; entry 1's frame is
        // 4 + frame_len bytes. We need to flip a prev_hash byte of
        // entry 3, so skip past entry 1 + entry 2 first.
        let mut bytes = std::fs::read(segment_path(d.path())).unwrap();
        let mut off = 0usize;
        for _ in 0..2 {
            let flen = u32::from_be_bytes(bytes[off..off + 4].try_into().unwrap()) as usize;
            off += 4 + flen;
        }
        // Now `off` is at entry 3's frame_len bytes; data starts at off+4,
        // prev_hash is at off+4+16..off+4+48.
        let target = off + 4 + 16 + 5; // a byte in the middle of prev_hash
        bytes[target] ^= 0x01;
        std::fs::write(segment_path(d.path()), bytes).unwrap();

        let log = Log::open(d.path()).expect("open should still succeed");
        let err = log.verify().expect_err("verify should detect divergence");
        match err {
            LogError::ChainBreak { .. } => {}
            other => panic!("expected ChainBreak, got {:?}", other),
        }
    }

    #[test]
    fn open_recovers_latest() {
        let d = tmpdir();
        {
            let mut log = Log::init(d.path(), "u").unwrap();
            for i in 0..10 {
                log.append(Kind::Opaque, format!("{i}").as_bytes()).unwrap();
            }
        }
        let log = Log::open(d.path()).unwrap();
        let (idx, _) = log.latest();
        assert_eq!(idx, 11);
    }
}
