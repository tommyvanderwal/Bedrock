/** Reactive stores for cluster state */
import { writable } from 'svelte/store';

export interface NodeInfo {
	name: string;
	host: string;
	online: boolean;
	kernel: string;
	load: string;
	mem_total_mb: number;
	mem_used_mb: number;
	uptime_since: string;
	running_vms: string[];
	all_vms: string[];
	cockpit_url: string;
	cpu_pct?: number;
}

export interface VMDisk {
	target: string;       // vda, vdb, ...
	bus: string;
	source: string;       // /dev/drbd1000 or /dev/almalinux/vm-X-disk0
	backing_lv: string;
	drbd_resource: string;
	drbd_minor: number | null;
	size_bytes?: number;
	size_gb?: number;
	drbd_role?: string;
	drbd_disk?: string;
	drbd_peer_disk?: string;
	drbd_sync_pct?: string;
}

export interface VMInfo {
	name: string;
	state: string;
	running_on: string | null;
	backup_node: string | null;
	defined_on: string[];
	disks: VMDisk[];
	drbd_resource: string;
	drbd_role: string;
	drbd_disk: string;
	drbd_peer_disk: string;
	drbd_replication: string;
	drbd_sync_pct: string;
	vnc_ws_url: string;
	cpu_pct?: number;
	disk_wr_iops?: number;
	disk_rd_iops?: number;
}

export interface TaskStep {
	name: string;
	state: 'pending' | 'running' | 'done' | 'failed' | 'skipped';
	progress?: number;
	duration_ms?: number;
	error?: string;
	started_at?: string;
	ended_at?: string;
}
export interface TaskInfo {
	id: string;
	type: string;
	subject: string;
	state: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';
	progress?: number;
	started_at: string;
	updated_at: string;
	ended_at?: string;
	error?: string;
	steps: TaskStep[];
	log_tail?: string;
	vm_name?: string;
	import_id?: string;
	node?: string;
}

export interface WitnessInfo {
	nodes: Record<string, { alive: boolean; last_seen_ms_ago: number }>;
	witness_uptime_secs?: number;
	error?: string;
}

export interface TopologyConnection {
	node: string;
	my_nic: string;
	protocol: string;     // lldp | cdp | mndp
	port_id: string;
	port_descr: string;
	first_seen: number;
	last_seen: number;
}
export interface TopologySwitch {
	device_key: string;   // lowercase MAC, the cross-protocol merge key
	system_name: string;
	mgmt_ip: string;
	platform: string;
	aliases: string[];    // distinct chassis_id values seen for this MAC
	protocols: string[];  // ['lldp', 'cdp', 'mndp']
	connections: TopologyConnection[];
}
export interface TopologyLink {
	node_a: string;
	nic_a: string;
	addr_a: string;
	node_b: string;
	nic_b: string;
	addr_b: string;
	speed_mbps: number;
	rtt_us: number;
	blip_total: number;
	first_seen?: number;
	last_seen?: number;
}
export interface TopologyInfo {
	switches: Record<string, TopologySwitch>;
	links: TopologyLink[];
	node_count: number;
	switch_count: number;
	link_count: number;
	computed_at: number;
}

export const nodes = writable<Record<string, NodeInfo>>({});
export const vms = writable<Record<string, VMInfo>>({});
export const witness = writable<WitnessInfo>({ nodes: {} });
export const topology = writable<TopologyInfo>({
	switches: {}, links: [],
	node_count: 0, switch_count: 0, link_count: 0, computed_at: 0,
});
export const events = writable<any[]>([]);
export const connected = writable(false);
export const lastUpdate = writable<string>('');
// Task registry — keyed by task id. Updated via WS 'task' channel.
export const tasks = writable<Record<string, TaskInfo>>({});

// Pending join requests. +layout polls /api/join/pending every 5s and
// writes them here; the main page (+page.svelte) renders them above the
// host grid so the operator can approve/reject inline.
export interface PendingJoin {
	request_id: string;
	node_name: string;
	host: string;
	fingerprint: string;
	bedrock_pubkey: string;
}
export const pendingJoins = writable<PendingJoin[]>([]);
