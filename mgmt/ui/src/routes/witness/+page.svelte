<script lang="ts">
	import { onMount } from 'svelte';
	import {
		listWitnesses, addWitness, removeWitness, discoverWitnesses,
		type Witness, type WitnessCandidate
	} from '$lib/api';

	let witnesses = $state<Record<string, Witness>>({});
	let loading = $state(true);
	let error = $state('');
	let notice = $state('');

	// add form
	let f_id = $state('');
	let f_addr = $state('');
	let f_backend = $state<'echo' | 'fileshare'>('echo');
	let f_pubkey = $state('');
	let adding = $state(false);

	// discovery
	let candidates = $state<WitnessCandidate[]>([]);
	let discovering = $state(false);

	const ids = $derived(Object.keys(witnesses).sort());

	async function refresh() {
		loading = true;
		try {
			witnesses = (await listWitnesses()).witnesses || {};
			error = '';
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	onMount(refresh);

	async function add() {
		error = ''; notice = '';
		if (!f_id.trim()) { error = 'Witness id is required'; return; }
		if (f_backend === 'echo') {
			if (!f_addr.trim()) { error = 'Address is required (host or host:port)'; return; }
			const pk = f_pubkey.trim().toLowerCase();
			if (!/^[0-9a-f]{64}$/.test(pk)) {
				error = "An Echo witness needs its 64-hex X25519 public key";
				return;
			}
		} else {
			// fileshare: addr is an absolute directory the share is mounted at
			if (!f_addr.trim().startsWith('/')) {
				error = 'A fileshare witness needs the absolute path of the mounted share (e.g. /mnt/witness)';
				return;
			}
		}
		adding = true;
		try {
			const r = await addWitness({
				witness_id: f_id.trim(),
				addr: f_addr.trim(),
				witness_pubkey: f_pubkey.trim(),
				backend: f_backend,
			});
			notice = `Witness ${r.witness_id} added (${r.backend} ${r.addr}).`;
			f_id = ''; f_addr = ''; f_pubkey = '';
			await refresh();
		} catch (e: any) {
			error = e.message;
		} finally {
			adding = false;
		}
	}

	async function remove(id: string) {
		if (!confirm(`Remove witness ${id}? This lowers the quorum vote count.`)) return;
		error = ''; notice = '';
		try {
			await removeWitness(id);
			notice = `Witness ${id} removed.`;
			await refresh();
		} catch (e: any) {
			error = e.message;
		}
	}

	async function discover() {
		discovering = true;
		error = '';
		try {
			candidates = (await discoverWitnesses()).candidates || [];
			if (candidates.length === 0) notice = 'No Bedrock hosts found on the LAN via mDNS.';
		} catch (e: any) {
			error = e.message;
		} finally {
			discovering = false;
		}
	}

	function useCandidate(c: WitnessCandidate) {
		f_addr = c.ip;
		f_backend = 'echo';
		notice = `Filled address ${c.ip} — paste the Echo's pubkey and Add.`;
	}
</script>

<svelte:head><title>Witnesses — Bedrock</title></svelte:head>

<div class="header">
	<h1>Witnesses</h1>
	<span class="meta">{ids.length} configured · each counts as 1 quorum vote (a node is 100)</span>
</div>

{#if notice}<div class="notice">{notice}</div>{/if}
{#if error}<div class="error">{error}</div>{/if}

<div class="card">
	<h3>Add witness</h3>
	<div class="form-grid">
		<label>Id<input placeholder="echo-rack-1" bind:value={f_id} /></label>
		<label>Backend
			<select bind:value={f_backend}>
				<option value="echo">BedRock Echo (UDP)</option>
				<option value="fileshare">Fileshare (mounted dir)</option>
			</select>
		</label>
		<label>{f_backend === 'echo' ? 'Address' : 'Share path'}<input
			placeholder={f_backend === 'echo' ? 'host or host:12321' : '/mnt/witness (absolute path)'}
			bind:value={f_addr} spellcheck="false" /></label>
		{#if f_backend === 'echo'}
			<label class="wide">Echo public key (X25519, 64 hex)
				<input placeholder="64 hex chars" bind:value={f_pubkey} spellcheck="false" />
			</label>
		{/if}
	</div>
	<button class="btn-add" onclick={add} disabled={adding}>{adding ? 'Adding…' : 'Add witness'}</button>
	<p class="hint">A witness raises the quorum bar by one vote and only counts toward
		failover once it is reachable + valid — a configured-but-unreachable witness
		is split-brain-safe (it makes failover harder, never easier). A
		<strong>fileshare</strong> witness is a directory you have mounted the same
		NFS/SMB/object share at on <em>every</em> node; Bedrock writes its slot files
		there. The path must exist and be writable on each node before it can vote.</p>
</div>

<div class="card">
	<h3>Discover on network (mDNS)</h3>
	<button class="btn-ghost" onclick={discover} disabled={discovering}>{discovering ? 'Scanning…' : 'Scan LAN'}</button>
	{#if candidates.length > 0}
		<table class="mini">
			<thead><tr><th>IP</th><th>Name</th><th>Node</th><th></th></tr></thead>
			<tbody>
				{#each candidates as c}
					<tr>
						<td><code>{c.ip}</code></td><td>{c.name || '—'}</td><td>{c.node || '—'}</td>
						<td><button class="btn-ghost sm" onclick={() => useCandidate(c)}>Use</button></td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

{#if loading}
	<p class="muted">Loading…</p>
{:else if ids.length === 0}
	<p class="muted">No witnesses configured. A 2-node cluster needs at least one
		witness to safely break a tie on failover.</p>
{:else}
	<table>
		<thead><tr><th>Id</th><th>Backend</th><th>Address</th><th>Public key</th><th></th></tr></thead>
		<tbody>
			{#each ids as id}
				<tr>
					<td><code>{id}</code></td>
					<td>{witnesses[id].backend || 'echo'}</td>
					<td><code>{witnesses[id].addr}</code></td>
					<td class="pk">{witnesses[id].witness_pubkey ? witnesses[id].witness_pubkey.slice(0, 16) + '…' : '—'}</td>
					<td><button class="btn-del" onclick={() => remove(id)}>Remove</button></td>
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

	.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; margin-bottom: 12px; }
	.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #8b949e; }
	.form-grid label.wide { grid-column: 1 / -1; }
	.form-grid input, .form-grid select { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 7px 9px; color: #c9d1d9; font-size: 13px; }
	.form-grid input:focus, .form-grid select:focus { outline: none; border-color: #1f6feb; }

	.btn-add { padding: 8px 16px; border: 1px solid #1f6feb; border-radius: 6px; background: #1f6feb22; color: #58a6ff; cursor: pointer; font-size: 13px; }
	.btn-add:hover { background: #1f6feb44; }
	.btn-add:disabled { opacity: 0.5; cursor: default; }
	.btn-ghost { padding: 6px 12px; border: 1px solid #30363d; border-radius: 6px; background: transparent; color: #c9d1d9; cursor: pointer; font-size: 12px; }
	.btn-ghost:hover { background: #21262d; }
	.btn-ghost.sm { padding: 3px 9px; font-size: 11px; }

	.hint { font-size: 11px; color: #8b949e; margin: 8px 0 0; line-height: 1.5; }

	.notice { margin-bottom: 12px; padding: 8px 12px; border-left: 3px solid #1f6feb; background: #21262d; color: #58a6ff; font-size: 12px; }
	.error { margin-bottom: 12px; padding: 8px 12px; border-left: 3px solid #f85149; background: #21262d; color: #f85149; font-size: 12px; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	table.mini { margin-top: 10px; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; }
	td code { font-family: ui-monospace, monospace; }
	td.pk { font-family: ui-monospace, monospace; color: #8b949e; }

	.btn-del { padding: 4px 10px; border: 1px solid #f85149; background: transparent; color: #f85149; border-radius: 4px; font-size: 12px; cursor: pointer; }
	.btn-del:hover { background: #f8514922; }
	.muted { color: #8b949e; font-size: 13px; }
</style>
