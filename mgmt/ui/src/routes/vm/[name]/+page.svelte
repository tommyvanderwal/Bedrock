<script lang="ts">
	import { page } from '$app/stores';
	import { vms, nodes, events } from '$lib/stores';
	import { goto } from '$app/navigation';
	import { apiGet, vmStart, vmShutdown, vmPoweroff, vmMigrate, vmDelete } from '$lib/api';
	import Chart from '$lib/Chart.svelte';
	import LogList from '$lib/LogList.svelte';

	let vmName = $derived($page.params.name);
	let vm = $derived($vms[vmName]);
	let node = $derived(vm?.running_on ? $nodes[vm.running_on] : null);

	let metrics = $state<any>({});
	let seededLogs = $state<any[]>([]);
	let liveLogs = $state<any[]>([]);
	// Live mgmt events mentioning this VM + seeded history.
	let logs = $derived([...liveLogs, ...seededLogs].slice(0, 50));
	let actionStatus = $state('');
	let converting = $state(false);

	let replicaCount = $derived(vm?.defined_on?.length ?? 1);
	let isPet = $derived(replicaCount >= 2);  // drives Migrate button enabled/disabled

	// Delete confirmation — Proxmox-style modal. Click Delete → modal shows
	// the VM name; operator types "delete" (literal word) to enable the
	// final Delete button. No upfront confirm() popup.
	let deleteModalOpen = $state(false);
	let deleteTyped = $state('');

	function openDeleteModal() {
		deleteTyped = '';
		deleteModalOpen = true;
	}
	function closeDeleteModal() {
		deleteModalOpen = false;
		deleteTyped = '';
	}
	async function confirmDelete() {
		if (deleteTyped !== 'delete') return;
		deleteModalOpen = false;
		converting = true;
		try {
			await vmDelete(vmName);
			goto('/vms');
		} catch (e: any) {
			actionStatus = `Delete failed: ${e.message}`;
			setTimeout(() => actionStatus = '', 8000);
			converting = false;
		}
	}

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

	async function fetchMetrics(name: string) {
		try {
			const allVm = await apiGet('/api/metrics/vms?hours=1&step=15s');
			const filtered: any = {};
			for (const [metricName, series] of Object.entries(allVm)) {
				filtered[metricName] = {};
				if (series && typeof series === 'object') {
					for (const [label, points] of Object.entries(series as any)) {
						if (label.includes(name)) filtered[metricName][label] = points;
					}
				}
			}
			metrics = filtered;
		} catch (e) { /* not ready */ }
	}

	async function fetchSeededLogs(name: string) {
		try {
			const r = await apiGet(`/api/logs/vm/${name}?limit=50&hours=4`);
			seededLogs = r.sort((a: any, b: any) => (b._time || '').localeCompare(a._time || ''));
		} catch (e) { /* keep whatever we have */ }
	}

	// Rebinds on route param change — previous setInterval + store subscription
	// are cleaned up so a navigation /vm/A → /vm/B doesn't leave stale filters.
	$effect(() => {
		const name = vmName;  // reactive dep
		metrics = {};
		seededLogs = [];
		liveLogs = [];
		fetchMetrics(name);
		fetchSeededLogs(name);
		const iv = setInterval(() => fetchMetrics(name), 15000);
		const unsub = events.subscribe(all => {
			liveLogs = all.filter((e: any) => (e._msg || '').includes(name));
		});
		return () => { clearInterval(iv); unsub(); };
	});
</script>

<svelte:head><title>VM: {vmName}</title></svelte:head>

<div class="breadcrumb">
	<a href="/">Overview</a> / <strong>{vmName}</strong>
</div>

