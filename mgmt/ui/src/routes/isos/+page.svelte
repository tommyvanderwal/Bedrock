<script lang="ts">
	import { onMount } from 'svelte';
	import { listIsos, uploadIso, deleteIso } from '$lib/api';

	let isos = $state<Array<{ name: string; size_bytes: number }>>([]);
	let loading = $state(true);
	let uploading = $state(false);
	let uploadPct = $state(0);
	let uploadName = $state('');
	let error = $state('');

	async function refresh() {
		loading = true;
		try {
			isos = await listIsos();
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	onMount(refresh);

	async function handleFile(e: Event) {
		const input = e.target as HTMLInputElement;
		if (!input.files?.length) return;
		const file = input.files[0];
		if (!file.name.toLowerCase().endsWith('.iso')) {
			error = 'File must end in .iso';
			return;
		}
		error = '';
		uploading = true;
		uploadPct = 0;
		uploadName = file.name;
		try {
			await uploadIso(file, (p) => (uploadPct = p));
			await refresh();
		} catch (e: any) {
			error = e.message;
		} finally {
			uploading = false;
			input.value = '';
		}
	}

	async function remove(name: string) {
		if (!confirm(`Delete ${name}?`)) return;
		try {
			await deleteIso(name);
			await refresh();
		} catch (e: any) {
			error = e.message;
		}
	}

	function fmtSize(bytes: number) {
		const mb = bytes / 1024 / 1024;
		if (mb < 1024) return `${mb.toFixed(0)} MB`;
		return `${(mb / 1024).toFixed(2)} GB`;
	}
</script>

<svelte:head><title>ISOs — Bedrock</title></svelte:head>

<div class="header">
	<h1>ISO library</h1>
	<span class="meta">{isos.length} {isos.length === 1 ? 'file' : 'files'}</span>
</div>

<div class="card upload-card">
	<h3>Upload ISO</h3>
	{#if uploading}
		<div class="progress-wrap">
			<div class="progress-label">{uploadName} — {uploadPct}%</div>
			<div class="progress-bar"><div class="progress-fill" style="width: {uploadPct}%"></div></div>
		</div>
	{:else}
		<label class="upload-btn">
			<input type="file" accept=".iso" onchange={handleFile} />
			<span>Choose .iso file</span>
		</label>
		<p class="hint">Files land in <code>/opt/bedrock/iso/</code> on the mgmt node and
			are NFS-exported read-only to the cluster LAN + DRBD ring.</p>
	{/if}
	{#if error}
		<div class="error">{error}</div>
	{/if}
</div>

{#if loading}
	<p class="muted">Loading…</p>
{:else if isos.length === 0}
	<p class="muted">No ISOs uploaded yet. Use the uploader above, or scp to
		<code>/opt/bedrock/iso/</code>.</p>
{:else}
	<table>
		<thead>
			<tr><th>Name</th><th>Size</th><th></th></tr>
		</thead>
		<tbody>
			{#each isos as iso}
				<tr>
					<td><code>{iso.name}</code></td>
					<td>{fmtSize(iso.size_bytes)}</td>
					<td><button class="btn-del" onclick={() => remove(iso.name)}>Delete</button></td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	.header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
	h1 { font-size: 22px; margin: 0; }
	.meta { color: #8b949e; font-size: 12px; }

	.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
	.card h3 { font-size: 12px; color: #8b949e; margin: 0 0 10px; text-transform: uppercase; letter-spacing: 0.5px; }

	.upload-btn { display: inline-block; padding: 8px 16px; border: 1px solid #1f6feb; border-radius: 6px; background: #1f6feb22; color: #58a6ff; cursor: pointer; font-size: 13px; }
	.upload-btn:hover { background: #1f6feb44; }
	.upload-btn input { display: none; }
	.hint { font-size: 11px; color: #8b949e; margin: 8px 0 0; }
	.hint code { background: #21262d; padding: 1px 4px; border-radius: 3px; }

	.progress-wrap { }
	.progress-label { font-size: 13px; margin-bottom: 6px; }
	.progress-bar { height: 10px; background: #21262d; border: 1px solid #30363d; border-radius: 4px; overflow: hidden; }
	.progress-fill { height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff); transition: width 0.15s; }

	.error { margin-top: 8px; padding: 8px 12px; border-left: 3px solid #f85149; background: #21262d; color: #f85149; font-size: 12px; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; }
	td code { font-family: ui-monospace, monospace; }

	.btn-del { padding: 4px 10px; border: 1px solid #f85149; background: transparent; color: #f85149; border-radius: 4px; font-size: 12px; cursor: pointer; }
	.btn-del:hover { background: #f8514922; }
	.muted { color: #8b949e; font-size: 13px; }
</style>
