<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { vmCreate, listIsos } from '$lib/api';

	let name = $state('');
	let vcpus = $state(2);
	let ramMb = $state(2048);
	let diskGb = $state(20);
	let priority = $state<'low' | 'normal' | 'high'>('normal');
	let iso = $state('');

	let isos = $state<Array<{ name: string; size_bytes: number }>>([]);
	let creating = $state(false);
	let error = $state('');

	onMount(async () => {
		try { isos = await listIsos(); } catch (e) { /* ignore */ }
	});

	const nameValid = $derived(/^[a-z][a-z0-9-]{1,30}[a-z0-9]$/.test(name));

	async function submit(e: Event) {
		e.preventDefault();
		if (!nameValid) { error = 'Invalid name'; return; }
		error = '';
		creating = true;
		try {
			const r = await vmCreate({
				name, vcpus, ram_mb: ramMb, disk_gb: diskGb, priority,
				iso: iso || null,
			}) as any;
			goto(`/vm/${r.name}`);
		} catch (e: any) {
			error = e.message;
			creating = false;
		}
	}
</script>

<svelte:head><title>New VM — Bedrock</title></svelte:head>

<div class="header">
	<h1>Create a VM</h1>
	<a href="/vms" class="back">← Back to VMs</a>
</div>

<form onsubmit={submit} class="card">
	<div class="row">
		<label class="field">
			<span class="lbl">Name</span>
			<input type="text" bind:value={name} placeholder="webapp2" required minlength="3" maxlength="32" />
			<span class="hint" class:ok={nameValid} class:bad={name && !nameValid}>
				lowercase letters, digits, dashes. Start with a letter.
			</span>
		</label>
	</div>

	<div class="row three">
		<label class="field">
			<span class="lbl">vCPUs</span>
			<input type="number" bind:value={vcpus} min="1" max="32" step="1" />
		</label>
		<label class="field">
			<span class="lbl">RAM (MB)</span>
			<input type="number" bind:value={ramMb} min="128" max="131072" step="128" />
			<span class="hint">{(ramMb / 1024).toFixed(ramMb >= 1024 ? 1 : 2)} GB</span>
		</label>
		<label class="field">
			<span class="lbl">Disk (GB)</span>
			<input type="number" bind:value={diskGb} min="1" max="2048" step="1" />
			<span class="hint">thin-provisioned</span>
		</label>
	</div>

	<div class="row">
		<div class="field">
			<span class="lbl">Priority</span>
			<div class="pri-group">
				{#each ['low','normal','high'] as p}
					<label class="pri-opt" class:active={priority === p}>
						<input type="radio" bind:group={priority} value={p} /> {p}
					</label>
				{/each}
			</div>
			<span class="hint">Stored in inventory. CPU/memory/start-order shares wire up
				to this in a follow-up.</span>
		</div>
	</div>

	<div class="row">
		<div class="field">
			<div class="lbl-row">
				<span class="lbl">Install ISO (optional)</span>
				<a href="/isos" class="upload-link">+ Upload new ISO</a>
			</div>
			<select bind:value={iso}>
				<option value="">— no ISO (blank VM) —</option>
				{#each isos as i}
					<option value={i.name}>{i.name} ({(i.size_bytes / 1024 / 1024 / 1024).toFixed(2)} GB)</option>
				{/each}
			</select>
			<span class="hint">
				{#if isos.length === 0}No ISOs yet — click <strong>Upload new ISO</strong> above.
				{:else}NFS-mounted at <code>/mnt/isos/</code> on every cluster node.
				{/if}
			</span>
		</div>
	</div>

	{#if error}
		<div class="error">{error}</div>
	{/if}

	<div class="actions">
		<button type="submit" class="btn-primary" disabled={!nameValid || creating}>
			{creating ? 'Creating…' : 'Create VM'}
		</button>
		<a href="/vms" class="btn-cancel">Cancel</a>
	</div>

	<p class="summary">
		Creates a <strong>cattle</strong> VM on the mgmt node with a thin-provisioned
		LV. Use the PET / ViPet checkboxes on the VM page later to add replication.
	</p>
</form>

<style>
	.header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
	h1 { font-size: 22px; margin: 0; }
	.back { font-size: 13px; color: #8b949e; margin-left: auto; }

	.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; max-width: 720px; }

	.row { margin-bottom: 16px; }
	.row.three { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }

	.field { display: flex; flex-direction: column; gap: 4px; }
	.lbl { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	.lbl-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
	.upload-link { font-size: 11px; color: #58a6ff; padding: 2px 8px; border: 1px solid #1f6feb; border-radius: 4px; }
	.upload-link:hover { background: #1f6feb22; text-decoration: none; }
	.hint { font-size: 11px; color: #6e7681; }
	.hint.ok { color: #3fb950; }
	.hint.bad { color: #d29922; }

	input[type="text"], input[type="number"], select {
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		color: #e6edf3; padding: 8px 12px; font-size: 14px;
	}
	input:focus, select:focus { outline: none; border-color: #58a6ff; }

	.pri-group { display: flex; gap: 8px; }
	.pri-opt {
		display: flex; align-items: center; gap: 6px;
		padding: 6px 14px; border: 1px solid #30363d; border-radius: 6px;
		background: #21262d; cursor: pointer; font-size: 13px; text-transform: capitalize;
	}
	.pri-opt.active { border-color: #58a6ff; background: #1f6feb22; color: #58a6ff; }
	.pri-opt input { accent-color: #58a6ff; }

	.error { margin: 8px 0; padding: 8px 12px; border-left: 3px solid #f85149;
		background: #21262d; color: #f85149; font-size: 12px; }

	.actions { display: flex; gap: 10px; align-items: center; margin-top: 20px; }
	.btn-primary { padding: 8px 18px; border: 1px solid #1a7f37; border-radius: 6px;
		background: #1a7f37; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer; }
	.btn-primary:hover { background: #2ea043; }
	.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
	.btn-cancel { padding: 8px 14px; font-size: 13px; color: #8b949e; }

	.summary { font-size: 12px; color: #8b949e; margin: 20px 0 0; line-height: 1.5; }
</style>
