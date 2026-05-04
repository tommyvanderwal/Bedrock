<script lang="ts">
	import { onMount } from 'svelte';
	import { listBackupTargets, getBackupCredsStatus, setBackupTarget,
		removeBackupTarget,
		type BackupTarget, type BackupCredsStatus } from '$lib/api';

	let targets = $state<Record<string, BackupTarget>>({});
	let credsStatus = $state<BackupCredsStatus | null>(null);
	let loading = $state(true);

	// Form state — defaults sized for the QNAP-S3 testbed case.
	let target_id = $state('main');
	let kind = $state<'kopia-s3' | 'kopia-fs'>('kopia-s3');
	let s3_endpoint = $state('');
	let s3_bucket = $state('');
	let s3_region = $state('us-east-1');
	let filesystem_path = $state('');
	let override_source_prefix = $state('');  // server fills with cluster_uuid:vms

	let s3_access_key = $state('');
	let s3_secret_key = $state('');
	let encryption_password = $state('');
	let force_password_overwrite = $state(false);

	let submitting = $state(false);
	let banner = $state<{ kind: 'ok' | 'err' | 'warn'; text: string } | null>(null);

	// Derived: does this cluster already have a kopia password installed
	// somewhere? If yes, the password field becomes optional / locked.
	let anyNodeHasPassword = $derived(
		credsStatus
			? Object.values(credsStatus.nodes).some(n => n.has_password)
			: false
	);
	let allNodesHavePassword = $derived(
		credsStatus
			? Object.values(credsStatus.nodes).every(n => n.has_password)
			: false
	);

	async function refresh() {
		loading = true;
		try {
			const [t, c] = await Promise.all([listBackupTargets(), getBackupCredsStatus()]);
			targets = t.targets || {};
			credsStatus = c;
			// If a target with this id already exists, prefill the form
			// with its non-secret fields. Lets the operator edit endpoint
			// or rotate keys without re-typing everything.
			const existing = targets[target_id];
			if (existing) {
				kind = (existing.kind as 'kopia-s3' | 'kopia-fs') || 'kopia-s3';
				s3_endpoint = existing.s3_endpoint || '';
				s3_bucket = existing.s3_bucket || '';
				s3_region = existing.s3_region || s3_region;
				filesystem_path = existing.filesystem_path || '';
				override_source_prefix = existing.override_source_prefix || '';
			}
		} catch (e: any) {
			banner = { kind: 'err', text: `Load failed: ${e.message}` };
		} finally {
			loading = false;
		}
	}

	onMount(refresh);

	async function submit() {
		banner = null;
		if (kind === 'kopia-s3' && !s3_bucket) {
			banner = { kind: 'err', text: 'S3 bucket is required for kopia-s3' };
			return;
		}
		if (kind === 'kopia-fs' && !filesystem_path) {
			banner = { kind: 'err', text: 'Filesystem path is required for kopia-fs' };
			return;
		}
		if (!anyNodeHasPassword && !encryption_password) {
			banner = { kind: 'err', text:
				'No encryption password is installed on any node yet. ' +
				'Provide one — it cannot be recovered later if lost.' };
			return;
		}
		submitting = true;
		try {
			const r = await setBackupTarget({
				target_id, kind,
				s3_endpoint, s3_bucket, s3_region,
				filesystem_path, override_source_prefix,
				s3_access_key: s3_access_key || undefined,
				s3_secret_key: s3_secret_key || undefined,
				encryption_password: encryption_password || undefined,
				force_password_overwrite,
				reason: 'set via dashboard',
			});
			const warns = (r as any)?.warnings as string[] | undefined;
			if (warns && warns.length > 0) {
				banner = { kind: 'warn',
					text: `Target saved (log idx ${r.log_index}). Some nodes had issues: ${warns.join('; ')}` };
			} else {
				banner = { kind: 'ok',
					text: `Target ${target_id} configured (log idx ${r.log_index}). Repo verified at ≥256-bit hash.` };
			}
			// Wipe secret fields after success — never keep them in the DOM.
			s3_access_key = '';
			s3_secret_key = '';
			encryption_password = '';
			force_password_overwrite = false;
			await refresh();
		} catch (e: any) {
			banner = { kind: 'err', text: e.message };
		} finally {
			submitting = false;
		}
	}

	async function remove(id: string) {
		if (!confirm(`Remove backup target "${id}"? Existing snapshots stay in the kopia repo; bedrock just stops using it.`)) return;
		try {
			await removeBackupTarget(id, 'removed via dashboard');
			await refresh();
		} catch (e: any) {
			banner = { kind: 'err', text: e.message };
		}
	}
