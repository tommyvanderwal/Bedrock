<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		listImports, uploadImport, convertImport, deleteImport, importCreateVM,
		type ImportJob,
	} from '$lib/api';

	let jobs = $state<ImportJob[]>([]);
	let uploading = $state(false);
	let uploadPct = $state(0);
	let uploadName = $state('');
	let error = $state('');
	let injectDrivers = $state(false);
	let poll: any = null;

	// VM-creation modal state
	let creatingFor = $state<string | null>(null);
	let createName = $state('');
	let createVcpus = $state(2);
	let createRam = $state(2048);
	let createPriority = $state<'low'|'normal'|'high'>('normal');
	let creating = $state(false);

	async function refresh() {
		try { jobs = await listImports(); } catch (e: any) { error = e.message; }
	}

	onMount(async () => {
		await refresh();
		poll = setInterval(refresh, 2000);
	});
	onDestroy(() => clearInterval(poll));

	async function handleFile(e: Event) {
		const input = e.target as HTMLInputElement;
		if (!input.files?.length) return;
		const file = input.files[0];
		const allowed = /\.(ova|ovf|vmdk|vhd|vhdx|qcow2|raw|img)$/i;
		if (!allowed.test(file.name)) {
			error = 'File must be .ova/.ovf/.vmdk/.vhd/.vhdx/.qcow2/.raw/.img';
			return;
		}
		error = ''; uploading = true; uploadPct = 0; uploadName = file.name;
		try {
			const job = await uploadImport(file, p => uploadPct = p);
			// Auto-start conversion once upload lands
			await convertImport(job.id, injectDrivers);
			await refresh();
		} catch (e: any) {
			error = e.message;
		} finally {
			uploading = false; input.value = '';
		}
	}

	async function retryConvert(id: string, retryWithDrivers = false) {
		try { await convertImport(id, retryWithDrivers); await refresh(); }
		catch (e: any) { error = e.message; }
	}
	async function remove(id: string) {
		if (!confirm(`Delete import ${id}? (uploaded + converted files will be removed)`)) return;
		try { await deleteImport(id); await refresh(); }
		catch (e: any) { error = e.message; }
	}

	function openCreateVM(j: ImportJob) {
		creatingFor = j.id;
		createName = (j.detected_name || j.original_name.replace(/\.[^.]+$/, ''))
			.toLowerCase().replace(/[^a-z0-9-]+/g, '-').slice(0, 32).replace(/^-+|-+$/g, '')
			|| 'imported';
		createVcpus = 2;
		createRam = 2048;
		createPriority = 'normal';
	}

	async function submitCreate() {
		if (!creatingFor) return;
		creating = true;
		try {
			await importCreateVM(creatingFor, {
				name: createName, vcpus: createVcpus, ram_mb: createRam,
				priority: createPriority,
			});
			creatingFor = null;
			await refresh();
		} catch (e: any) {
			error = e.message;
		} finally { creating = false; }
	}

	function fmtBytes(b: number | undefined): string {
		if (!b) return '-';
		const mb = b / 1024 / 1024;
		return mb < 1024 ? `${mb.toFixed(0)} MB` : `${(mb / 1024).toFixed(2)} GB`;
	}
	function fmtTime(s: string | undefined): string {
		return s ? s.replace('T', ' ') : '-';
	}
</script>

<svelte:head><title>Imports — Bedrock</title></svelte:head>

<div class="header">
	<h1>VM Imports</h1>
	<span class="meta">{jobs.length} job{jobs.length === 1 ? '' : 's'}</span>
</div>

