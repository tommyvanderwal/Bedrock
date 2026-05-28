-- Bedrock cluster-state schema for rqlite (on-disk SQLite mode).
--
-- This is the v1.0 replacement for the bedrock-rust hash-chained log.
-- Per docs/post-alpha-rewrite-notes.md D-01..D-22:
--
--   * Cluster state lives in rqlite — strong consistency via Raft,
--     HTTP/JSON wire protocol, MIT licensed, sqlite3-inspectable on
--     disk.
--   * The schema preserves the same logical shape as today's
--     view_builder.py fold output, so mgmt/app.py, orchestrator.py,
--     and the rest of the read-side don't change semantically — only
--     the source of data changes.
--   * Mutations bump `bedrock_meta.revision` (monotonic) — that's
--     the replacement for the log's `log_index`. Subscribers
--     (orchestrator.py reactor) poll for revision changes.
--   * Where today's `log_entries.py` had a free-form payload bytes
--     blob, here we have typed columns. JSON columns are used for
--     lists/maps that don't have a fixed cardinality (tier peers,
--     drbd_node_ids per tier, VM backup history).
--
-- Conventions:
--   * Every table has an `updated_at INTEGER` epoch-seconds column,
--     for forensics + cache-staleness checks.
--   * Composite-PK tables (tier_drbd_node_ids, vm_backups) use
--     surrogate INTEGER PRIMARY KEY plus a UNIQUE constraint on the
--     business key, so rows can be addressed cheaply.
--   * Work-queue tables (vm_intents, backup_intents) follow the
--     INTENT → OUTCOME pattern (D-19): a row appears with
--     state='pending', the owning node observes via watch/poll,
--     does idempotent work, transitions the row to 'completed'
--     or 'failed'.

-- ─────────────────────────────────────────────────────────────────
-- Meta — singleton row holding the cluster revision counter
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bedrock_meta (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
    revision     INTEGER NOT NULL DEFAULT 0,
    schema_ver   INTEGER NOT NULL DEFAULT 1,
    bootstrapped_at INTEGER
);

