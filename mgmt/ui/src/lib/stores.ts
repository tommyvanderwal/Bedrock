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

export const nodes = writable<Record<string, NodeInfo>>({});
export const vms = writable<Record<string, VMInfo>>({});
export const witness = writable<WitnessInfo>({ nodes: {} });
export const events = writable<any[]>([]);
export const connected = writable(false);
export const lastUpdate = writable<string>('');
// Task registry — keyed by task id. Updated via WS 'task' channel.
export const tasks = writable<Record<string, TaskInfo>>({});
