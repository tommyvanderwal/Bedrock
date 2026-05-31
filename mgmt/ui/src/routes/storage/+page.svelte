<script lang="ts">
	import { onMount } from 'svelte';
	import {
		listStorageEndpoints, setStorageEndpoint, removeStorageEndpoint,
		testStorageEndpoint, enableWitnessOnEndpoint, enableBackupOnEndpoint,
		type StorageEndpoint, type StorageEndpointInput
	} from '$lib/api';

	// The published, meaningless default kopia password — NOT a secret. Shown so the
	// operator sees the repo is effectively unencrypted and can opt into a real key.
	const PUBLIC_REPO_PASSWORD = 'PublicBedrockNotAPasswordBecauseKopiaForcesThisEvenWhenNotNeeded';

	let endpoints = $state<StorageEndpoint[]>([]);
	let loading = $state(true);
	let error = $state('');
	let notice = $state('');

	// ── add/configure form ──────────────────────────────────────────────
	let f_id = $state('');
	let f_type = $state<'s3' | 'smb' | 'nfs'>('s3');
	let f_label = $state('');
	// s3
	let f_s3_endpoint = $state('');
	let f_s3_bucket = $state('');
	let f_s3_region = $state('');
	let f_s3_prefix = $state('');
	let f_s3_access_key = $state('');
	let f_s3_secret_key = $state('');
	let f_s3_disable_tls = $state(false);
	let f_s3_disable_tls_verification = $state(false);
	// smb/nfs
	let f_fs_server = $state('');
	let f_fs_share = $state('');
	let f_fs_options = $state('');
	let f_fs_username = $state('');
	let f_fs_password = $state('');

	// the two activation boxes
	let enable_backup = $state(false);
	let enable_witness = $state(false);
	let enc_password = $state(PUBLIC_REPO_PASSWORD);   // revealed under "enable backups"

	let testing = $state(false);
	let test_ok = $state<boolean | null>(null);
	let test_reason = $state('');
	let saving = $state(false);

	function formInput(): StorageEndpointInput {
		return {
			endpoint_id: f_id.trim(), type: f_type, label: f_label.trim(),
			s3_endpoint: f_s3_endpoint.trim(), s3_bucket: f_s3_bucket.trim(),
			s3_region: f_s3_region.trim(), s3_prefix: f_s3_prefix.trim(),
			s3_access_key: f_s3_access_key.trim(),
			s3_secret_key: f_s3_secret_key ? f_s3_secret_key : undefined,
			s3_disable_tls: f_s3_disable_tls,
			s3_disable_tls_verification: f_s3_disable_tls_verification,
			fs_server: f_fs_server.trim(), fs_share: f_fs_share.trim(),
			fs_options: f_fs_options.trim(), fs_username: f_fs_username.trim(),
			fs_password: f_fs_password ? f_fs_password : undefined,
		};
	}

	async function refresh() {
		loading = true;
		try {
			endpoints = (await listStorageEndpoints()).endpoints || [];
			error = '';
		} catch (e: any) { error = e.message; }
		finally { loading = false; }
	}
	onMount(refresh);

	async function runTest() {
		error = ''; test_ok = null; test_reason = '';
		if (!validate()) return;
		testing = true;
		try {
			const r = await testStorageEndpoint({
				...formInput(),
				usage: enable_witness ? 'witness' : 'kopia',
			});
			test_ok = r.ok; test_reason = r.reason;
		} catch (e: any) { error = e.message; }
		finally { testing = false; }
	}

	function validate(): boolean {
		if (!f_id.trim()) { error = 'Endpoint id is required'; return false; }
		if (f_type === 's3') {
			if (!f_s3_endpoint.trim() || !f_s3_bucket.trim()) {
				error = 'S3 needs an endpoint URL and a bucket'; return false;
			}
		} else {
			if (!f_fs_server.trim() || !f_fs_share.trim()) {
				error = `${f_type.toUpperCase()} needs a server and a share/export path`; return false;
			}
		}
		return true;
	}

	async function save() {
		error = ''; notice = '';
		if (!validate()) return;
		if (!enable_backup && !enable_witness) {
			error = 'Tick at least one box — enable for backups and/or as a witness';
			return;
		}
		saving = true;
		try {
			const eid = f_id.trim();
			await setStorageEndpoint(formInput());
			const did: string[] = [];
			if (enable_backup) {
				// Send "" when the encryption field is still the public default →
				// the repo uses Bedrock's published constant (effectively unencrypted).
				const pw = enc_password === PUBLIC_REPO_PASSWORD ? '' : enc_password;
				await enableBackupOnEndpoint(eid, { encryption_password: pw });
				did.push('backups');
			}
			if (enable_witness) {
				await enableWitnessOnEndpoint(eid, {});
				did.push('witness');
			}
			notice = `Endpoint ${eid} saved + enabled for ${did.join(' + ')}.`;
			resetForm();
			await refresh();
		} catch (e: any) { error = e.message; }
		finally { saving = false; }
	}

	function resetForm() {
		f_id = ''; f_label = '';
		f_s3_endpoint = ''; f_s3_bucket = ''; f_s3_region = ''; f_s3_prefix = '';
		f_s3_access_key = ''; f_s3_secret_key = '';
		f_s3_disable_tls = false; f_s3_disable_tls_verification = false;
		f_fs_server = ''; f_fs_share = ''; f_fs_options = ''; f_fs_username = ''; f_fs_password = '';
		enable_backup = false; enable_witness = false; enc_password = PUBLIC_REPO_PASSWORD;
		test_ok = null; test_reason = '';
	}

	async function remove(eid: string) {
		if (!confirm(`Remove storage endpoint ${eid}? It must not be in use.`)) return;
		error = ''; notice = '';
		try {
			await removeStorageEndpoint(eid);
			notice = `Endpoint ${eid} removed.`;
			await refresh();
		} catch (e: any) { error = e.message; }
	}

	function usageText(ep: StorageEndpoint): string {
		const u = ep.usage || { backup_targets: [], witnesses: [] };
		const parts: string[] = [];
		if (u.backup_targets?.length) parts.push(`backup (${u.backup_targets.join(', ')})`);
		if (u.witnesses?.length) parts.push(`witness (${u.witnesses.join(', ')})`);
		return parts.length ? parts.join(' · ') : '— not activated';
	}
