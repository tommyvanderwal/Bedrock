/** REST API client for Bedrock management — Bearer-token aware. */

const BASE = '';  // same origin
const TOKEN_KEY = 'bedrock_token';
const EXP_KEY = 'bedrock_token_exp';

export function setToken(token: string, exp: number) {
	if (typeof localStorage === 'undefined') return;
	localStorage.setItem(TOKEN_KEY, token);
	localStorage.setItem(EXP_KEY, String(exp));
}

export function clearToken() {
	if (typeof localStorage === 'undefined') return;
	localStorage.removeItem(TOKEN_KEY);
	localStorage.removeItem(EXP_KEY);
}

export function getToken(): string | null {
	if (typeof localStorage === 'undefined') return null;
	const t = localStorage.getItem(TOKEN_KEY);
	if (!t) return null;
	const exp = Number(localStorage.getItem(EXP_KEY) || '0');
	if (exp && Date.now() / 1000 > exp - 30) {
		clearToken();
		return null;
	}
	return t;
}

// Module-local fetch that wraps the global one: adds Bearer token,
// redirects to /login on 401. All apiGet/apiPost/etc below pick this
// up because of lexical scoping — no callsite changes needed.
const _origFetch: typeof globalThis.fetch = (typeof globalThis !== 'undefined' && globalThis.fetch)
	? globalThis.fetch.bind(globalThis)
	: ((..._a: any[]) => Promise.reject(new Error('fetch not available'))) as any;

