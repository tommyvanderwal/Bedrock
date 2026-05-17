<script lang="ts">
	import { onMount } from 'svelte';
	import { nodes, vms, witness, events, pendingJoins } from '$lib/stores';
	import { vmStart, vmShutdown, vmPoweroff, vmMigrate, apiGet, apiPost } from '$lib/api';
	import Chart from '$lib/Chart.svelte';
	import LogList from '$lib/LogList.svelte';

	// Tracks which pending request is currently being approved/rejected
	// so the buttons can disable themselves while the round-trip is in
	// flight (avoid double-clicks racing).
	let busyRequest = $state<string | null>(null);

	async function approveJoin(rid: string) {
		busyRequest = rid;
		try { await apiPost('/api/join/approve', { request_id: rid }); }
		finally { busyRequest = null; }
	}

	async function rejectJoin(rid: string) {
		busyRequest = rid;
		try { await apiPost('/api/join/reject',
			{ request_id: rid, reason: 'denied by operator' }); }
		finally { busyRequest = null; }
	}

	let actionStatus = $state('');
	let nodeMetrics = $state<any>({});
	let vmMetrics = $state<any>({});
	let seededLogs = $state<any[]>([]);
	let liveLogs = $state<any[]>([]);
	let recentLogs = $derived([...liveLogs, ...seededLogs].slice(0, 50));

	async function fetchMetrics() {
		try {
			nodeMetrics = await apiGet('/api/metrics/nodes?hours=1&step=30s');
			vmMetrics = await apiGet('/api/metrics/vms?hours=1&step=30s');
		} catch (e) { /* metrics not available yet */ }
		try {
			if (seededLogs.length === 0) {
				const r = await apiGet('/api/logs?limit=30&hours=1');
			seededLogs = r.sort((a: any, b: any) => (b._time || '').localeCompare(a._time || ''));
			}
		} catch (e) { /* keep whatever we have */ }
	}

	onMount(() => {
		fetchMetrics();
		const interval = setInterval(fetchMetrics, 30000);
		const unsub = events.subscribe(all => { liveLogs = all; });
		return () => { clearInterval(interval); unsub(); };
	});

	async function doAction(fn: () => Promise<any>, label: string) {
		actionStatus = `${label}...`;
		try {
			const r = await fn();
			actionStatus = `${label}: ${r.status || 'done'}${r.duration_s ? ` (${r.duration_s}s)` : ''}`;
			setTimeout(() => actionStatus = '', 5000);
		} catch (e: any) {
			actionStatus = `${label} failed: ${e.message}`;
			setTimeout(() => actionStatus = '', 8000);
		}
	}
</script>

<svelte:head><title>Bedrock — Cluster Overview</title></svelte:head>

