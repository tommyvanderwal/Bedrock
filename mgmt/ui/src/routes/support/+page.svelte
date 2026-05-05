<script lang="ts">
	import { onMount } from 'svelte';
	import { getSupportChecks, type SupportCheck, type SupportChecksResponse } from '$lib/api';

	let data = $state<SupportChecksResponse | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function refresh() {
		loading = true;
		error = null;
		try {
			data = await getSupportChecks();
		} catch (e: any) {
			error = e.message;
			data = null;
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		refresh();
		// Refresh every 30s — checks are cheap and the operator wants
		// live state when they're poking at the cluster.
		const iv = setInterval(refresh, 30000);
		return () => clearInterval(iv);
	});

	function badgeText(overall: string): string {
		if (overall === 'ok') return '✓ Optimal Support';
		if (overall === 'warn') return '⚠ Supported with caveats';
		return '✗ Unsupported configuration';
	}
</script>

<svelte:head><title>Support — Bedrock</title></svelte:head>

<h1>Supportability</h1>
<p class="subtitle">
	Live snapshot of the cluster's supportability state. The "Optimal
	Support" badge requires every check to pass. Warnings keep you
	supported with caveats; fails mean we can't reliably help when
	something breaks. Re-evaluated every 30 seconds.
</p>

{#if loading && !data}
	<p class="muted">Running checks…</p>
{:else if error}
	<div class="banner banner-err">{error}</div>
{:else if data}
	<div class="overall overall-{data.overall}">
		{badgeText(data.overall)}
	</div>

	<div class="checks">
		{#each data.checks as c}
			<div class="check check-{c.status}">
				<div class="check-head">
					<span class="dot dot-{c.status}"></span>
					<h3>{c.label}</h3>
					<span class="status-pill pill-{c.status}">{c.status.toUpperCase()}</span>
				</div>
				<p class="note">{c.note}</p>
				{#if c.remediation && c.status !== 'ok'}
					<p class="remediation">→ {c.remediation}</p>
				{/if}
			</div>
		{/each}
	</div>

	<div class="refresh-row">
		<button class="btn" onclick={refresh}>Re-run checks</button>
		<span class="muted">auto-refresh every 30 s</span>
	</div>
{/if}

<style>
	h1 { font-size: 24px; margin: 0 0 6px; }
	.subtitle {
		color: #8b949e; font-size: 13px; max-width: 720px; margin: 0 0 18px;
	}
	.banner {
		padding: 8px 12px; border-radius: 6px; margin-bottom: 14px; font-size: 13px;
	}
	.banner-err { background: #f8514922; border-left: 3px solid #f85149; color: #f85149; }
	.muted { color: #6e7681; }

	.overall {
		display: inline-block; padding: 8px 16px; border-radius: 8px;
		font-size: 16px; font-weight: 600; margin-bottom: 18px;
		border: 2px solid;
	}
	.overall-ok    { background: #1a7f3722; border-color: #3fb950; color: #3fb950; }
	.overall-warn  { background: #d2992222; border-color: #d29922; color: #d29922; }
	.overall-fail  { background: #f8514922; border-color: #f85149; color: #f85149; }

	.checks {
		display: grid; gap: 10px; max-width: 920px;
	}
	.check {
		background: #161b22; border: 1px solid #30363d; border-radius: 8px;
		padding: 12px 16px;
	}
	.check-ok    { border-left: 4px solid #3fb950; }
	.check-warn  { border-left: 4px solid #d29922; }
	.check-fail  { border-left: 4px solid #f85149; }

	.check-head {
		display: flex; align-items: center; gap: 10px; margin-bottom: 6px;
	}
	.check-head h3 {
		flex: 1; margin: 0; font-size: 14px; color: #c9d1d9; font-weight: 600;
	}
	.dot {
		width: 10px; height: 10px; border-radius: 50%;
	}
	.dot-ok    { background: #3fb950; }
	.dot-warn  { background: #d29922; }
	.dot-fail  { background: #f85149; }

	.status-pill {
		padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
	}
	.pill-ok    { background: #1a7f3744; color: #3fb950; }
	.pill-warn  { background: #d2992244; color: #d29922; }
	.pill-fail  { background: #f8514944; color: #f85149; }

	.note { margin: 0 0 4px; font-size: 13px; color: #c9d1d9; }
	.remediation {
		margin: 6px 0 0; font-size: 12px; color: #8b949e;
		font-style: italic;
	}

	.refresh-row {
		margin-top: 18px; display: flex; align-items: center; gap: 12px;
	}
	.btn {
		padding: 6px 14px; border: 1px solid #30363d; border-radius: 6px;
		background: #21262d; color: #e6edf3; font-size: 13px; cursor: pointer;
	}
	.btn:hover { background: #30363d; }
</style>
