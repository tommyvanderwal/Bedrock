<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { nodes, vms, events } from '$lib/stores';
	import { apiGet } from '$lib/api';
	import Chart from '$lib/Chart.svelte';
	import LogList from '$lib/LogList.svelte';

	let nodeName = $derived($page.params.name);
	let node = $derived($nodes[nodeName]);

	// VMs defined on this node (running + stopped)
	let nodeVms = $derived(
		Object.values($vms).filter(v =>
			(v.defined_on || []).includes(nodeName) || v.running_on === nodeName
		)
	);
	let runningHere = $derived(nodeVms.filter(v => v.running_on === nodeName));
	let secondaryHere = $derived(nodeVms.filter(v => v.running_on !== nodeName));

	let metrics = $state<any>({});
	let seededLogs = $state<any[]>([]);
	let liveLogs = $state<any[]>([]);
	let logs = $derived([...liveLogs, ...seededLogs].slice(0, 50));

	function filterByHost(series: any, host: string) {
		const out: any = {};
		for (const [k, v] of Object.entries(series || {})) {
			if (k.includes(host)) out[k] = v;
		}
		return out;
	}

	async function fetchData() {
		if (!node) return;
		const host = node.host;
		try {
			const nm = await apiGet('/api/metrics/nodes?hours=1&step=15s');
			metrics = {
				cpu: filterByHost(nm.cpu, host),
				mem: filterByHost(nm.mem, host),
				net_rx: filterByHost(nm.net_rx, host),
				net_tx: filterByHost(nm.net_tx, host),
			};
		} catch (e) { /* not ready */ }
		try {
			if (seededLogs.length === 0) {
				const r = await apiGet(`/api/logs/node/${nodeName}?limit=50&hours=4`);
				seededLogs = r.sort((a: any, b: any) => (b._time || '').localeCompare(a._time || ''));
			}
		} catch (e) { /* keep whatever we have */ }
	}

	onMount(() => {
		fetchData();
		const i = setInterval(fetchData, 15000);
		const short = nodeName.split('.')[0];
		const unsub = events.subscribe(all => {
			liveLogs = all.filter((e: any) =>
				(e.hostname || '').includes(short) || (e._msg || '').includes(short));
		});
		return () => { clearInterval(i); unsub(); };
	});

	function memPct(n: any): number {
		return n.mem_total_mb ? Math.round(n.mem_used_mb / n.mem_total_mb * 100) : 0;
	}
</script>

<svelte:head><title>Node: {nodeName}</title></svelte:head>