</script>

<svelte:head><title>Backups — Bedrock</title></svelte:head>

<h1>Backup targets</h1>
<p class="subtitle">
	One Kopia repository per cluster. Operator picks where it lives —
	S3 / S3-compatible (Wasabi, B2, R2, MinIO, QNAP-S3, …) or a
	filesystem path. Every node connects to the same repo.
	Bedrock enforces a <strong>≥256-bit content-hash floor</strong> at
	repo creation; weaker repos are refused.
</p>

{#if banner}
	<div class="banner banner-{banner.kind}">{banner.text}</div>
{/if}

<h2>Configured</h2>
<div class="card">
	{#if loading}
		<p class="muted">Loading…</p>
	{:else if Object.keys(targets).length === 0}
		<p class="muted">None yet. Use the form below to set one.</p>
	{:else}
		<table>
			<thead>
				<tr><th>ID</th><th>Kind</th><th>Where</th><th>Region</th><th></th></tr>
			</thead>
			<tbody>
				{#each Object.entries(targets) as [id, t]}
					<tr>
						<td><code>{id}</code></td>
						<td>{t.kind}</td>
						<td>
							{#if t.kind === 'kopia-s3'}
								<code>{t.s3_endpoint || '—'}</code> / <code>{t.s3_bucket || '—'}</code>
							{:else}
								<code>{t.filesystem_path || '—'}</code>
							{/if}
						</td>
						<td>{t.s3_region || '—'}</td>
						<td><button class="btn-small danger" onclick={() => remove(id)}>Remove</button></td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<h2>Per-node secrets</h2>
<div class="card">
	{#if !credsStatus}
		<p class="muted">…</p>
	{:else}
		<table>
			<thead><tr><th>Node</th><th>backup.key</th><th>credentials</th></tr></thead>
			<tbody>
				{#each Object.entries(credsStatus.nodes) as [name, info]}
					<tr>
						<td>{name}</td>
						<td>
							{#if info.has_password}
								<span class="pill ok">installed</span>
							{:else}
								<span class="pill warn">missing</span>
							{/if}
						</td>
						<td>
							{#if Object.keys(info.creds).length === 0}
								<span class="muted">none</span>
							{:else}
								{#each Object.keys(info.creds) as cid}
									<code class="cred-pill">{cid}</code>
								{/each}
							{/if}
							{#if info.error}<span class="err">⚠ {info.error}</span>{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<h2>Set / update target</h2>
<div class="card form-card">
	<div class="row">
		<label>Target ID
			<input type="text" bind:value={target_id} placeholder="main" />
		</label>
		<label>Kind
			<select bind:value={kind}>
				<option value="kopia-s3">S3 (or S3-compatible)</option>
				<option value="kopia-fs">Filesystem / NFS path</option>
			</select>
		</label>
	</div>

	{#if kind === 'kopia-s3'}
		<div class="row">
			<label class="grow">Endpoint
				<input type="text" bind:value={s3_endpoint}
					placeholder="qnap.local:9000  or  s3.wasabisys.com" />
				<span class="hint">For QNAP, this is the host:port of the S3 service. Plain HTTPS endpoints work without scheme.</span>
			</label>
		</div>
		<div class="row">
			<label class="grow">Bucket
				<input type="text" bind:value={s3_bucket} placeholder="bedrock-backups" />
			</label>
			<label>Region
				<input type="text" bind:value={s3_region} placeholder="us-east-1" />
				<span class="hint">Many S3-compatibles ignore this; <code>us-east-1</code> is a safe default.</span>
			</label>
		</div>
		<div class="row">
			<label class="grow">Access key
				<input type="text" autocomplete="off" bind:value={s3_access_key}
					placeholder={Object.keys(credsStatus?.nodes?.[Object.keys(credsStatus?.nodes ?? {})[0]]?.creds ?? {}).includes(target_id) ? '(installed — leave blank to keep)' : 'AKIA…'} />
			</label>
			<label class="grow">Secret key
				<input type="password" autocomplete="off" bind:value={s3_secret_key}
					placeholder={Object.keys(credsStatus?.nodes?.[Object.keys(credsStatus?.nodes ?? {})[0]]?.creds ?? {}).includes(target_id) ? '(installed — leave blank to keep)' : 'secret…'} />
			</label>
		</div>
	{:else}
		<div class="row">
			<label class="grow">Filesystem path
				<input type="text" bind:value={filesystem_path}
					placeholder="/mnt/nas/bedrock-backups" />
				<span class="hint">Must be reachable on every node (NFS mount, shared FS, etc.).</span>
			</label>
		</div>
	{/if}

	<div class="row">
		<label class="grow">Encryption password
			{#if allNodesHavePassword && !force_password_overwrite}
				<input type="password" autocomplete="off" disabled placeholder="(installed on every node — leave blank)" />
				<span class="hint">
					<label class="inline">
						<input type="checkbox" bind:checked={force_password_overwrite} />
						Rotate password (destroys access to existing backups — irreversible)
					</label>
				</span>
			{:else}
				<input type="password" autocomplete="off" bind:value={encryption_password}
					placeholder="32+ chars; cannot be recovered if lost" />
				<span class="hint warn">
					This is the single secret that protects every backup.
					<strong>Write it down somewhere outside the cluster</strong> —
					if every node and your records lose it, restores are impossible.
				</span>
			{/if}
		</label>
	</div>

	<div class="row">
		<label class="grow">Override-source prefix <span class="muted">(optional)</span>
			<input type="text" bind:value={override_source_prefix}
				placeholder="(default: <cluster-uuid>:vms)" />
			<span class="hint">Stable VM identity in the kopia repo; defaults are fine.</span>
		</label>
	</div>

	<div class="actions">
		<button class="btn primary" onclick={submit} disabled={submitting}>
			{submitting ? 'Saving…' : 'Save target'}
		</button>
		<span class="muted">Bedrock will run <code>kopia repository connect</code> on this node, verify the block hash is ≥256-bit, then notify peers via the cluster log.</span>
	</div>
</div>

<style>
	h1 { font-size: 24px; margin: 0 0 6px; }
	h2 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin: 24px 0 8px; }
	.subtitle { color: #8b949e; font-size: 13px; max-width: 720px; margin: 0 0 18px; }

	.card {
		background: #161b22; border: 1px solid #30363d; border-radius: 8px;
		padding: 14px 16px;
	}
	.form-card { padding: 16px 20px; }

	table { width: 100%; border-collapse: collapse; font-size: 13px; }
	th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #21262d; }
	th { color: #8b949e; font-weight: 500; font-size: 12px; }
	tbody tr:last-child td { border-bottom: none; }
	code { background: #21262d; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
	.muted { color: #6e7681; }
	.err { color: #f85149; margin-left: 8px; font-size: 12px; }

	.row {
		display: flex; gap: 14px; margin-bottom: 14px; align-items: flex-start;
	}
	.row label {
		display: flex; flex-direction: column; font-size: 12px; color: #8b949e;
		gap: 4px; min-width: 180px;
	}
	.row label.grow { flex: 1; }
	.row label.inline {
		flex-direction: row; align-items: center; gap: 6px; min-width: 0;
		color: #c9d1d9;
	}
	.row input[type="text"], .row input[type="password"], .row select {
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		padding: 6px 10px; color: #e6edf3; font-size: 13px;
		font-family: ui-monospace, SFMono-Regular, monospace;
	}
	.row input:focus, .row select:focus {
		outline: none; border-color: #58a6ff;
	}
	.row input:disabled {
		background: #0a0d11; color: #6e7681; font-style: italic;
	}
	.hint { font-size: 11px; color: #6e7681; }
	.hint.warn { color: #d29922; }

	.actions { display: flex; align-items: center; gap: 12px; margin-top: 8px; }
	.actions .muted { font-size: 11px; max-width: 480px; }

	.btn {
		padding: 6px 14px; border: 1px solid #30363d; border-radius: 6px;
		background: #21262d; color: #e6edf3; font-size: 13px; cursor: pointer;
	}
	.btn:hover:not(:disabled) { background: #30363d; }
	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn.primary { border-color: #1f6feb; color: #fff; background: #1f6feb; }
	.btn.primary:hover:not(:disabled) { background: #388bfd; }

	.btn-small {
		padding: 2px 8px; border: 1px solid #30363d; border-radius: 4px;
		background: #21262d; color: #8b949e; font-size: 11px; cursor: pointer;
	}
	.btn-small.danger:hover { color: #f85149; border-color: #f85149; }

	.pill {
		display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
	}
	.pill.ok { background: #1a7f3733; color: #3fb950; }
	.pill.warn { background: #d2992233; color: #d29922; }
	.cred-pill { font-size: 11px; margin-right: 4px; }

	.banner {
		padding: 8px 12px; border-radius: 6px; margin-bottom: 14px; font-size: 13px;
	}
	.banner-ok { background: #1a7f3722; border-left: 3px solid #3fb950; color: #3fb950; }
	.banner-err { background: #f8514922; border-left: 3px solid #f85149; color: #f85149; }
	.banner-warn { background: #d2992222; border-left: 3px solid #d29922; color: #d29922; }
</style>
