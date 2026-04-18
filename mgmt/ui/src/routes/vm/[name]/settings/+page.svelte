<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { nodes } from '$lib/stores';
	import {
		getVmSettings, setVmResources, setVmPriority, setVmCdrom,
		vmConvert, listIsos,
		type VMSettings,
	} from '$lib/api';

	let vmName = $derived($page.params.name);

	let loaded = $state(false);
	let settings = $state<VMSettings | null>(null);
	let error = $state('');
	let busy = $state('');  // action label while a save is in flight

	// Editable form state (copied from settings on load)
	let vcpus = $state(1);
	let ramMb = $state(1024);
	let diskGb = $state(10);

	let priority = $state<'low' | 'normal' | 'high'>('normal');

	// CDROM + ISO list
	let isos = $state<Array<{ name: string; size_bytes: number }>>([]);
	let cdromToInsert = $state('');

	// HA state
	let nodeCount = $derived(Object.keys($nodes).length);
	let currentType = $derived(
		settings?.drbd_resource
			? (settings.name ? inferType(settings) : 'cattle')
			: 'cattle'
	);
	function inferType(_s: VMSettings): string {
		// Derived from cluster view — fetched separately below via resource peer count.
		return _s.drbd_resource ? 'pet' : 'cattle';
	}
	// We'll instead carry HA level via a separate load
	let haType = $state<'cattle' | 'pet' | 'vipet'>('cattle');

	async function load() {
		error = '';
		try {
			settings = await getVmSettings(vmName);
			vcpus = settings.vcpus;
			ramMb = settings.ram_mb;
			diskGb = settings.disk_gb;
			priority = settings.priority;
			cdromToInsert = '';
			// Infer HA type from existing /api/cluster data in stores (defined_on length)
			const clusterRes = await fetch('/api/cluster').then(r => r.json());
			const vm = clusterRes.vms?.[vmName];
			const n = vm?.defined_on?.length ?? 1;
			haType = n >= 3 ? 'vipet' : n === 2 ? 'pet' : 'cattle';
			// ISO list
			const all = await listIsos();
			isos = all.filter(i => i.name !== 'virtio-win.iso');
			loaded = true;
		} catch (e: any) {
			error = e.message;
			loaded = true;
		}
	}

	onMount(load);

	let resourcesDirty = $derived(
		settings && (vcpus !== settings.vcpus || ramMb !== settings.ram_mb || diskGb !== settings.disk_gb)
	);

	async function saveResources() {
		if (!settings || !resourcesDirty) return;
		busy = 'Saving resources...';
		error = '';
		try {
			const patch: any = {};
			if (vcpus !== settings.vcpus) patch.vcpus = vcpus;
			if (ramMb !== settings.ram_mb) patch.ram_mb = ramMb;
			if (diskGb !== settings.disk_gb) patch.disk_gb = diskGb;
			await setVmResources(vmName, patch);
			await load();
		} catch (e: any) { error = e.message; }
		finally { busy = ''; }
	}

	async function changePriority(e: Event) {
		const p = (e.target as HTMLInputElement).value as 'low' | 'normal' | 'high';
		busy = 'Applying priority...';
		try {
			await setVmPriority(vmName, p);
			priority = p;
		} catch (e: any) { error = e.message; }
		finally { busy = ''; }
	}

	async function ejectCdrom() {
		busy = 'Ejecting CDROM...';
		try {
			await setVmCdrom(vmName, 'eject');
			await load();
		} catch (e: any) { error = e.message; }
		finally { busy = ''; }
	}

	async function insertCdrom() {
		if (!cdromToInsert) return;
		busy = `Inserting ${cdromToInsert}...`;
		try {
			await setVmCdrom(vmName, 'insert', cdromToInsert);
			await load();
		} catch (e: any) { error = e.message; }
		finally { busy = ''; }
	}

	async function togglePet(e: Event) {
		const wantHA = (e.target as HTMLInputElement).checked;
		const target = wantHA ? 'pet' : 'cattle';
		if (!confirm(`Convert ${vmName} to ${target.toUpperCase()}? VM stays online.`)) {
			(e.target as HTMLInputElement).checked = !wantHA;
			return;
		}
		busy = `Converting → ${target}...`;
		try {
			await vmConvert(vmName, target);
			await load();
		} catch (e: any) { error = e.message; }
		finally { busy = ''; }
	}

	async function toggleViPet(e: Event) {
		const wantViPet = (e.target as HTMLInputElement).checked;
		const target = wantViPet ? 'vipet' : 'pet';
		if (!confirm(`Convert ${vmName} to ${target.toUpperCase()}?`)) {
			(e.target as HTMLInputElement).checked = !wantViPet;
			return;
		}
		busy = `Converting → ${target}...`;
		try {
			await vmConvert(vmName, target);
			await load();
		} catch (e: any) { error = e.message; }
		finally { busy = ''; }
	}