-- ─────────────────────────────────────────────────────────────────
-- Cluster identity — singleton row
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cluster_info (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
    cluster_uuid  TEXT NOT NULL,
    cluster_name  TEXT,
    mgmt_master   TEXT,
    updated_at    INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Membership
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nodes (
    node_name        TEXT PRIMARY KEY,
    host             TEXT NOT NULL,                     -- mgmt-LAN IP for SSH/HTTPS
    loopback_ip      TEXT NOT NULL DEFAULT '',          -- cluster /32 on `lo`
    role             TEXT NOT NULL DEFAULT 'compute',   -- 'compute' | 'mgmt+compute'
    pubkey           TEXT NOT NULL DEFAULT '',          -- SSH ed25519
    bedrock_pubkey   TEXT NOT NULL DEFAULT '',          -- inter-node API signing
    maintenance      INTEGER NOT NULL DEFAULT 0,        -- bool 0/1
    state            TEXT NOT NULL DEFAULT 'active',     -- 'joining' | 'active'
    updated_at       INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Storage tiers
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tiers (
    tier_name        TEXT PRIMARY KEY,
    mode             TEXT NOT NULL,
    master           TEXT,
    peers            TEXT NOT NULL DEFAULT '[]',        -- JSON array of node names
    backend_path     TEXT,
    version          INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL
);

-- Permanent per-tier DRBD node-id assignments (per L3: node-ids are
-- permanent for a resource). Composite key (tier_name, node_name).
CREATE TABLE IF NOT EXISTS tier_drbd_node_ids (
    tier_name   TEXT NOT NULL,
    node_name   TEXT NOT NULL,
    node_id     INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (tier_name, node_name)
);

-- ─────────────────────────────────────────────────────────────────
-- Witnesses
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS witnesses (
    witness_id              TEXT PRIMARY KEY,
    addr                    TEXT NOT NULL,
    witness_pubkey          TEXT NOT NULL,    -- hex
    encrypted_witness_key   TEXT NOT NULL,    -- hex (AEAD-wrapped per-witness key)
    backend                 TEXT NOT NULL DEFAULT 'echo',  -- 'echo' | 'smb' | 's3' (D-17)
    updated_at              INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Cluster parameters — open-ended key/value
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS params (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,    -- JSON-encoded for any value type
    updated_at  INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Operator logins (dashboard auth)
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS operators (
    username       TEXT PRIMARY KEY,
    salt           TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Join handshake — pending and resolved join requests
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS join_requests (
    request_id           TEXT PRIMARY KEY,
    node_name            TEXT NOT NULL,
    host                 TEXT NOT NULL,
    bedrock_pubkey       TEXT NOT NULL,
    x25519_eph_pubkey    TEXT NOT NULL,
    fingerprint          TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'rejected'
    master_eph_pubkey    TEXT NOT NULL DEFAULT '',
    ciphertext           TEXT NOT NULL DEFAULT '',
    nonce                TEXT NOT NULL DEFAULT '',
    reason               TEXT NOT NULL DEFAULT '',
    -- Joiner's TLS cert (CA-signed at /api/join/approve) and the
    -- cluster CA cert. Both PEM-encoded. Returned to the joiner via
    -- /api/join/status so it can configure rqlited mTLS immediately.
    node_cert_pem        TEXT NOT NULL DEFAULT '',
    ca_cert_pem          TEXT NOT NULL DEFAULT '',
    created_at           INTEGER NOT NULL,
    resolved_at          INTEGER
);

-- ─────────────────────────────────────────────────────────────────
-- VMs — declared state + last-known runtime state
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS vms (
    vm_name           TEXT PRIMARY KEY,
    vm_type           TEXT NOT NULL DEFAULT 'cattle',
    host              TEXT NOT NULL DEFAULT '',
    ram_mb            INTEGER NOT NULL DEFAULT 0,
    disk_gb           INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL DEFAULT 'creating', -- 'creating'|'created'|'create_failed'|'running'|'shut off'|'paused'|...
    intent_index      INTEGER,  -- the bedrock_meta.revision when the intent was filed
    fail_reason       TEXT,
    backup_schedule   TEXT,     -- JSON {target_id, cron_expr, label_prefix, retention_count, set_at_index}
    last_backup_error TEXT,     -- JSON {ts_index, target_id, reason}
    last_restore      TEXT,     -- JSON {ts_index, kopia_snapshot_id, target_id, dest_node}
    last_restore_err  TEXT,     -- JSON {ts_index, kopia_snapshot_id, target_id, reason}
    -- Predetermined failover sequence: JSON array of node_names in
    -- priority order. Primary is the first entry (matches `host`
    -- when not in a failover window). Secondary is index 1, tertiary
    -- is index 2 for vipet VMs. Cattle VMs use '[]' (no failover —
    -- local LV, no DRBD replication). The takeover protocol on a
    -- surviving node consults this list to decide whether it is the
    -- next failover target after a dead primary.
    failover_order    TEXT NOT NULL DEFAULT '[]',
    -- HA-importance / resource-claim used by the self-heal repair loop
    -- to order replica restoration after a permanent host loss:
    -- 'high' replicas are rebuilt before 'normal' before 'low'.
    -- Set at create time; 'normal' for VMs created before this column.
    priority          TEXT NOT NULL DEFAULT 'normal',
    updated_at        INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- VM intents — INTENT/OUTCOME work queue (D-19)
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS vm_intents (
    intent_id     TEXT PRIMARY KEY,        -- UUID
    vm_name       TEXT NOT NULL,
    intent_type   TEXT NOT NULL,           -- 'create' | 'destroy' | 'migrate' | 'state_change'
    payload       TEXT NOT NULL,           -- JSON of the operation-specific fields
    requested_by  TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'in_progress' | 'completed' | 'failed'
    result        TEXT,                    -- JSON of outcome details (kopia_snapshot_id for backup, etc.)
    error         TEXT,
    owning_node   TEXT NOT NULL DEFAULT '', -- which node should execute (often payload-dependent)
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    completed_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_vm_intents_state  ON vm_intents(state);
CREATE INDEX IF NOT EXISTS idx_vm_intents_vm     ON vm_intents(vm_name);
CREATE INDEX IF NOT EXISTS idx_vm_intents_owner  ON vm_intents(owning_node, state);

-- ─────────────────────────────────────────────────────────────────
-- Backup targets + history
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS backup_targets (
    target_id                       TEXT PRIMARY KEY,
    kind                            TEXT NOT NULL DEFAULT 'kopia-s3',  -- 'kopia-s3' | 'kopia-fs'
    s3_endpoint                     TEXT NOT NULL DEFAULT '',
    s3_bucket                       TEXT NOT NULL DEFAULT '',
    s3_region                       TEXT NOT NULL DEFAULT '',
    s3_disable_tls                  INTEGER NOT NULL DEFAULT 0,
    s3_disable_tls_verification     INTEGER NOT NULL DEFAULT 0,
    filesystem_path                 TEXT NOT NULL DEFAULT '',
    override_source_prefix          TEXT NOT NULL DEFAULT '',
    cache_directory                 TEXT NOT NULL DEFAULT '',
    updated_at                      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vm_backups (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_name             TEXT NOT NULL,
    target_id           TEXT NOT NULL,
    source_node         TEXT NOT NULL DEFAULT '',
    disks               TEXT NOT NULL,          -- JSON array: [{target_dev,lv_path,kopia_snapshot_id,bytes_added}, ...]
    primary_kopia_id    TEXT NOT NULL,          -- disks[0].kopia_snapshot_id, denormalised for UI lookup
    bytes_added         INTEGER NOT NULL DEFAULT 0,  -- rolled up across disks
    duration_s          REAL NOT NULL DEFAULT 0,
    label               TEXT NOT NULL DEFAULT '',
    fs_freeze_used      INTEGER NOT NULL DEFAULT 0,
    ts_index            INTEGER NOT NULL,       -- bedrock_meta.revision at backup time
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vm_backups_vm       ON vm_backups(vm_name, ts_index DESC);
CREATE INDEX IF NOT EXISTS idx_vm_backups_kopia    ON vm_backups(primary_kopia_id);

-- ─────────────────────────────────────────────────────────────────
-- Mesh path table — bedrock-net's LINK_UP/DOWN/QUALITY equivalent
-- ─────────────────────────────────────────────────────────────────

-- Canonical-order path key: "a_node|a_nic|b_node|b_nic" with
-- (a_node, a_nic) sorted alphabetically below (b_node, b_nic).
-- Computed by the writer + stored as-is (a < b).
CREATE TABLE IF NOT EXISTS paths (
    path_key         TEXT PRIMARY KEY,
    node_a           TEXT NOT NULL,
    nic_a            TEXT NOT NULL,
    link_addr_a      TEXT NOT NULL DEFAULT '',
    node_b           TEXT NOT NULL,
    nic_b            TEXT NOT NULL,
    link_addr_b      TEXT NOT NULL DEFAULT '',
    speed_mbps       INTEGER NOT NULL DEFAULT 0,
    rtt_us           INTEGER NOT NULL DEFAULT 0,
    observed_at      REAL NOT NULL DEFAULT 0,
    up_since         REAL NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Observability backends — designated VM/VL nodes (the 2-node
-- dual-write pattern). Each row = one designated backend node for
-- one of the two stacks.
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS obs_backends (
    stack       TEXT NOT NULL,         -- 'metrics' | 'logs'
    node_name   TEXT NOT NULL,
    position    INTEGER NOT NULL,      -- 0 or 1 (which of the 2 dual-write targets)
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (stack, position)
);

-- ─────────────────────────────────────────────────────────────────
-- DRBD resources — one row per DRBD resource (cluster + per-VM).
-- Per docs/storage-architecture.md: one thin data LV + one thin
-- meta LV per resource in a named thinpool. The orchestrator
-- materialises the LV pair + drbd_resource on each target node
-- from the rows here.
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS drbd_resources (
    name             TEXT PRIMARY KEY,         -- e.g. "cluster", "vm-foo-disk0"
    minor            INTEGER NOT NULL,         -- DRBD minor number
    data_lv          TEXT NOT NULL,            -- e.g. "bedrock-data-vm-foo-disk0"
    meta_lv          TEXT NOT NULL,            -- e.g. "bedrock-meta-vm-foo-disk0"
    thinpool         TEXT NOT NULL DEFAULT 'thinpool',
    data_size_bytes  INTEGER NOT NULL,
    meta_size_bytes  INTEGER NOT NULL,
    max_peers        INTEGER NOT NULL DEFAULT 7,
    -- JSON array of node_names that should host this resource.
    -- For cluster singleton: capped at 3. For per-VM: replica count
    -- per VM type (cattle=0, pet=2, vipet=3). Unordered set; the
    -- failover priority order lives on vms.failover_order, not here.
    peers            TEXT NOT NULL DEFAULT '[]',
    -- Last-known authoritative DRBD current-uuid for this resource,
    -- recorded by the node that promoted DRBD to Primary. Written
    -- via UPDATE level='strong' so quorum confirms before the VM is
    -- started on the new primary. Read via SELECT level='strong'
    -- in the pre-start safety check (is_safe_to_start_vm) to ensure
    -- we are not about to start a VM whose disk data is behind the
    -- cluster's last-known state. Updated on every successful
    -- drbdadm primary transition.
    current_uuid     TEXT NOT NULL DEFAULT '',
    uuid_ts_set      INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

-- Membership of the 3-peer cluster-singleton set. The calm
-- orchestrator owns this table; on a node leave/join it picks
-- replacements deliberately (resource-aware) and schedules
-- drbdadm new-peer / detach operations via the operations table.
-- Which nodes carry the cluster-singleton DRBD resource (capped
-- at 3). This is the set the .254 arbiter VIP can migrate to.
-- The calm orchestrator owns this table; on a node leave/join it
-- picks replacements deliberately (resource-aware) and schedules
-- drbdadm new-peer / detach operations via the operations table.
CREATE TABLE IF NOT EXISTS cluster_drbd_membership (
    node_name   TEXT PRIMARY KEY,
    joined_at   INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- Membership of the Raft-3 SeaweedFS master set. Same calm-loop
-- ownership pattern as cluster_drbd_membership.
CREATE TABLE IF NOT EXISTS seaweed_master_membership (
    node_name   TEXT PRIMARY KEY,
    joined_at   INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────────
-- Sagas — generic crash-safe orchestration. Every long-running
-- cluster operation writes intent here, executes idempotent steps,
-- and writes "done". Recovery from power loss: on boot, query for
-- in-flight operations, resume from last incomplete step. Per
-- docs/storage-architecture.md "Everything goes through rqlite".
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,         -- e.g. "drbd_resource_create",
                                         --      "cluster_tier_promote",
                                         --      "weed_master_reshuffle",
                                         --      "node_leave"
    target_node   TEXT,                  -- node that runs this; NULL = any
    params        TEXT NOT NULL,         -- JSON of operation-specific fields
    state         TEXT NOT NULL DEFAULT 'pending',
                                         -- 'pending'|'in_progress'|
                                         -- 'completed'|'failed'
    requested_by  TEXT NOT NULL DEFAULT '',
    error         TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    completed_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_operations_state  ON operations(state);
CREATE INDEX IF NOT EXISTS idx_operations_target ON operations(target_node, state);
CREATE INDEX IF NOT EXISTS idx_operations_kind   ON operations(kind, state);

CREATE TABLE IF NOT EXISTS operation_steps (
    op_id        INTEGER NOT NULL,
    step_name    TEXT NOT NULL,
    state        TEXT NOT NULL,          -- 'done' | 'failed'
    error        TEXT,
    started_at   INTEGER,
    finished_at  INTEGER,
    PRIMARY KEY (op_id, step_name),
    FOREIGN KEY (op_id) REFERENCES operations(id)
);

CREATE INDEX IF NOT EXISTS idx_operation_steps_op ON operation_steps(op_id);
