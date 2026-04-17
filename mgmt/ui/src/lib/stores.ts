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

export interface VMInfo {
	name: string;
	state: string;
	running_on: string | null;
	backup_node: string | null;
	defined_on: string[];
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