</script>

<svelte:head><title>{vmName} — Settings</title></svelte:head>

<div class="breadcrumb">
	<a href="/vm/{vmName}">← {vmName}</a>  /  <strong>Settings</strong>
</div>

{#if busy}<div class="banner busy">{busy}</div>{/if}
{#if error}<div class="banner err">{error}</div>{/if}

{#if !loaded}
	<p class="muted">Loading…</p>
{:else if !settings}
	<p>VM not found: {vmName}</p>
{:else}

<div class="card">
	<h3>Resources</h3>
	<div class="row three">
		<label class="field">
			<span class="lbl">vCPUs</span>
			<input type="number" bind:value={vcpus} min="1" max="32" step="1" />
			<span class="hint reboot">⟳ applies on next reboot</span>
		</label>
		<label class="field">
			<span class="lbl">RAM (MB)</span>
			<input type="number" bind:value={ramMb} min="128" max="131072" step="128" />
			<span class="hint reboot">⟳ applies on next reboot</span>
		</label>
		<label class="field">
			<span class="lbl">Disk (GB)</span>
			<input type="number" bind:value={diskGb} min={settings.disk_gb} max="2048" step="1" />
			<span class="hint live">✓ grow applies live (guest may rescan)</span>
		</label>
	</div>
	<div class="row">
		<button class="btn-primary" disabled={!resourcesDirty || !!busy}
			onclick={saveResources}>
			{resourcesDirty ? 'Save resources' : 'No changes'}
		</button>
		<span class="muted">
			Current: {settings.vcpus} vCPU · {settings.ram_mb} MB · {settings.disk_gb} GB
		</span>
	</div>
	<p class="note">Disk can only grow; shrinking is not supported. vCPU and RAM
		changes are queued and take effect on the next reboot of the VM.</p>
</div>

<div class="card">
	<h3>Priority  <span class="hint live">✓ live (cgroup cpu_shares)</span></h3>
	<div class="pri-group">
		{#each ['low','normal','high'] as p}
			<label class="pri-opt" class:active={priority === p}>
				<input type="radio" bind:group={priority} value={p} onchange={changePriority} />
				{p}
			</label>
		{/each}
	</div>
	<p class="note">
		low = 256, normal = 1024 (libvirt default), high = 4096 shares.
		Current cpu_shares on host: <code>{settings.cpu_shares ?? '—'}</code>.
	</p>
</div>

<div class="card">
	<h3>HA replication  <span class="hint live">✓ conversion is online</span></h3>
	<label class="ha-check" class:disabled={nodeCount < 2 || !!busy}>
		<input type="checkbox" checked={haType !== 'cattle'}
			disabled={nodeCount < 2 || !!busy}
			onchange={togglePet} />
		<span><strong>PET</strong> (HA — 2-way DRBD)</span>
		{#if nodeCount < 2}<em class="hint">need ≥ 2 nodes</em>{/if}
	</label>
	<label class="ha-check nested" class:disabled={haType === 'cattle' || nodeCount < 3 || !!busy}>
		<input type="checkbox" checked={haType === 'vipet'}
			disabled={haType === 'cattle' || nodeCount < 3 || !!busy}
			onchange={toggleViPet} />
		<span><strong>ViPet</strong> (VeryHA — 3-way DRBD)</span>
		{#if nodeCount < 3}<em class="hint">need ≥ 3 nodes</em>
		{:else if haType === 'cattle'}<em class="hint">enable PET first</em>{/if}
	</label>
	<p class="note">Current: <code>{haType}</code>. DRBD resource:
		<code>{settings.drbd_resource || '—'}</code>.</p>
</div>

<div class="card">
	<h3>CD-ROM  <span class="hint live">✓ live eject / insert</span></h3>
	{#if !settings.cdrom_slot}
		<p class="muted">This VM has no CDROM device. (It was created without an ISO.)
		Add one by recreating the VM with an ISO selected.</p>
	{:else}
		<div class="row">
			<span class="muted">Currently inserted:</span>
			<code class="iso-name">{settings.cdrom_iso ?? '(empty)'}</code>
			{#if settings.cdrom_iso}
				<button class="btn-warn" disabled={!!busy} onclick={ejectCdrom}>Eject</button>
			{/if}
		</div>
		<div class="row">
			<select bind:value={cdromToInsert}>
				<option value="">— pick an ISO to insert —</option>
				{#each isos as i}
					<option value={i.name} disabled={i.name === settings.cdrom_iso}>
						{i.name}
					</option>
				{/each}
			</select>
			<button class="btn-primary" disabled={!cdromToInsert || !!busy}
				onclick={insertCdrom}>Insert</button>
		</div>
		<p class="note">Slot: <code>{settings.cdrom_slot}</code>. The virtio-win
			driver ISO is attached separately and not listed here.</p>
	{/if}
</div>

{/if}

<style>
	.breadcrumb { font-size: 13px; color: #8b949e; margin-bottom: 14px; }
	.breadcrumb strong { color: #e6edf3; }

	.banner { padding: 8px 14px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
	.banner.busy { background: #1f6feb22; border: 1px solid #1f6feb; color: #58a6ff; }
	.banner.err  { background: #f8514922; border: 1px solid #f85149; color: #f85149; }

	.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
		padding: 16px 20px; margin-bottom: 14px; max-width: 860px; }
	.card h3 { font-size: 12px; color: #8b949e; margin: 0 0 12px;
		text-transform: uppercase; letter-spacing: 0.5px;
		display: flex; align-items: baseline; gap: 10px; }

	.row { display: flex; gap: 12px; align-items: center; margin: 10px 0; flex-wrap: wrap; }
	.row.three { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }

	.field { display: flex; flex-direction: column; gap: 4px; }
	.lbl { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	.hint { font-size: 11px; color: #8b949e; text-transform: none; letter-spacing: 0; font-weight: 400; }
	.hint.reboot { color: #d29922; }
	.hint.live { color: #3fb950; }

	input[type="number"], select {
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		color: #e6edf3; padding: 7px 10px; font-size: 14px; min-width: 140px;
	}
	input:focus, select:focus { outline: none; border-color: #58a6ff; }

	.pri-group { display: flex; gap: 8px; }
	.pri-opt { display: flex; align-items: center; gap: 6px;
		padding: 6px 14px; border: 1px solid #30363d; border-radius: 6px;
		background: #21262d; cursor: pointer; font-size: 13px; text-transform: capitalize; }
	.pri-opt.active { border-color: #58a6ff; background: #1f6feb22; color: #58a6ff; }

	.ha-check { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 6px 0; cursor: pointer; }
	.ha-check.nested { margin-left: 20px; }
	.ha-check.disabled { opacity: 0.4; cursor: not-allowed; }

	.btn-primary { padding: 7px 16px; border: 1px solid #1a7f37; border-radius: 6px;
		background: #1a7f37; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; }
	.btn-primary:hover:not(:disabled) { background: #2ea043; }
	.btn-primary:disabled { opacity: 0.35; cursor: not-allowed; }
	.btn-warn { padding: 7px 14px; border: 1px solid #d29922; border-radius: 6px;
		background: transparent; color: #d29922; font-size: 13px; cursor: pointer; }
	.btn-warn:hover:not(:disabled) { background: #d2992222; }

	.muted { color: #8b949e; font-size: 13px; }
	.iso-name { background: #21262d; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
	.note { font-size: 11px; color: #6e7681; margin: 10px 0 0; line-height: 1.5; }
	code { background: #21262d; padding: 1px 6px; border-radius: 3px; font-size: 11px; }
</style>
