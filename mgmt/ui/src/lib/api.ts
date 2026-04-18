/** REST API client for Bedrock management */

const BASE = '';  // same origin

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
	return apiPost(`/api/vms/${name}/shutdown`);
}

export async function vmPoweroff(name: string) {
	return apiPost(`/api/vms/${name}/poweroff`);
}

export async function vmMigrate(name: string, targetNode?: string) {
	return apiPost(`/api/vms/${name}/migrate`, targetNode ? { target_node: targetNode } : {});
}

export async function vmConvert(name: string, targetType: 'cattle' | 'pet' | 'vipet') {
	return apiPost(`/api/vms/${name}/convert`, { target_type: targetType });
}

export interface VMCreateRequest {
	name: string;
	vcpus: number;
	ram_mb: number;
	disk_gb: number;
	priority: 'low' | 'normal' | 'high';
	iso?: string | null;
}

export async function vmCreate(req: VMCreateRequest) {
	return apiPost('/api/vms/create', req);
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
	return apiPost(`/api/vms/${name}/resources`, body);
}

export async function setVmPriority(name: string, priority: 'low' | 'normal' | 'high') {
	return apiPost(`/api/vms/${name}/priority`, { priority });
}

export async function setVmCdrom(name: string, action: 'eject' | 'insert', iso?: string) {
	return apiPost(`/api/vms/${name}/cdrom`, { action, iso });
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
		xhr.open('POST', '/api/isos/upload');
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