{#if node}
<div class="node-header">
	<h1>{nodeName}</h1>
	<span class="tag" class:online={node.online} class:offline={!node.online}>
		{node.online ? 'Online' : 'Offline'}
	</span>
	<span class="meta">{node.host}</span>
</div>

<div class="summary">
	<div class="metric-tile">
		<div class="label">Load (1m)</div>
		<div class="big">{node.load || '-'}</div>
	</div>
	<div class="metric-tile">
		<div class="label">Memory</div>
		<div class="big">{memPct(node)}%</div>
		<div class="sub">{node.mem_used_mb} / {node.mem_total_mb} MB</div>
	</div>
	<div class="metric-tile">
		<div class="label">VMs running</div>
		<div class="big">{runningHere.length}</div>
		<div class="sub">{nodeVms.length} defined</div>
	</div>
	<div class="metric-tile">
		<div class="label">Kernel</div>
		<div class="kernel">{node.kernel || '-'}</div>
		<div class="sub">up since {node.uptime_since || '-'}</div>
	</div>
</div>

<div class="info-grid">
	<div class="card">
		<h3>Running VMs ({runningHere.length})</h3>
		{#if runningHere.length}
			<table class="mini">
				<thead><tr><th>Name</th><th>DRBD</th><th></th></tr></thead>
				<tbody>
					{#each runningHere as vm}
						<tr>
							<td><a href="/vm/{vm.name}">{vm.name}</a></td>
							<td>
								{#if vm.drbd_role}
									<span class="tag" class:primary={vm.drbd_role === 'Primary'}
										class:secondary={vm.drbd_role !== 'Primary'}>{vm.drbd_role}</span>
								{:else}
									<span class="muted">cattle</span>
								{/if}
							</td>
							<td>
								{#if vm.state === 'running' && vm.vnc_ws_url}
									<a href="/console/{vm.name}" class="btn-inline">console</a>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<div class="muted">none</div>
		{/if}
	</div>

	<div class="card">
		<h3>Replicas from other hosts ({secondaryHere.length})</h3>
		{#if secondaryHere.length}
			<table class="mini">
				<thead><tr><th>VM</th><th>Primary</th><th>Role</th></tr></thead>
				<tbody>
					{#each secondaryHere as vm}
						<tr>
							<td><a href="/vm/{vm.name}">{vm.name}</a></td>
							<td>{vm.running_on || '-'}</td>
							<td><span class="tag secondary">Secondary</span></td>
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<div class="muted">none</div>
		{/if}
	</div>

	<div class="card">
		<h3>Links</h3>
		<div class="links">
			<a href={node.cockpit_url} target="_blank" class="btn">Cockpit</a>
			<a href="http://{node.host}:9100/metrics" target="_blank" class="btn">node_exporter</a>
			<a href="http://{node.host}:9177/metrics" target="_blank" class="btn">vm_exporter</a>
		</div>
	</div>
</div>

{#if Object.keys(metrics.cpu || {}).length > 0}
<h2>Metrics (1h)</h2>
<div class="charts">
	<Chart title="CPU %" data={metrics.cpu} series={Object.keys(metrics.cpu || {})} width={440} height={160} />
	<Chart title="Memory %" data={metrics.mem} series={Object.keys(metrics.mem || {})} width={440} height={160} />
	<Chart title="Network RX (B/s)" data={metrics.net_rx} series={Object.keys(metrics.net_rx || {})} width={440} height={160} />
	<Chart title="Network TX (B/s)" data={metrics.net_tx} series={Object.keys(metrics.net_tx || {})} width={440} height={160} />
</div>
{/if}

<h2>Recent Logs</h2>
<LogList {logs} />

{:else}
<p>Node not found: {nodeName}</p>
{/if}

<style>
	.node-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
	h1 { font-size: 22px; margin: 0; }
	.meta { font-size: 12px; color: #8b949e; margin-left: auto; }
	h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin: 24px 0 10px; }
	h3 { font-size: 12px; color: #8b949e; margin: 0 0 10px; text-transform: uppercase; letter-spacing: 0.5px; }

	.tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
	.tag.online { background: #1a7f37; color: #fff; }
	.tag.offline { background: #f85149; color: #fff; }
	.tag.primary { background: #1f6feb; color: #fff; }
	.tag.secondary { background: #30363d; color: #8b949e; }
	.muted { color: #8b949e; font-size: 13px; }

	.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 16px; }
	.metric-tile { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; }
	.metric-tile .label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	.metric-tile .big { font-size: 26px; font-weight: 600; margin-top: 4px; }
	.metric-tile .sub { font-size: 11px; color: #8b949e; margin-top: 2px; }
	.metric-tile .kernel { font-size: 13px; font-weight: 500; margin-top: 4px; word-break: break-all; }

	.info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
	.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }

	table.mini { width: 100%; border-collapse: collapse; font-size: 13px; }
	table.mini th { text-align: left; padding: 4px 6px; font-size: 11px; color: #8b949e; text-transform: uppercase; border-bottom: 1px solid #30363d; }
	table.mini td { padding: 6px; border-bottom: 1px solid #21262d; }
	table.mini tr:last-child td { border-bottom: none; }

	.charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 12px; }

	.links { display: flex; gap: 8px; flex-wrap: wrap; }
	.btn { padding: 4px 10px; border: 1px solid #30363d; border-radius: 6px; background: #21262d; color: #e6edf3; font-size: 12px; }
	.btn:hover { background: #30363d; text-decoration: none; }
	.btn-inline { font-size: 11px; color: #58a6ff; }
</style>