async function fetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
	const headers = new Headers(init.headers || {});
	const t = getToken();
	if (t && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${t}`);
	const r = await _origFetch(input as any, { ...init, headers });
	if (r.status === 401 && typeof window !== 'undefined'
		&& !window.location.pathname.startsWith('/login')) {
		clearToken();
		const back = window.location.pathname + window.location.search;
		window.location.href = `/login?next=${encodeURIComponent(back)}`;
	}
	return r;
}

/** Public POST — no Bearer header. Used by /api/login. */
export async function apiPostPublic(path: string, body: any) {
	const r = await _origFetch(`${BASE}${path}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function apiGet(path: string) {
	const r = await fetch(`${BASE}${path}`);
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function apiPost(path: string, body?: any) {
	const r = await fetch(`${BASE}${path}`, {
		method: 'POST',
		headers: body ? { 'Content-Type': 'application/json' } : {},
		body: body ? JSON.stringify(body) : undefined,
	});
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function getCluster() {
	return apiGet('/api/cluster');
}

export async function vmStart(name: string) {
	return apiPost(`/api/vms/${name}/start`);
}

export async function vmShutdown(name: string) {
	return apiPost(`/api/vms/${name}/stop`);
}

export async function vmPoweroff(name: string) {
	return apiPost(`/api/vms/${name}/force-stop`);
}

export async function vmMigrate(name: string, targetNode?: string) {
	return apiPost(`/api/vms/${name}/migrate`, targetNode ? { target_node: targetNode } : {});
}

export async function vmConvert(name: string, targetType: 'cattle' | 'pet' | 'vipet') {
	return apiPost(`/api/vms/${name}/ha-level`, { vm_type: targetType });
}

export interface VMCreateRequest {
	name: string;
	vcpus: number;
	ram_mb: number;
	disk_gb: number;
	priority: 'low' | 'normal' | 'high';
	iso?: string | null;
	extra_disks?: Array<{ size_gb: number }>;
}

export async function vmCreate(req: VMCreateRequest) {
	return apiPost('/api/vms', req);
}

export async function vmAttachDisk(name: string, size_gb: number) {
	return apiPost(`/api/vms/${encodeURIComponent(name)}/disks`, { size_gb });
}

export async function vmDelete(name: string) {
	const r = await fetch(`/api/vms/${encodeURIComponent(name)}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export interface VMSettings {
	name: string;
	host: string;
	vcpus: number;
	ram_mb: number;
	disk_gb: number;
	disk_path: string;
	disk_target: string;
	drbd_resource: string;
	cdrom_slot: string | null;
	cdrom_iso: string | null;
	priority: 'low' | 'normal' | 'high';
	cpu_shares: number | null;
}

export async function getVmSettings(name: string): Promise<VMSettings> {
	return apiGet(`/api/vms/${name}/settings`);
}

export async function setVmResources(name: string, body: Partial<Pick<VMSettings, 'vcpus' | 'ram_mb' | 'disk_gb'>>) {
	return apiPost(`/api/vms/${name}/compute`, body);
}

export async function setVmPriority(name: string, priority: 'low' | 'normal' | 'high') {
	return apiPost(`/api/vms/${name}/priority`, { priority });
}

export async function setVmCdrom(name: string, action: 'eject' | 'insert', iso?: string) {
	return apiPost(`/api/vms/${name}/cdrom`, { action, iso });
}

// ── Imports ───────────────────────────────────────────────────────────────
export interface ImportJob {
	id: string;
	original_name: string;
	input_format: string;
	input_size_bytes: number;
	status: 'uploading' | 'uploaded' | 'converting' | 'ready' | 'failed' | 'consumed';
	created_at: string;
	virtual_size_gb?: number;
	virtual_size_bytes?: number;
	detected_name?: string;
	detected_os_type?: string;
	detected_firmware?: 'bios' | 'uefi';
	injected_drivers?: boolean;
	// Populated at upload time by virt-inspector (or format hint for VHD/VHDX)
	os_type?: string;        // windows / linux / freebsd / ""
	os_distro?: string;
	os_product_name?: string;
	os_version?: string;
	os_osinfo?: string;
	os_detection?: string;   // which path produced the result
	error?: string;
	consumed_as?: string;
	log_tail?: string;
	log_size?: number;
}

export async function listImports(): Promise<ImportJob[]> { return apiGet('/api/imports'); }
export async function getImport(id: string): Promise<ImportJob> { return apiGet(`/api/imports/${id}`); }
export async function uploadImport(file: File, onProgress?: (pct: number) => void): Promise<ImportJob> {
	return new Promise((resolve, reject) => {
		const xhr = new XMLHttpRequest();
		xhr.open('POST', '/api/imports/upload');
		const _t1 = getToken(); if (_t1) xhr.setRequestHeader('Authorization', `Bearer ${_t1}`);
		xhr.upload.onprogress = (e) => {
			if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
		};
		xhr.onload = () => {
			if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
			else reject(new Error(`${xhr.status}: ${xhr.responseText}`));
		};
		xhr.onerror = () => reject(new Error('Upload failed'));
		const fd = new FormData();
		fd.append('file', file);
		xhr.send(fd);
	});
}
export async function convertImport(id: string, injectDrivers: boolean | null = null) {
	// null → server auto-selects based on detected OS at upload time.
	const body: any = {};
	if (injectDrivers !== null) body.inject_drivers = injectDrivers;
	return apiPost(`/api/imports/${id}/convert`, body);
}
export async function deleteImport(id: string) {
	const r = await fetch(`/api/imports/${encodeURIComponent(id)}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}
export async function importCreateVM(id: string, body: {name: string; vcpus: number; ram_mb: number; priority: 'low'|'normal'|'high'}) {
	return apiPost(`/api/imports/${id}/create-vm`, body);
}

// ── Exports ───────────────────────────────────────────────────────────────
export interface ExportJob {
	id: string; vm: string; format: string; status: string;
	size_bytes?: number; created_at: string; error?: string;
}
export async function listExports(): Promise<ExportJob[]> { return apiGet('/api/exports'); }
export async function startVmExport(name: string, format: 'qcow2'|'vmdk'|'vhdx'|'raw'): Promise<ExportJob> {
	return apiPost(`/api/vms/${name}/export`, { format });
}
export async function deleteExport(id: string) {
	const r = await fetch(`/api/exports/${encodeURIComponent(id)}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function listIsos(): Promise<Array<{ name: string; size_bytes: number }>> {
	return apiGet('/api/isos');
}

export async function deleteIso(name: string) {
	const r = await fetch(`/api/isos/${encodeURIComponent(name)}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function uploadIso(file: File, onProgress?: (pct: number) => void) {
	return new Promise((resolve, reject) => {
		const xhr = new XMLHttpRequest();
		xhr.open('POST', '/api/isos');
		const _t2 = getToken(); if (_t2) xhr.setRequestHeader('Authorization', `Bearer ${_t2}`);
		xhr.upload.onprogress = (e) => {
			if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
		};
		xhr.onload = () => {
			if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
			else reject(new Error(`${xhr.status}: ${xhr.responseText}`));
		};
		xhr.onerror = () => reject(new Error('Upload failed'));
		const fd = new FormData();
		fd.append('file', file);
		xhr.send(fd);
	});
}

// ── Backup ────────────────────────────────────────────────────────────────

export interface BackupTarget {
	id: string;
	kind: 'kopia-s3' | 'kopia-fs';
	s3_endpoint?: string;
	s3_bucket?: string;
	s3_region?: string;
	filesystem_path?: string;
	override_source_prefix?: string;
	cache_directory?: string;
	// Multi-target replication
	is_mirror?: boolean;       // a sync-to destination (never independently created)
	sync_to?: string[];        // secondary target ids this primary mirrors to
	delete_orphans?: boolean;  // kopia sync-to --delete (prune mirrors)
}

export interface VmBackupDisk {
	target_dev: string;     // 'vda', 'vdb', …
	lv_path: string;
	kopia_snapshot_id: string;
	bytes_added: number;
}

export interface VmBackup {
	// `kopia_snapshot_id` at the top is the row's primary identifier
	// (= disks[0].kopia_snapshot_id). Keep using it for restore/delete
	// API calls; the server resolves the full row from it.
	kopia_snapshot_id: string;
	disks?: VmBackupDisk[];     // multi-disk authoritative list
	target_id: string;
	source_node: string;
	bytes_added: number;        // rolled-up across disks
	duration_s: number;
	label: string;
	fs_freeze_used?: boolean;
	ts_index: number;
}

export interface BackupTargetSetRequest {
	target_id?: string;
	kind: 'kopia-s3' | 'kopia-fs';
	s3_endpoint?: string;
	s3_bucket?: string;
	s3_region?: string;
	filesystem_path?: string;
	override_source_prefix?: string;
	cache_directory?: string;
	reason?: string;
	// Inline credentials — sent only when the operator types them in;
	// server fans out via SSH to every node, then runs kopia connect.
	s3_access_key?: string;
	s3_secret_key?: string;
	encryption_password?: string;
	force_password_overwrite?: boolean;
	// Multi-target replication
	is_mirror?: boolean;
	sync_to?: string[];
	delete_orphans?: boolean;
}

export interface BackupCredsStatus {
	nodes: Record<string, {
		has_password: boolean;
		creds: Record<string, boolean>;
		error?: string;
	}>;
}

export async function listBackupTargets(): Promise<{ targets: Record<string, BackupTarget> }> {
	return apiGet('/api/backup/targets');
}

export async function getBackupCredsStatus(): Promise<BackupCredsStatus> {
	return apiGet('/api/backup/credentials/status');
}

export async function setBackupTarget(req: BackupTargetSetRequest) {
	return apiPost('/api/backup/targets', req);
}

export async function removeBackupTarget(target_id: string, reason: string = '') {
	const q = reason ? `?reason=${encodeURIComponent(reason)}` : '';
	const r = await fetch(`/api/backup/targets/${encodeURIComponent(target_id)}${q}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function vmBackup(name: string, target_id: string = 'main', label: string = '') {
	return apiPost(`/api/vms/${encodeURIComponent(name)}/backup`, { target_id, label });
}

export async function vmBackupsList(name: string): Promise<{
	vm: string;
	backups: VmBackup[];
	last_backup_error?: { ts_index: number; target_id: string; reason: string };
	last_restore?: { ts_index: number; kopia_snapshot_id: string; target_id: string; dest_node: string };
	last_restore_error?: { ts_index: number; kopia_snapshot_id: string; target_id: string; reason: string };
}> {
	return apiGet(`/api/vms/${encodeURIComponent(name)}/backups`);
}

export interface ClusterBackupRow extends VmBackup {
	vm: string;
	vm_present: boolean;
}

export async function listAllBackups(): Promise<{ backups: ClusterBackupRow[] }> {
	return apiGet('/api/backups');
}

export async function vmRestore(name: string, body: {
	target_id?: string;
	kopia_snapshot_id: string;
	dest_node?: string;
	target_lv_path?: string;
}) {
	return apiPost(`/api/vms/${encodeURIComponent(name)}/restore`, body);
}

export async function vmBackupDelete(name: string, kopia_snapshot_id: string,
	body: { target_id?: string; reason?: string } = {}) {
	const r = await fetch(`/api/vms/${encodeURIComponent(name)}/backups/${encodeURIComponent(kopia_snapshot_id)}`, {
		method: 'DELETE',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

// ── Backup scheduling ────────────────────────────────────────────────────

export interface BackupSchedule {
	target_id: string;
	cron_expr: string;
	label_prefix: string;
	retention_count: number;
	set_at_index: number;
}

export async function setBackupSchedule(name: string, body: {
	target_id: string;
	cron_expr: string;
	label_prefix?: string;
	retention_count?: number;
}): Promise<{ status: string; log_index: number; vm: string; cron_expr: string; next_fires_utc: string[] }> {
	return apiPost(`/api/vms/${encodeURIComponent(name)}/backup-schedule`, body);
}

export async function removeBackupSchedule(name: string, reason: string = '') {
	const q = reason ? `?reason=${encodeURIComponent(reason)}` : '';
	const r = await fetch(`/api/vms/${encodeURIComponent(name)}/backup-schedule${q}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export async function cronPreview(expr: string, n: number = 5): Promise<{
	cron_expr: string; next_fires_utc: string[]
}> {
	const q = `?expr=${encodeURIComponent(expr)}&n=${n}`;
	const r = await fetch(`/api/cron/preview${q}`);
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

// ── Support / supportability ─────────────────────────────────────────────

export interface SupportCheck {
	id: string;
	label: string;
	status: 'ok' | 'warn' | 'fail';
	note: string;
	remediation: string;
}

export interface SupportChecksResponse {
	checks: SupportCheck[];
	overall: 'ok' | 'warn' | 'fail';
}

export async function getSupportChecks(): Promise<SupportChecksResponse> {
	return apiGet('/api/support/checks');
}

// ── Witness ─────────────────────────────────────────────────────────────────

export interface Witness {
	addr: string;
	witness_pubkey: string;
	encrypted_witness_key?: string;
	backend?: 'echo' | 'fileshare';
}

export async function listWitnesses(): Promise<{ witnesses: Record<string, Witness> }> {
	return apiGet('/api/witnesses');
}

export async function addWitness(body: {
	witness_id: string;
	addr: string;
	witness_pubkey?: string;
	backend?: 'echo' | 'fileshare';
}) {
	return apiPost('/api/witnesses', body);
}

export async function removeWitness(witness_id: string) {
	const r = await fetch(`/api/witnesses/${encodeURIComponent(witness_id)}`, { method: 'DELETE' });
	if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
	return r.json();
}

export interface WitnessCandidate { ip: string; echo_id: string; pubkey: string; }

export async function discoverWitnesses(): Promise<{ candidates: WitnessCandidate[] }> {
	return apiGet('/api/witnesses/discover');
}
