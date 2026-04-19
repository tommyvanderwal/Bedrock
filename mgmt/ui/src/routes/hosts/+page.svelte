<script lang="ts">
	import { nodes, vms } from '$lib/stores';

	let rows = $derived.by(() => {
		return Object.entries($nodes)
			.sort((a, b) => a[0].localeCompare(b[0]))
			.map(([name, n]: [string, any]) => {
				const running = Object.values($vms).filter((v: any) => v.running_on === name).length;
				const defined = Object.values($vms).filter((v: any) => (v.defined_on || []).includes(name)).length;
				const memPct = n.mem_total_mb ? Math.round(n.mem_used_mb / n.mem_total_mb * 100) : 0;
				// Worst thinpool data usage on this node (null if no pool)
				const pools: any[] = n.thinpools || [];
				const poolPct = pools.length ? Math.max(...pools.map(p => p.data_pct)) : null;
				const poolSizeGb = pools.length ? Math.round(pools.reduce((s, p) => s + p.size_bytes, 0) / (1024**3)) : 0;
				return { name, node: n, running, defined, memPct, poolPct, poolSizeGb };
			});
	});
</script>

<svelte:head><title>Hosts — Bedrock</title></svelte:head>

<div class="header">
	<h1>Hosts</h1>
	<span class="meta">{rows.length} node{rows.length === 1 ? '' : 's'}</span>
</div>

<table>
	<thead>
		<tr>
			<th>Name</th>
			<th>Status</th>
			<th>Host</th>
			<th>Load</th>
			<th>Memory</th>
			<th>Thin pool</th>
			<th>VMs (running / defined)</th>
			<th>Kernel</th>
			<th></th>
		</tr>
	</thead>
	<tbody>
		{#each rows as r}
			<tr>
				<td><a href="/node/{r.name}"><strong>{r.name.split('.')[0]}</strong></a></td>
				<td>
					<span class="tag" class:online={r.node.online} class:offline={!r.node.online}>
						{r.node.online ? 'Online' : 'Offline'}
					</span>
				</td>
				<td>{r.node.host}</td>
				<td>{r.node.load || '-'}</td>
				<td>
					{#if r.node.mem_total_mb}
						<div class="bar" title="{r.node.mem_used_mb} / {r.node.mem_total_mb} MB">
							<div class="bar-fill" style="width: {r.memPct}%"></div>
							<span class="bar-label">{r.memPct}%</span>
						</div>
					{:else}
						<span class="muted">-</span>
					{/if}
				</td>
				<td>
					{#if r.poolPct !== null}
						<div class="bar"
							class:pool-warn={r.poolPct >= 80 && r.poolPct < 95}
							class:pool-full={r.poolPct >= 95}
							title="{r.poolPct}% of {r.poolSizeGb} GB">
							<div class="bar-fill" style="width: {r.poolPct}%"></div>
							<span class="bar-label">{r.poolPct}%</span>
						</div>
					{:else}
						<span class="muted">-</span>
					{/if}
				</td>
				<td>{r.running} / {r.defined}</td>
				<td class="kernel">{r.node.kernel || '-'}</td>
				<td>
					{#if r.node.cockpit_url}
						<a href={r.node.cockpit_url} target="_blank" class="btn-inline">Cockpit</a>
					{/if}
				</td>
			</tr>
		{/each}
		{#if rows.length === 0}
			<tr><td colspan="9" class="muted centered">no hosts yet</td></tr>
		{/if}
	</tbody>
</table>

<style>
	.header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
	h1 { font-size: 22px; margin: 0; }
	.meta { color: #8b949e; font-size: 12px; }

	table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
	th { background: #21262d; text-align: left; padding: 10px 12px; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
	td { padding: 10px 12px; border-top: 1px solid #21262d; font-size: 13px; vertical-align: middle; }
	tr:hover td { background: #1c2128; }
	td.kernel { font-size: 11px; color: #8b949e; max-width: 280px; word-break: break-all; }

	.tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
	.tag.online { background: #1a7f37; color: #fff; }
	.tag.offline { background: #6e7681; color: #fff; }

	.bar { position: relative; width: 140px; height: 16px; background: #0d1117; border: 1px solid #30363d; border-radius: 4px; overflow: hidden; }
	.bar-fill { height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff); }
	.bar.pool-warn .bar-fill { background: linear-gradient(90deg, #bf8700, #d29922); }
	.bar.pool-full .bar-fill { background: linear-gradient(90deg, #a40e26, #f85149); }
	.bar-label { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #e6edf3; }

	.muted { color: #8b949e; }
	.centered { text-align: center; padding: 24px; }
	.btn-inline { font-size: 11px; color: #58a6ff; }
</style>
