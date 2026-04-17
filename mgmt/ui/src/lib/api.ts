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
