<script lang="ts">
	import { vms } from '$lib/stores';
	import { vmStart, vmShutdown, vmPoweroff, vmMigrate } from '$lib/api';

	let actionStatus = $state('');

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

	let rows = $derived(
		Object.entries($vms).sort((a, b) => a[0].localeCompare(b[0]))
	);
	function vmType(vm: any): string {
		const n = (vm.defined_on || []).length;
		return n >= 3 ? 'vipet' : n === 2 ? 'pet' : 'cattle';
	}
</script>

<svelte:head><title>VMs — Bedrock</title></svelte:head>

<div class="header">
	<h1>Virtual Machines</h1>
	<span class="meta">{rows.length} total</span>
	{#if actionStatus}
		<span class="toast">{actionStatus}</span>
	{/if}
</div>

<table>
	<thead>
		<tr>
			<th>Name</th>
			<th>State</th>
			<th>Type</th>
			<th>Running on</th>
			<th>DRBD</th>
			<th>Actions</th>
		</tr>
	</thead>
	<tbody>
		{#each rows as [name, vm]}
			<tr>
				<td>
					<a href="/vm/{name}"><strong>{name}</strong></a>
					{#if vm.drbd_resource}<div class="sub">{vm.drbd_resource}</div>{/if}
				</td>
				<td>
					<span class="tag" class:running={vm.state === 'running'} class:off={vm.state !== 'running'}>
						{vm.state}
					</span>
				</td>
				<td><span class="type type-{vmType(vm)}">{vmType(vm)}</span></td>
				<td>
					{#if vm.running_on}
						<a href="/node/{vm.running_on}">{vm.running_on.split('.')[0]}</a>
					{:else}-{/if}
				</td>
				<td>
					{#if vm.drbd_role}
						<span class="tag" class:primary={vm.drbd_role === 'Primary'} class:secondary={vm.drbd_role !== 'Primary'}>
							{vm.drbd_role}
						</span>
						<span class="tag" class:uptodate={vm.drbd_disk === 'UpToDate'} class:syncing={vm.drbd_disk && vm.drbd_disk !== 'UpToDate'}>
							{vm.drbd_disk}
						</span>
						{#if vm.drbd_sync_pct}<small>{vm.drbd_sync_pct}%</small>{/if}
					{:else}
						<span class="muted">-</span>
					{/if}
				</td>
				<td>
					<div class="actions">
						<button class="btn start" disabled={vm.state === 'running'}
							onclick={() => doAction(() => vmStart(name), `Start ${name}`)}>Start</button>
						<button class="btn migrate"
							disabled={vm.state !== 'running' || vmType(vm) === 'cattle'}
							title={vmType(vm) === 'cattle' ? 'Enable PET first' : ''}
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
		{#if rows.length === 0}
			<tr><td colspan="6" class="muted centered">no VMs yet</td></tr>
		{/if}
	</tbody>
</table>

<style>
	.header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
	h1 { font-size: 22px; margin: 0; }
	.meta { color: #8b949e; font-size: 12px; }
	.toast { margin-left: auto; background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 4px 12px; font-size: 12px; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; vertical-align: middle; }
	tr:hover td { background: #1c2128; }
	.sub { font-size: 11px; color: #8b949e; margin-top: 2px; }

	.tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
	.tag.running, .tag.uptodate { background: #1a7f37; color: #fff; }
	.tag.off { background: #6e7681; color: #fff; }
	.tag.primary { background: #1f6feb; color: #fff; }
	.tag.secondary { background: #30363d; color: #8b949e; }
	.tag.syncing { background: #d29922; color: #000; }

	.type { font-size: 11px; padding: 1px 6px; border-radius: 4px; text-transform: uppercase; }
	.type-cattle { background: #30363d; color: #e6edf3; }
	.type-pet { background: #1f6feb; color: #fff; }
	.type-vipet { background: #8957e5; color: #fff; }

	.muted { color: #8b949e; }
	.centered { text-align: center; padding: 24px; }

	.actions { display: flex; gap: 6px; flex-wrap: wrap; }
	.btn { padding: 4px 10px; border: 1px solid #30363d; border-radius: 6px; background: #21262d; color: #e6edf3; font-size: 12px; cursor: pointer; }
	.btn:hover { background: #30363d; }
	.btn:disabled { opacity: 0.3; cursor: not-allowed; }
	.btn.start { border-color: #1a7f37; color: #3fb950; }
	.btn.migrate { border-color: #1f6feb; color: #58a6ff; }
	.btn.stop { border-color: #d29922; color: #d29922; }
	.btn.poweroff { border-color: #f85149; color: #f85149; }
	.btn.console { border-color: #8957e5; color: #bc8cff; }
</style>