<!-- Status bar -->
<div class="status-bar">
	{#each Object.entries($nodes) as [name, node]}
		<span class="status-item">
			<span class="dot" class:green={node.online} class:red={!node.online}></span>
			{name}
		</span>
	{/each}
	<span class="status-item">
		<span class="dot" class:green={!$witness.error} class:red={!!$witness.error}></span>
		Witness
		{#if $witness.witness_uptime_secs}
			<span class="meta">({Math.round($witness.witness_uptime_secs / 60)}m)</span>
		{/if}
	</span>
	{#if actionStatus}
		<span class="toast">{actionStatus}</span>
	{/if}
</div>

<!-- Nodes -->
<h2>Hosts</h2>

{#if $pendingJoins.length > 0}
	<div class="pending-list" data-testid="pending-list">
		{#each $pendingJoins as req (req.request_id)}
			<div class="pending-card" data-testid="join-pending">
				<div class="pending-left">
					<span class="pending-dot"></span>
					<div class="pending-text">
						<div class="pending-line1">
							<span class="pending-tag">Pending join</span>
							<span class="pending-name">{req.node_name}</span>
							<span class="pending-host">{req.host}</span>
						</div>
						<div class="pending-fp" data-testid="join-fingerprint">
							{req.fingerprint}
						</div>
					</div>
				</div>
				<div class="pending-actions">
					<button class="btn-reject" disabled={busyRequest === req.request_id}
						onclick={() => rejectJoin(req.request_id)}
						data-testid="join-reject">Reject</button>
					<button class="btn-approve" disabled={busyRequest === req.request_id}
						onclick={() => approveJoin(req.request_id)}
						data-testid="join-approve">
						{busyRequest === req.request_id ? 'Approving…' : 'Approve'}
					</button>
				</div>
			</div>
		{/each}
		<p class="pending-hint">
			Compare each fingerprint with what the joining node printed on its own
			console. Approve only when they match exactly.
		</p>
	</div>
{/if}

<div class="grid">
	{#each Object.entries($nodes) as [name, node]}
		<div class="card" class:offline={!node.online}>
			<div class="card-header">
				<h3><a href="/node/{name}">{name}</a></h3>
				<span class="tag" class:online={node.online} class:offline={!node.online}>
					{node.online ? 'Online' : 'Offline'}
				</span>
			</div>
			<div class="stat"><span class="label">Host</span><span>{node.host}</span></div>
			<div class="stat"><span class="label">Kernel</span><span>{node.kernel || '-'}</span></div>
			<div class="stat"><span class="label">Load</span><span>{node.load || '-'}</span></div>
			<div class="stat">
				<span class="label">Memory</span>
				<span>{node.mem_used_mb}/{node.mem_total_mb} MB ({node.mem_total_mb ? Math.round(node.mem_used_mb / node.mem_total_mb * 100) : 0}%)</span>
			</div>
			<div class="stat"><span class="label">VMs</span><span>{node.running_vms?.join(', ') || 'none'}</span></div>
			<div class="stat"><span class="label">Up since</span><span>{node.uptime_since || '-'}</span></div>
			<div class="card-actions">
				<a href={node.cockpit_url} target="_blank" class="btn small">Cockpit</a>
			</div>
		</div>
	{/each}
</div>

<!-- Node Metrics Charts -->
{#if nodeMetrics.cpu && Object.keys(nodeMetrics.cpu).length > 0}
<h2>Node Metrics (1h)</h2>
<div class="charts-grid">
	<Chart title="CPU %" data={nodeMetrics.cpu} series={Object.keys(nodeMetrics.cpu)} width={440} height={140} />
	<Chart title="Memory %" data={nodeMetrics.mem} series={Object.keys(nodeMetrics.mem || {})} width={440} height={140} />
	<Chart title="Network RX (bytes/s)" data={nodeMetrics.net_rx} series={Object.keys(nodeMetrics.net_rx || {})} width={440} height={140} />
</div>
{/if}

{#if vmMetrics.cpu && Object.keys(vmMetrics.cpu).length > 0}
<h2>VM Metrics (1h)</h2>
<div class="charts-grid">
	<Chart title="VM CPU %" data={vmMetrics.cpu} series={Object.keys(vmMetrics.cpu)} width={440} height={140} />
	<Chart title="Disk Write IOPS" data={vmMetrics.disk_wr_iops} series={Object.keys(vmMetrics.disk_wr_iops || {})} width={440} height={140} />
	<Chart title="Disk Write Latency (ms)" data={vmMetrics.disk_wr_lat} series={Object.keys(vmMetrics.disk_wr_lat || {})} width={440} height={140} />
</div>
{/if}

<!-- VMs -->
<h2>Virtual Machines</h2>
<table>
	<thead>
		<tr>
			<th>VM</th>
			<th>State</th>
			<th>Node</th>
			<th>Backup</th>
			<th>DRBD</th>
			<th>Actions</th>
		</tr>
	</thead>
	<tbody>
		{#each Object.entries($vms) as [name, vm]}
			<tr>
				<td>
					<strong><a href="/vm/{name}">{name}</a></strong>
					<br><small class="muted">{vm.drbd_resource}</small>
				</td>
				<td>
					<span class="tag" class:running={vm.state === 'running'} class:off={vm.state !== 'running'}>
						{vm.state}
					</span>
				</td>
				<td>{vm.running_on || '-'}</td>
				<td>{vm.backup_node || '-'}</td>
				<td>
					{#if vm.drbd_role}
						<span class="tag" class:primary={vm.drbd_role === 'Primary'} class:secondary={vm.drbd_role !== 'Primary'}>
							{vm.drbd_role}
						</span>
						<span class="tag" class:uptodate={vm.drbd_disk === 'UpToDate'} class:syncing={vm.drbd_disk !== 'UpToDate'}>
							{vm.drbd_disk}
						</span>
						{#if vm.drbd_sync_pct}
							<small>{vm.drbd_sync_pct}%</small>
						{/if}
					{:else}
						<span class="muted">-</span>
					{/if}
				</td>
				<td>
					<div class="actions">
						<button class="btn start" disabled={vm.state === 'running'}
							onclick={() => doAction(() => vmStart(name), `Start ${name}`)}>Start</button>
						<button class="btn migrate" disabled={vm.state !== 'running'}
							onclick={() => doAction(() => vmMigrate(name), `Migrate ${name}`)}>Migrate</button>
						<button class="btn stop" disabled={vm.state !== 'running'}
							onclick={() => doAction(() => vmShutdown(name), `Shutdown ${name}`)}>Shutdown</button>
						<button class="btn poweroff" disabled={vm.state !== 'running'}
							onclick={() => doAction(() => vmPoweroff(name), `Power off ${name}`)}>Power Off</button>
						{#if vm.state === 'running' && vm.vnc_ws_url}
							<a href="/console/{name}" class="btn console">Console</a>
						{/if}
					</div>
				</td>
			</tr>
		{/each}
	</tbody>
</table>

<!-- Logs -->
<h2>Recent Logs</h2>
<LogList logs={recentLogs} />

<style>
	h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin: 20px 0 10px; }

	/* Pending join cards — full-width entries above the host grid so
	   the operator can read the fingerprint comfortably and act on it
	   inline. Amber accent matches the sidebar badge. */
	.pending-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
	.pending-card {
		background: #d2992211;
		border: 1px solid #d2992255;
		border-left: 3px solid #d29922;
		border-radius: 6px;
		padding: 12px 16px;
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 14px;
	}
	.pending-left { display: flex; gap: 12px; align-items: center; min-width: 0; flex: 1; }
	.pending-dot {
		width: 10px; height: 10px; border-radius: 50%;
		background: #d29922; flex-shrink: 0;
		animation: pulse 1.8s infinite;
	}
	.pending-text { min-width: 0; flex: 1; }
	.pending-line1 { display: flex; gap: 12px; align-items: baseline; font-size: 14px; }
	.pending-tag {
		background: #d29922; color: #000;
		font-size: 10px; font-weight: 700;
		padding: 2px 7px; border-radius: 4px;
		text-transform: uppercase; letter-spacing: 0.5px;
	}
	.pending-name { color: #e6edf3; font-weight: 600; }
	.pending-host { color: #8b949e; }
	.pending-fp {
		margin-top: 4px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 13px;
		color: #c9d1d9;
		word-break: break-all;
	}
	.pending-actions { display: flex; gap: 8px; flex-shrink: 0; }
	.pending-actions button {
		padding: 7px 16px;
		border-radius: 5px;
		font-weight: 600;
		font-size: 13px;
		cursor: pointer;
	}
	.btn-reject {
		background: #21262d; border: 1px solid #f8514955; color: #f85149;
	}
	.btn-reject:hover:enabled { background: #f8514922; }
	.btn-approve {
		background: #238636; border: 1px solid #2ea043; color: white;
	}
	.btn-approve:hover:enabled { background: #2ea043; }
	.pending-actions button:disabled { opacity: 0.6; cursor: wait; }
	.pending-hint { font-size: 12px; color: #8b949e; margin: 4px 0 0 0; }
	@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
	.charts-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 12px; margin-bottom: 8px; }

	.status-bar { display: flex; gap: 16px; align-items: center; margin-bottom: 16px; font-size: 13px; flex-wrap: wrap; }
	.status-item { display: flex; gap: 6px; align-items: center; }
	.dot { width: 8px; height: 8px; border-radius: 50%; }
	.dot.green { background: #3fb950; }
	.dot.red { background: #f85149; }
	.meta { font-size: 11px; color: #8b949e; }
	.muted { color: #8b949e; }
	.toast { margin-left: auto; background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 4px 12px; font-size: 12px; }

	.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; min-height: 220px; }
	.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
	.card.offline { border-color: #f85149; opacity: 0.7; }
	.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
	.card-header h3 { font-size: 15px; margin: 0; }
	.card-actions { margin-top: 10px; }
	.stat { display: flex; justify-content: space-between; font-size: 13px; margin: 3px 0; }
	.stat .label { color: #8b949e; }

	.tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
	.tag.running, .tag.online, .tag.uptodate { background: #1a7f37; color: #fff; }
	.tag.off, .tag.offline { background: #6e7681; color: #fff; }
	.tag.primary { background: #1f6feb; color: #fff; }
	.tag.secondary { background: #30363d; color: #8b949e; }
	.tag.syncing { background: #d29922; color: #000; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; }
	tr:hover td { background: #1c2128; }

	.actions { display: flex; gap: 6px; flex-wrap: wrap; }
	.btn { padding: 4px 10px; border: 1px solid #30363d; border-radius: 6px; background: #21262d; color: #e6edf3; font-size: 12px; cursor: pointer; }
	.btn:hover { background: #30363d; }
	.btn:disabled { opacity: 0.3; cursor: not-allowed; }
	.btn.start { border-color: #1a7f37; color: #3fb950; }
	.btn.migrate { border-color: #1f6feb; color: #58a6ff; }
	.btn.stop { border-color: #d29922; color: #d29922; }
	.btn.poweroff { border-color: #f85149; color: #f85149; }
	.btn.console { border-color: #8957e5; color: #bc8cff; }
	.btn.small { font-size: 11px; padding: 2px 8px; }

</style>
