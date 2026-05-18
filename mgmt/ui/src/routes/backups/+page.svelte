<script lang="ts">
	import { onMount } from 'svelte';
	import { listBackupTargets, getBackupCredsStatus, setBackupTarget,
		removeBackupTarget, listAllBackups, vmRestore, vmBackupDelete,
		type BackupTarget, type BackupCredsStatus,
		type ClusterBackupRow } from '$lib/api';

	let targets = $state<Record<string, BackupTarget>>({});
	let credsStatus = $state<BackupCredsStatus | null>(null);
	let loading = $state(true);

	// Cluster-wide backup history.
	let clusterBackups = $state<ClusterBackupRow[]>([]);
	let restoring = $state<string | null>(null);  // kopia_snapshot_id in progress

	// Restore-confirm modal — destructive write to /dev/<lv>, type vm name.
	let restoreModal = $state<ClusterBackupRow | null>(null);
	let restoreTyped = $state('');

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
	// force_password_overwrite is a CLI-only emergency flag; not exposed
	// in the UI on purpose. Rotating the kopia password destroys access
	// to every existing snapshot — too dangerous to surface as a
	// dashboard checkbox. The /backups page disables the password
	// field entirely once any node has the key installed.

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
			const [t, c, b] = await Promise.all([
				listBackupTargets(), getBackupCredsStatus(), listAllBackups(),
			]);
			targets = t.targets || {};
			credsStatus = c;
			clusterBackups = b.backups || [];
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

	onMount(() => {
		refresh();
		// Light polling — backup history grows, in-flight backups land
		// in cluster.json after the BACKUP_DONE log fold (~1s after the
		// task completes). 15s is fine; live updates flow via the
		// 'task' WS channel for in-flight progress.
		const iv = setInterval(refresh, 15000);
		return () => clearInterval(iv);
	});

	function openRestoreModal(b: ClusterBackupRow) {
		restoreModal = b;
		restoreTyped = '';
	}
	function closeRestoreModal() {
		restoreModal = null;
		restoreTyped = '';
	}

	async function confirmRestore() {
		if (!restoreModal) return;
		if (restoreTyped !== restoreModal.vm) return;
		const b = restoreModal;
		restoreModal = null;
		restoring = b.kopia_snapshot_id;
		banner = { kind: 'warn', text: `Restore queued for ${b.vm} → snapshot ${b.kopia_snapshot_id.slice(0,12)}…` };
		try {
			await vmRestore(b.vm, {
				target_id: b.target_id,
				kopia_snapshot_id: b.kopia_snapshot_id,
			});
			banner = { kind: 'ok',
				text: `Restore started for ${b.vm}. Track progress in the Tasks drawer.` };
		} catch (e: any) {
			banner = { kind: 'err', text: `Restore failed to start: ${e.message}` };
		} finally {
			restoring = null;
			setTimeout(() => banner?.kind === 'ok' && (banner = null), 8000);
		}
	}

	async function deleteSnapshot(b: ClusterBackupRow) {
		if (!confirm(`Delete kopia snapshot ${b.kopia_snapshot_id.slice(0,12)}… for ${b.vm}? Repo chunks are GC'd at the next maintenance run.`)) return;
		try {
			await vmBackupDelete(b.vm, b.kopia_snapshot_id, { target_id: b.target_id });
			await refresh();
		} catch (e: any) {
			banner = { kind: 'err', text: `Delete failed: ${e.message}` };
		}
	}

	function fmtBytes(n: number): string {
		if (!n) return '0 B';
		if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GiB`;
		if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
		if (n >= 1024) return `${(n / 1024).toFixed(0)} KiB`;
		return `${n} B`;
	}

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
				// force_password_overwrite intentionally omitted — UI
				// never rotates passwords; CLI-only emergency path.
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

<div class="key-banner">
	<strong>Operator responsibility:</strong> the encryption password is set
	<strong>once</strong> when you first configure a target. It is the only
	secret that decrypts your backups. Bedrock writes it to
	<code>/etc/bedrock/backup.key</code> on every node, but a full cluster
	wipe destroys all copies. Store the password — together with the S3
	access key / secret key / bucket name / endpoint — in a password
	manager or printed sheet outside this cluster. Lose any of those and
	your backups become permanently unreadable; there is no recovery
	channel.
</div>

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

<h2>Snapshots — cluster-wide</h2>
<div class="card">
	{#if clusterBackups.length === 0}
		<p class="muted">No backups yet. Backups appear here automatically when a VM has been backed up via the dashboard, CLI, or API.</p>
	{:else}
		<table class="snapshots-table">
			<thead>
				<tr>
					<th>VM</th>
					<th>Snapshot</th>
					<th>Disks</th>
					<th>Quiesce</th>
					<th>Target</th>
					<th>Size added</th>
					<th>Duration</th>
					<th>Source node</th>
					<th>Label</th>
					<th>Log idx</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each clusterBackups as b}
					<tr class:in-progress={restoring === b.kopia_snapshot_id}>
						<td><a href="/vm/{b.vm}">{b.vm}</a></td>
						<td><code title={b.kopia_snapshot_id}>{b.kopia_snapshot_id.slice(0, 12)}…</code></td>
						<td>
							{#if b.disks && b.disks.length > 1}
								<span class="pill multi" title={b.disks.map(d => `${d.target_dev}=${d.kopia_snapshot_id.slice(0,8)}…`).join('\n')}>{b.disks.length} disks</span>
							{:else}
								<span class="muted">{b.disks?.[0]?.target_dev || 'disk0'}</span>
							{/if}
						</td>
						<td>
							{#if b.fs_freeze_used}
								<span class="pill ok" title="virsh domfsfreeze succeeded — guest filesystems were quiesced before snapshot">fs-freeze</span>
							{:else}
								<span class="pill warn" title="VM was off, or no qemu-guest-agent — snapshot is crash-consistent only">crash</span>
							{/if}
						</td>
						<td>{b.target_id}</td>
						<td>{fmtBytes(b.bytes_added)}</td>
						<td>{b.duration_s ? `${b.duration_s.toFixed(1)}s` : '-'}</td>
						<td>{b.source_node || '-'}</td>
						<td>{#if b.label}{b.label}{:else}<span class="muted">—</span>{/if}</td>
						<td class="muted">{b.ts_index}</td>
						<td>
							<button class="btn-small primary" disabled={restoring !== null}
								title="Restore ALL disks of this snapshot back onto the VM. The VM must be shut down."
								onclick={() => openRestoreModal(b)}>Restore</button>
							<button class="btn-small danger" disabled={restoring !== null}
								onclick={() => deleteSnapshot(b)}>Delete</button>
						</td>
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
				<option value="kopia-fs">Filesystem path</option>
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
					placeholder="/mnt/shared/bedrock-backups" />
				<span class="hint">Must be reachable on every node (shared FS).</span>
			</label>
		</div>
	{/if}

	<div class="row">
		<label class="grow">Encryption password
			{#if allNodesHavePassword}
				<input type="password" autocomplete="off" disabled
					placeholder="(installed on every node — set once, never rotate from here)" />
				<span class="hint">
					Already configured. Bedrock deliberately does NOT expose
					password rotation in the UI — rotating destroys access to
					every existing backup. If you ever truly need to start
					over with a fresh key, do it via the CLI on the master
					(<code>bedrock backup target set …</code> with
					<code>--force-password-overwrite</code>) <em>after</em>
					you've confirmed every backup tied to the old key is
					expendable.
				</span>
			{:else}
				<input type="password" autocomplete="off" bind:value={encryption_password}
					placeholder="32+ chars; you set this ONCE" />
				<span class="hint warn">
					This is set <strong>once</strong> and never rotated. It is the
					single secret that decrypts every backup. Bedrock does not
					store it anywhere recoverable — losing it means losing every
					backup ever taken against this repo.
				</span>
			{/if}
		</label>
	</div>

	{#if !allNodesHavePassword}
		<div class="critical-notice">
			<h4>⚠ Operator responsibility — write these down NOW, outside this cluster:</h4>
			<ol>
				<li>The <strong>encryption password</strong> you typed above (it gets written to <code>/etc/bedrock/backup.key</code> on every node, but a full cluster wipe destroys all copies).</li>
				<li>The <strong>S3 access key</strong> and <strong>secret key</strong> for this bucket.</li>
				<li>The <strong>bucket name</strong> and <strong>endpoint URL</strong>.</li>
			</ol>
			<p>
				Store them in a password manager / safe / printed sheet —
				somewhere that survives a total cluster failure. Without all
				three pieces, restoring backups onto a new cluster is
				<strong>physically impossible</strong>: the bytes on S3 are
				AES-256-GCM-encrypted with a key derived from the password,
				and bedrock has no recovery channel.
			</p>
		</div>
	{/if}

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

{#if restoreModal}
	<div class="modal-bg" role="presentation" onclick={closeRestoreModal}>
		<div class="modal" role="dialog" aria-modal="true"
			onclick={(e) => e.stopPropagation()}
			onkeydown={(e) => { if (e.key === 'Escape') closeRestoreModal(); }}>
			<h3>Restore VM from snapshot</h3>
			<dl class="restore-meta">
				<dt>VM</dt><dd><code>{restoreModal.vm}</code></dd>
				<dt>Backup ID</dt><dd><code>{restoreModal.kopia_snapshot_id}</code></dd>
				<dt>Target</dt><dd><code>{restoreModal.target_id}</code></dd>
				<dt>Label</dt><dd>{restoreModal.label || '—'}</dd>
				<dt>Source node</dt><dd>{restoreModal.source_node || '—'}</dd>
				<dt>Quiesce</dt><dd>{restoreModal.fs_freeze_used ? 'fs-freeze (consistent)' : 'crash-consistent'}</dd>
				<dt>Disks ({restoreModal.disks?.length ?? 1})</dt>
				<dd>
					{#if restoreModal.disks && restoreModal.disks.length > 0}
						<ul class="restore-disks">
							{#each restoreModal.disks as d}
								<li>
									<code>{d.target_dev}</code> →
									<code title={d.kopia_snapshot_id}>{d.kopia_snapshot_id.slice(0,12)}…</code>
									<span class="muted">({fmtBytes(d.bytes_added)})</span>
								</li>
							{/each}
						</ul>
					{:else}
						<span class="muted">single disk (legacy backup record)</span>
					{/if}
				</dd>
			</dl>
			<p class="restore-warn">
				All <strong>{restoreModal.disks?.length ?? 1}</strong> disk(s) of
				<code>{restoreModal.vm}</code> will be streamed back onto their LVs in
				one operation. <strong>Anything written since this backup will be lost.</strong>
				The VM must be shut down before restoring.
			</p>
			<label class="restore-typecheck">
				Type the VM name <code>{restoreModal.vm}</code> to confirm:
				<input type="text" bind:value={restoreTyped} autocomplete="off"
					autofocus
					onkeydown={(e) => { if (e.key === 'Enter' && restoreModal && restoreTyped === restoreModal.vm) confirmRestore(); }} />
			</label>
			<div class="restore-actions">
				<button class="btn" onclick={closeRestoreModal}>Cancel</button>
				<button class="btn btn-danger"
					disabled={!restoreModal || restoreTyped !== restoreModal.vm}
					onclick={confirmRestore}>Restore</button>
			</div>
		</div>
	</div>
{/if}

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
		margin-right: 4px;
	}
	.btn-small:hover:not(:disabled) { background: #30363d; }
	.btn-small:disabled { opacity: 0.4; cursor: not-allowed; }
	.btn-small.danger:hover:not(:disabled) { color: #f85149; border-color: #f85149; }
	.btn-small.primary { border-color: #1f6feb; color: #58a6ff; }
	.btn-small.primary:hover:not(:disabled) { background: #1f6feb22; color: #fff; }

	.snapshots-table { font-variant-numeric: tabular-nums; }
	.snapshots-table tbody tr.in-progress { opacity: 0.5; }

	/* Restore confirm modal — same look-and-feel as VM delete modal */
	.modal-bg {
		position: fixed; inset: 0; background: #0008; backdrop-filter: blur(2px);
		display: flex; align-items: center; justify-content: center; z-index: 1000;
	}
	.modal {
		background: #161b22; border: 1px solid #30363d; border-radius: 8px;
		padding: 24px; min-width: 460px; max-width: 600px;
	}
	.modal h3 {
		font-size: 16px; color: #e6edf3; text-transform: none; letter-spacing: 0;
		margin: 0 0 12px;
	}
	.restore-meta {
		display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px;
		font-size: 12px; margin: 0 0 12px;
	}
	.restore-meta dt { color: #8b949e; }
	.restore-meta dd { margin: 0; color: #c9d1d9; }
	.restore-meta code {
		background: #0d1117; padding: 1px 6px; border-radius: 3px; font-size: 11px;
	}
	.restore-disks { list-style: none; margin: 0; padding: 0; font-size: 12px; }
	.restore-disks li { padding: 2px 0; }
	.restore-warn {
		font-size: 12px; color: #d29922; background: #d2992211;
		border-left: 3px solid #d29922; padding: 8px 12px; margin: 0 0 14px;
	}
	.restore-typecheck { display: block; font-size: 12px; color: #c9d1d9; margin-bottom: 14px; }
	.restore-typecheck code { background: #21262d; padding: 1px 6px; border-radius: 3px; color: #f0c674; }
	.restore-typecheck input {
		display: block; width: 100%; margin-top: 6px;
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		padding: 8px 12px; color: #e6edf3; font-size: 14px;
		font-family: ui-monospace, SFMono-Regular, monospace;
	}
	.restore-typecheck input:focus { outline: none; border-color: #58a6ff; }
	.restore-actions { display: flex; justify-content: flex-end; gap: 8px; }
	.btn-danger { border-color: #f85149; color: #fff; background: #da3633; }
	.btn-danger:hover:not(:disabled) { background: #f85149; }
	.btn-danger:disabled { background: #30363d; color: #6e7681; border-color: #30363d; }

	.pill {
		display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
	}
	.pill.ok { background: #1a7f3733; color: #3fb950; }
	.pill.warn { background: #d2992233; color: #d29922; }
	.pill.multi { background: #1f6feb33; color: #58a6ff; cursor: help; }
	.cred-pill { font-size: 11px; margin-right: 4px; }

	.banner {
		padding: 8px 12px; border-radius: 6px; margin-bottom: 14px; font-size: 13px;
	}
	.banner-ok { background: #1a7f3722; border-left: 3px solid #3fb950; color: #3fb950; }
	.banner-err { background: #f8514922; border-left: 3px solid #f85149; color: #f85149; }
	.banner-warn { background: #d2992222; border-left: 3px solid #d29922; color: #d29922; }

	.key-banner {
		background: #d2992211; border: 1px solid #d29922; border-radius: 6px;
		padding: 12px 16px; margin: 0 0 18px; font-size: 13px; color: #d8b362;
		max-width: 920px; line-height: 1.5;
	}
	.key-banner strong { color: #f0c674; }
	.key-banner code {
		background: #21262d; padding: 1px 6px; border-radius: 3px; color: #c9d1d9;
	}

	.critical-notice {
		background: #f8514916; border-left: 4px solid #f85149; border-radius: 4px;
		padding: 12px 16px; margin: 4px 0 16px; color: #ec8c84;
		font-size: 13px; line-height: 1.5;
	}
	.critical-notice h4 {
		margin: 0 0 8px; color: #f85149; font-size: 13px; text-transform: none;
		letter-spacing: 0; font-weight: 600;
	}
	.critical-notice ol { margin: 4px 0 8px; padding-left: 22px; }
	.critical-notice li { margin: 3px 0; }
	.critical-notice strong { color: #ffae9e; }
	.critical-notice code {
		background: #21262d; padding: 1px 6px; border-radius: 3px; color: #c9d1d9;
	}
	.critical-notice p { margin: 8px 0 0; }
</style>