<div class="card upload-card">
	<h3>Upload a disk image</h3>
	{#if uploading}
		<div class="progress-wrap">
			<div class="progress-label">{uploadName} — {uploadPct}%</div>
			<div class="progress-bar"><div class="progress-fill" style="width: {uploadPct}%"></div></div>
		</div>
	{:else}
		<label class="upload-btn">
			<input type="file" accept=".ova,.ovf,.vmdk,.vhd,.vhdx,.qcow2,.raw,.img" onchange={handleFile} />
			<span>Choose a file…</span>
		</label>
		<label class="inject">
			<input type="checkbox" bind:checked={injectDrivers} />
			<span>Windows guest — <strong>inject virtio drivers</strong> with virt-v2v
				<em>(viostor + NetKVM; slower, ~2–10 min)</em></span>
		</label>
		<p class="hint">
			VMware: .ova, .ovf, .vmdk · Hyper-V: .vhd, .vhdx · Generic: .qcow2, .raw, .img.
			Linux guests: format-only convert via qemu-img (~seconds).
			New VMs spawned from imports are Q35 + UEFI + clock=UTC.
		</p>
	{/if}
	{#if error}<div class="error">{error}</div>{/if}
</div>

{#if jobs.length === 0}
	<p class="muted">No imports yet. Upload a disk image above to get started.</p>
{:else}
	<table>
		<thead><tr>
			<th>Original file</th><th>Format</th><th>Size</th>
			<th>Status</th><th>Detected</th><th>When</th><th></th>
		</tr></thead>
		<tbody>
			{#each jobs as j (j.id)}
				<tr>
					<td>
						<code class="fn">{j.original_name}</code>
						<div class="sub">id: {j.id}</div>
					</td>
					<td><span class="fmt">{j.input_format}</span></td>
					<td>{fmtBytes(j.input_size_bytes)}</td>
					<td>
						<span class="status status-{j.status}">{j.status}</span>
						{#if j.status === 'failed' && j.error}
							<div class="sub err">{j.error}</div>
						{/if}
						{#if j.status === 'ready' && j.virtual_size_gb}
							<div class="sub">→ qcow2, {j.virtual_size_gb} GB virtual</div>
						{/if}
						{#if j.status === 'consumed' && j.consumed_as}
							<div class="sub">→ VM <a href="/vm/{j.consumed_as}"><strong>{j.consumed_as}</strong></a></div>
						{/if}
					</td>
					<td>
						{#if j.detected_name || j.detected_os_type || j.detected_firmware}
							<div>{j.detected_name || '-'}</div>
							<div class="sub">
								{j.detected_os_type || ''}
								{#if j.detected_firmware}· <strong>{j.detected_firmware.toUpperCase()}</strong>{/if}
								{#if j.injected_drivers}· drivers injected{/if}
							</div>
						{:else}<span class="muted">-</span>{/if}
					</td>
					<td class="sub">{fmtTime(j.created_at)}</td>
					<td class="actions">
						{#if j.status === 'ready'}
							<button class="btn-primary" onclick={() => openCreateVM(j)}>Create VM</button>
						{:else if j.status === 'failed'}
							<button class="btn-warn" onclick={() => retryConvert(j.id)}>Retry</button>
						{/if}
						{#if j.status !== 'consumed'}
							<button class="btn-del" onclick={() => remove(j.id)}>×</button>
						{:else}
							<button class="btn-del" onclick={() => remove(j.id)}>Clean up</button>
						{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

{#if creatingFor}
	<div class="modal-bg" onclick={() => creatingFor = null}>
		<div class="modal" onclick={(e) => e.stopPropagation()}>
			<h3>Create VM from import</h3>
			<label class="mf">
				<span class="ml">Name</span>
				<input type="text" bind:value={createName} />
			</label>
			<label class="mf">
				<span class="ml">vCPUs</span>
				<input type="number" bind:value={createVcpus} min="1" max="32" />
			</label>
			<label class="mf">
				<span class="ml">RAM (MB)</span>
				<input type="number" bind:value={createRam} min="128" max="131072" step="128" />
			</label>
			<label class="mf">
				<span class="ml">Priority</span>
				<select bind:value={createPriority}>
					<option value="low">low</option>
					<option value="normal">normal</option>
					<option value="high">high</option>
				</select>
			</label>
			<p class="hint">Imported as <strong>cattle</strong> (local disk). Use the Settings
				page to convert to PET / ViPet after it boots. Machine: Q35, UEFI, clock=UTC.</p>
			<div class="modal-actions">
				<button class="btn-primary" disabled={creating} onclick={submitCreate}>
					{creating ? 'Creating…' : 'Create VM'}
				</button>
				<button class="btn-cancel" onclick={() => creatingFor = null}>Cancel</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
	h1 { font-size: 22px; margin: 0; }
	.meta { color: #8b949e; font-size: 12px; }
	.muted { color: #8b949e; font-size: 13px; }

	.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
	.card h3 { font-size: 12px; color: #8b949e; margin: 0 0 10px; text-transform: uppercase; letter-spacing: 0.5px; }

	.upload-btn { display: inline-block; padding: 8px 16px; border: 1px solid #1f6feb; border-radius: 6px;
		background: #1f6feb22; color: #58a6ff; cursor: pointer; font-size: 13px; }
	.upload-btn:hover { background: #1f6feb44; }
	.upload-btn input { display: none; }
	.inject { display: block; margin-top: 8px; font-size: 12px; color: #c9d1d9; cursor: pointer; }
	.inject input { margin-right: 6px; }
	.inject em { color: #8b949e; font-style: normal; font-size: 11px; }
	.hint { font-size: 11px; color: #8b949e; margin: 8px 0 0; line-height: 1.5; }
	.progress-wrap { }
	.progress-label { font-size: 13px; margin-bottom: 6px; }
	.progress-bar { height: 10px; background: #21262d; border: 1px solid #30363d; border-radius: 4px; overflow: hidden; }
	.progress-fill { height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff); transition: width 0.15s; }
	.error { margin-top: 8px; padding: 8px 12px; border-left: 3px solid #f85149; background: #21262d; color: #f85149; font-size: 12px; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; vertical-align: top; }
	.fn { font-family: ui-monospace, monospace; font-size: 12px; }
	.sub { font-size: 11px; color: #8b949e; margin-top: 2px; }
	.sub.err { color: #f85149; }
	.fmt { background: #21262d; padding: 1px 8px; border-radius: 10px; font-size: 11px; text-transform: uppercase; }

	.status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
	.status-uploaded { background: #30363d; color: #c9d1d9; }
	.status-converting { background: #d2992244; color: #d29922; }
	.status-ready { background: #1a7f37; color: #fff; }
	.status-failed { background: #f85149; color: #fff; }
	.status-consumed { background: #1f6feb22; color: #58a6ff; }

	.actions { white-space: nowrap; }
	.btn-primary { padding: 5px 12px; border: 1px solid #1a7f37; border-radius: 4px; background: #1a7f37; color: #fff; font-size: 12px; cursor: pointer; margin-right: 6px; }
	.btn-primary:hover:not(:disabled) { background: #2ea043; }
	.btn-primary:disabled { opacity: 0.35; cursor: not-allowed; }
	.btn-warn { padding: 5px 12px; border: 1px solid #d29922; border-radius: 4px; background: transparent; color: #d29922; font-size: 12px; cursor: pointer; margin-right: 6px; }
	.btn-warn:hover { background: #d2992222; }
	.btn-del { padding: 5px 10px; border: 1px solid #30363d; border-radius: 4px; background: transparent; color: #8b949e; font-size: 12px; cursor: pointer; }
	.btn-del:hover { border-color: #f85149; color: #f85149; }
	.btn-cancel { padding: 7px 14px; font-size: 13px; color: #8b949e; background: transparent; border: none; cursor: pointer; }

	.modal-bg { position: fixed; inset: 0; background: #0008; display: flex; align-items: center; justify-content: center; z-index: 100; }
	.modal { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 24px; min-width: 420px; max-width: 500px; }
	.modal h3 { margin: 0 0 16px; font-size: 14px; color: #e6edf3; text-transform: none; letter-spacing: 0; }
	.mf { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
	.ml { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	.mf input, .mf select {
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		color: #e6edf3; padding: 7px 10px; font-size: 13px;
	}
	.modal-actions { display: flex; gap: 8px; align-items: center; margin-top: 16px; }
</style>