{#if vm}
<div class="header">
	<h1>{vmName}</h1>
	<span class="tag" class:running={vm.state === 'running'} class:off={vm.state !== 'running'}>
		{vm.state}
	</span>
	{#if actionStatus}
		<span class="toast">{actionStatus}</span>
	{/if}
</div>

<div class="info-grid">
	<div class="info-card">
		<h3>Virtual Machine</h3>
		<div class="stat"><span>State</span><span>{vm.state}</span></div>
		<div class="stat"><span>Running on</span>
			<span>{#if vm.running_on}<a href="/node/{vm.running_on}">{vm.running_on}</a>{:else}-{/if}</span>
		</div>
		<div class="stat"><span>Backup node</span>
			<span>{#if vm.backup_node}<a href="/node/{vm.backup_node}">{vm.backup_node}</a>{:else}-{/if}</span>
		</div>
	</div>
	<div class="info-card">
		<h3>DRBD Storage</h3>
		<div class="stat"><span>Resource</span><span>{vm.drbd_resource}</span></div>
		<div class="stat"><span>Role</span>
			<span class="tag" class:primary={vm.drbd_role === 'Primary'} class:secondary={vm.drbd_role !== 'Primary'}>
				{vm.drbd_role || '-'}
			</span>
		</div>
		<div class="stat"><span>Disk</span>
			<span class="tag" class:uptodate={vm.drbd_disk === 'UpToDate'} class:syncing={vm.drbd_disk && vm.drbd_disk !== 'UpToDate'}>
				{vm.drbd_disk || '-'}
			</span>
		</div>
		<div class="stat"><span>Peer disk</span><span>{vm.drbd_peer_disk || '-'}</span></div>
		{#if vm.drbd_sync_pct}
			<div class="stat"><span>Sync</span><span>{vm.drbd_sync_pct}%</span></div>
		{/if}
	</div>
	<div class="info-card">
		<h3>Actions</h3>
		<div class="actions">
			<button class="btn start" disabled={vm.state === 'running'}
				onclick={() => doAction(() => vmStart(vmName), 'Start')}>Start</button>
			<button class="btn migrate" disabled={vm.state !== 'running' || !isPet}
				title={!isPet ? 'Migration requires PET or ViPet (DRBD replication)' : ''}
				onclick={() => doAction(() => vmMigrate(vmName), 'Migrate')}>Live Migrate</button>
			<button class="btn stop" disabled={vm.state !== 'running'}
				onclick={() => doAction(() => vmShutdown(vmName), 'Shutdown')}>Shutdown</button>
			<button class="btn poweroff" disabled={vm.state !== 'running'}
				onclick={() => doAction(() => vmPoweroff(vmName), 'Power Off')}>Power Off</button>
			{#if vm.state === 'running' && vm.vnc_ws_url}
				<a href="/console/{vmName}" class="btn console">Open Console</a>
			{/if}
			<a href="/vm/{vmName}/settings" class="btn settings">Settings</a>
			<button class="btn delete" disabled={converting}
				title="Stop, tear down DRBD, remove LVs, drop from inventory"
				onclick={openDeleteModal}>Delete VM</button>
		</div>
	</div>
</div>

{#if Object.values(metrics.cpu || {}).length > 0}
<h2>Performance (1h)</h2>
<div class="charts-grid">
	<Chart title="CPU %" data={metrics.cpu || {}} series={Object.keys(metrics.cpu || {})} width={440} height={160} />
	<Chart title="Disk Write IOPS" data={metrics.disk_wr_iops || {}} series={Object.keys(metrics.disk_wr_iops || {})} width={440} height={160} />
	<Chart title="Disk Write Latency (ms)" data={metrics.disk_wr_lat || {}} series={Object.keys(metrics.disk_wr_lat || {})} width={440} height={160} />
	<Chart title="Disk Read IOPS" data={metrics.disk_rd_iops || {}} series={Object.keys(metrics.disk_rd_iops || {})} width={440} height={160} />
</div>
{/if}

<h2>Recent Logs</h2>
<LogList {logs} />

{:else}
<p>VM not found: {vmName}</p>
{/if}

{#if deleteModalOpen}
	<div class="modal-bg" role="presentation" onclick={closeDeleteModal}>
		<div class="modal" role="dialog" aria-modal="true"
			onclick={(e) => e.stopPropagation()}
			onkeydown={(e) => { if (e.key === 'Escape') closeDeleteModal(); }}>
			<h3>Delete VM</h3>
			<p class="del-vm-name">{vmName}</p>
			<p class="del-warn">
				Stops the VM, tears down any DRBD resource, and removes the disk LVs
				on every node. This cannot be undone.
			</p>
			<label class="del-label">
				Type <code>delete</code> to confirm:
				<input type="text" bind:value={deleteTyped} autocomplete="off"
					autofocus
					onkeydown={(e) => { if (e.key === 'Enter' && deleteTyped === 'delete') confirmDelete(); }} />
			</label>
			<div class="del-actions">
				<button class="btn" onclick={closeDeleteModal}>Cancel</button>
				<button class="btn btn-danger" disabled={deleteTyped !== 'delete'}
					onclick={confirmDelete}>Delete</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.breadcrumb { font-size: 13px; color: #8b949e; margin-bottom: 12px; }
	.header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
	h1 { font-size: 24px; margin: 0; }
	h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin: 20px 0 10px; }
	h3 { font-size: 13px; color: #8b949e; margin: 0 0 8px; text-transform: uppercase; }
	.tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
	.tag.running, .tag.uptodate { background: #1a7f37; color: #fff; }
	.tag.off { background: #6e7681; color: #fff; }
	.tag.primary { background: #1f6feb; color: #fff; }
	.tag.secondary { background: #30363d; color: #8b949e; }
	.tag.syncing { background: #d29922; color: #000; }
	.toast { margin-left: auto; background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 4px 12px; font-size: 12px; }

	.info-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
	.info-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
	.stat { display: flex; justify-content: space-between; font-size: 13px; margin: 4px 0; }
	.stat span:first-child { color: #8b949e; }
	.charts-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 12px; }

	.actions { display: flex; gap: 8px; flex-wrap: wrap; }
	.btn { padding: 6px 14px; border: 1px solid #30363d; border-radius: 6px; background: #21262d; color: #e6edf3; font-size: 13px; cursor: pointer; }
	.btn:hover { background: #30363d; text-decoration: none; }
	.btn:disabled { opacity: 0.3; cursor: not-allowed; }
	.btn.start { border-color: #1a7f37; color: #3fb950; }
	.btn.migrate { border-color: #1f6feb; color: #58a6ff; }
	.btn.stop { border-color: #d29922; color: #d29922; }
	.btn.poweroff { border-color: #f85149; color: #f85149; }
	.btn.console { border-color: #8957e5; color: #bc8cff; }
	.btn.settings { border-color: #6e7681; color: #c9d1d9; margin-left: auto; }
	.btn.settings:hover { background: #30363d; text-decoration: none; }
	.btn.delete { border-color: #f85149; color: #f85149; }
	.btn.delete:hover:not(:disabled) { background: #f8514922; }

	.ha-check { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 6px 0; cursor: pointer; }
	.ha-check.nested { margin-left: 20px; }
	.ha-check.disabled { opacity: 0.4; cursor: not-allowed; }
	.ha-check input { accent-color: #58a6ff; cursor: inherit; }
	.ha-check .hint { font-style: italic; color: #8b949e; font-size: 11px; }
	.ha-note { font-size: 11px; color: #8b949e; margin: 8px 0 0; }
	.ha-note code { background: #21262d; padding: 1px 6px; border-radius: 3px; color: #e6edf3; }

	/* Delete confirmation modal (Proxmox-style) */
	.modal-bg {
		position: fixed; inset: 0; background: #0008; backdrop-filter: blur(2px);
		display: flex; align-items: center; justify-content: center; z-index: 1000;
	}
	.modal {
		background: #161b22; border: 1px solid #30363d; border-radius: 8px;
		padding: 24px; min-width: 420px; max-width: 560px;
	}
	.modal h3 {
		font-size: 16px; color: #e6edf3; text-transform: none; letter-spacing: 0;
		margin: 0 0 8px;
	}
	.del-vm-name {
		font-family: ui-monospace, SFMono-Regular, monospace;
		font-size: 18px; font-weight: 600; color: #e6edf3;
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		padding: 8px 12px; margin: 4px 0 12px;
	}
	.del-warn {
		font-size: 13px; color: #d29922; background: #d2992211;
		border-left: 3px solid #d29922; padding: 8px 12px; margin: 0 0 16px;
	}
	.del-label { display: block; font-size: 13px; color: #c9d1d9; margin-bottom: 12px; }
	.del-label code { background: #21262d; padding: 1px 6px; border-radius: 3px; color: #f85149; font-weight: 600; }
	.del-label input {
		display: block; width: 100%; margin-top: 6px;
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		padding: 8px 12px; color: #e6edf3; font-size: 14px;
		font-family: ui-monospace, SFMono-Regular, monospace;
	}
	.del-label input:focus { outline: none; border-color: #58a6ff; }
	.del-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
	.btn-danger { border-color: #f85149; color: #fff; background: #da3633; }
	.btn-danger:hover:not(:disabled) { background: #f85149; }
	.btn-danger:disabled { background: #30363d; color: #6e7681; border-color: #30363d; }
</style>