</script>

<svelte:head><title>Storage — Bedrock</title></svelte:head>

<div class="header">
	<h1>Storage endpoints</h1>
	<span class="meta">{endpoints.length} configured · one S3/SMB/NFS store, used for backups and/or as a witness</span>
</div>

{#if notice}<div class="notice">{notice}</div>{/if}
{#if error}<div class="error">{error}</div>{/if}

<div class="card">
	<h3>Add storage endpoint</h3>
	<div class="form-grid">
		<label>Id<input placeholder="nas-1 / aws-offsite" bind:value={f_id} spellcheck="false" /></label>
		<label>Type
			<select bind:value={f_type}>
				<option value="s3">S3 (object store)</option>
				<option value="smb">SMB / CIFS</option>
				<option value="nfs">NFS</option>
			</select>
		</label>
		<label class="wide">Label (optional)<input placeholder="Office QNAP" bind:value={f_label} /></label>

		{#if f_type === 's3'}
			<label>Endpoint URL<input placeholder="https://s3.example.com" bind:value={f_s3_endpoint} spellcheck="false" /></label>
			<label>Bucket<input placeholder="bedrock" bind:value={f_s3_bucket} spellcheck="false" /></label>
			<label>Region<input placeholder="us-east-1" bind:value={f_s3_region} spellcheck="false" /></label>
			<label>Prefix (optional)<input placeholder="cluster-a" bind:value={f_s3_prefix} spellcheck="false" /></label>
			<label>Access key<input bind:value={f_s3_access_key} spellcheck="false" /></label>
			<label>Secret key<input type="password" placeholder={"•••• (kept if blank on edit)"} bind:value={f_s3_secret_key} /></label>
			<label class="check"><input type="checkbox" bind:checked={f_s3_disable_tls} /> Plain HTTP (no TLS)</label>
			<label class="check"><input type="checkbox" bind:checked={f_s3_disable_tls_verification} /> Skip TLS cert verify (self-signed)</label>
		{:else}
			<label>Server<input placeholder="10.0.0.5" bind:value={f_fs_server} spellcheck="false" /></label>
			<label>{f_type === 'nfs' ? 'Export path' : 'Share'}<input
				placeholder={f_type === 'nfs' ? '/export/bedrock' : 'bedrock'} bind:value={f_fs_share} spellcheck="false" /></label>
			<label>Mount options (optional)<input placeholder="vers=4.1" bind:value={f_fs_options} spellcheck="false" /></label>
			{#if f_type === 'smb'}
				<label>Username<input bind:value={f_fs_username} spellcheck="false" /></label>
				<label>Password<input type="password" placeholder={"•••• (kept if blank on edit)"} bind:value={f_fs_password} /></label>
			{/if}
		{/if}
	</div>

	<div class="boxes">
		<label class="box"><input type="checkbox" bind:checked={enable_backup} /> <strong>Enable for backups</strong></label>
		{#if enable_backup}
			<div class="reveal">
				<label>Encryption password
					<input bind:value={enc_password} spellcheck="false" />
				</label>
				<p class="hint">This is <strong>NOT a password</strong> — it's Bedrock's published
					public default, so the repo is <em>effectively unencrypted</em> (recoverability
					over secrecy, like a plain local disk). Leave it as-is for trusted storage.
					Replace it with a real passphrase <strong>only</strong> for an untrusted remote
					(e.g. public-cloud S3) — then losing it means losing the backups.</p>
			</div>
		{/if}
		<label class="box"><input type="checkbox" bind:checked={enable_witness} />
			<strong>Enable as {f_type === 's3' ? 'S3' : 'fileshare'}-Witness</strong></label>
		{#if enable_witness}
			<p class="hint reveal">This store becomes a quorum witness — every node writes a slot
				here and reads it back to break a 2-node failover tie. It must be reachable +
				writable from every node.</p>
		{/if}
	</div>

	<div class="actions">
		<button class="btn-ghost" onclick={runTest} disabled={testing}>{testing ? 'Testing…' : 'Test on master'}</button>
		<button class="btn-add" onclick={save} disabled={saving}>{saving ? 'Saving…' : 'Save + activate'}</button>
		{#if test_ok !== null}
			<span class="test-result" class:ok={test_ok} class:bad={!test_ok}>
				{test_ok ? '✓' : '✗'} {test_reason}
			</span>
		{/if}
	</div>
	<p class="hint">Test runs on the master node: a real write + read-back round-trip
		(S3 PUT/GET/DELETE; SMB/NFS mount + read-after-write — which also catches a
		DFS-R / redirecting share that serves a stale replica). Always test before you
		rely on a store for backups or quorum.</p>
</div>

{#if loading}
	<p class="muted">Loading…</p>
{:else if endpoints.length === 0}
	<p class="muted">No storage endpoints yet. Add one above, then tick a box to use it
		for backups and/or as a witness.</p>
{:else}
	<table>
		<thead><tr><th>Id</th><th>Type</th><th>Location</th><th>Creds</th><th>Used for</th><th></th></tr></thead>
		<tbody>
			{#each endpoints as ep}
				<tr>
					<td><code>{ep.endpoint_id}</code>{#if ep.label}<span class="lbl">{ep.label}</span>{/if}</td>
					<td>{ep.type}</td>
					<td><code>{ep.type === 's3'
						? `${ep.s3_endpoint || ''}/${ep.s3_bucket || ''}${ep.s3_prefix ? '/' + ep.s3_prefix : ''}`
						: `${ep.fs_server || ''}:${ep.fs_share || ''}`}</code></td>
					<td>{ep.type === 's3'
						? (ep.has_s3_secret ? 'S3 secret ✓' : '—')
						: (ep.has_fs_password ? 'SMB pw ✓' : ep.type === 'nfs' ? 'none (nfs)' : '—')}</td>
					<td class="usage">{usageText(ep)}</td>
					<td><button class="btn-del" onclick={() => remove(ep.endpoint_id)}>Remove</button></td>
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
	.form-grid label.check { flex-direction: row; align-items: center; gap: 7px; }
	.form-grid input, .form-grid select { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 7px 9px; color: #c9d1d9; font-size: 13px; }
	.form-grid label.check input { width: auto; }
	.form-grid input:focus, .form-grid select:focus { outline: none; border-color: #1f6feb; }

	.boxes { border-top: 1px solid #21262d; padding-top: 12px; margin-bottom: 12px; display: flex; flex-direction: column; gap: 8px; }
	.box { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #c9d1d9; cursor: pointer; }
	.reveal { margin: 2px 0 6px 24px; padding-left: 12px; border-left: 2px solid #30363d; }
	.reveal label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #8b949e; max-width: 640px; }
	.reveal input { width: 100%; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 7px 9px; color: #c9d1d9; font-size: 12px; font-family: ui-monospace, monospace; }

	.actions { display: flex; align-items: center; gap: 10px; }
	.btn-add { padding: 8px 16px; border: 1px solid #1f6feb; border-radius: 6px; background: #1f6feb22; color: #58a6ff; cursor: pointer; font-size: 13px; }
	.btn-add:hover { background: #1f6feb44; }
	.btn-add:disabled, .btn-ghost:disabled { opacity: 0.5; cursor: default; }
	.btn-ghost { padding: 7px 14px; border: 1px solid #30363d; border-radius: 6px; background: transparent; color: #c9d1d9; cursor: pointer; font-size: 13px; }
	.btn-ghost:hover { background: #21262d; }
	.test-result { font-size: 12px; }
	.test-result.ok { color: #3fb950; }
	.test-result.bad { color: #f85149; }

	.hint { font-size: 11px; color: #8b949e; margin: 8px 0 0; line-height: 1.5; max-width: 720px; }

	.notice { margin-bottom: 12px; padding: 8px 12px; border-left: 3px solid #1f6feb; background: #21262d; color: #58a6ff; font-size: 12px; }
	.error { margin-bottom: 12px; padding: 8px 12px; border-left: 3px solid #f85149; background: #21262d; color: #f85149; font-size: 12px; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; vertical-align: top; }
	td code { font-family: ui-monospace, monospace; }
	td .lbl { color: #8b949e; font-size: 11px; margin-left: 8px; }
	td.usage { color: #8b949e; font-size: 12px; }

	.btn-del { padding: 4px 10px; border: 1px solid #f85149; background: transparent; color: #f85149; border-radius: 4px; font-size: 12px; cursor: pointer; }
	.btn-del:hover { background: #f8514922; }
	.muted { color: #8b949e; font-size: 13px; }
</style>
