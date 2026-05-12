<script lang="ts">
	import { topology, type TopologySwitch, type TopologyConnection } from '$lib/stores';

	function deviceLabel(sw: TopologySwitch): string {
		// Friendly label: system_name if known, else MAC. The MAC always
		// appears underneath as a stable identifier.
		return sw.system_name || sw.device_key.toUpperCase();
	}

	function uniqueNodes(conns: TopologyConnection[]): string[] {
		const set = new Set<string>();
		for (const c of conns) set.add(c.node);
		return Array.from(set).sort();
	}

	function fmtAge(ts: number | undefined): string {
		if (!ts) return '';
		const age = Date.now() / 1000 - ts;
		if (age < 60) return `${Math.floor(age)}s ago`;
		if (age < 3600) return `${Math.floor(age / 60)}m ago`;
		if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
		return `${Math.floor(age / 86400)}d ago`;
	}

	// Sort switches deterministic — by (system_name || device_key).
	let cards = $derived.by(() => {
		const switches = $topology.switches || {};
		return Object.values(switches).sort((a, b) =>
			(a.system_name || a.device_key).localeCompare(b.system_name || b.device_key));
	});
</script>

<svelte:head><title>Topology — Bedrock</title></svelte:head>

<div class="header">
	<h1>Physical topology</h1>
	<span class="meta">
		{$topology.switch_count} device{$topology.switch_count === 1 ? '' : 's'}
		seen by {$topology.node_count} node{$topology.node_count === 1 ? '' : 's'}
		· refreshed {fmtAge($topology.computed_at)}
	</span>
</div>

<p class="explainer">
	What each cluster node sees on the other end of its cables, learned from
	<strong>LLDP</strong> / <strong>CDP</strong> / <strong>MNDP</strong> frames
	that switches and routers advertise themselves with. One card per physical
	device, merged by MAC address. The <em>Connections</em> column shows
	<code>node → NIC → switch port</code> — when several rows in one card
	point to the same switch, that's an at-a-glance answer to <em>"which
	NICs are all plugged into the same switch?"</em>.
</p>

{#if cards.length === 0}
	<div class="empty">
		<p>No switches or routers are advertising themselves on any of this
		cluster's NICs yet.</p>
		<p class="muted">If you have managed switches in the path, give them up
		to ~60 seconds to send their first LLDP/CDP cycle. Unmanaged switches
		do not advertise — that's expected and not a fault. The mesh layer
		works either way.</p>
	</div>
{/if}

<div class="cards">
	{#each cards as sw (sw.device_key)}
		{@const peers = uniqueNodes(sw.connections)}
		{@const shared = peers.length > 1}
		<div class="card" class:shared>
			<div class="card-head">
				<div class="title">
					<span class="icon">▣</span>
					<span class="name">{deviceLabel(sw)}</span>
					{#if shared}
						<span class="tag shared-tag" title="{peers.length} cluster nodes connect here">
							shared by {peers.length} nodes
						</span>
					{/if}
				</div>
				<div class="ids">
					<span class="mac" title="Chassis MAC (merge key)">
						{sw.device_key}
					</span>
					{#if sw.mgmt_ip}
						<span class="ip" title="Switch management IP">{sw.mgmt_ip}</span>
					{/if}
				</div>
			</div>

			<div class="meta-row">
				{#if sw.platform}
					<span class="kv"><span class="k">platform</span> <span class="v">{sw.platform}</span></span>
				{/if}
				{#if sw.aliases.length > 0}
					<span class="kv"><span class="k">aliases</span>
						<span class="v">{sw.aliases.join(' · ')}</span>
					</span>
				{/if}
				<span class="kv">
					<span class="k">via</span>
					<span class="v">
						{#each sw.protocols as p, i}
							<span class="proto proto-{p}">{p}</span>{i < sw.protocols.length - 1 ? ' ' : ''}
						{/each}
					</span>
				</span>
			</div>

			<table class="conns">
				<thead>
					<tr>
						<th>Cluster node</th>
						<th>Local NIC</th>
						<th>Switch port</th>
						<th>via</th>
						<th>Last seen</th>
					</tr>
				</thead>
				<tbody>
					{#each sw.connections as c (c.node + c.my_nic + c.protocol)}
						<tr>
							<td>
								<a href="/node/{c.node}" class="node-link">
									{c.node.split('.')[0]}
								</a>
							</td>
							<td><code>{c.my_nic}</code></td>
							<td>
								{#if c.port_id}
									<code>{c.port_id}</code>
								{:else}
									<span class="muted">unknown</span>
								{/if}
								{#if c.port_descr}
									<span class="port-descr">— {c.port_descr}</span>
								{/if}
							</td>
							<td><span class="proto proto-{c.protocol}">{c.protocol}</span></td>
							<td class="muted">{fmtAge(c.last_seen)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/each}
</div>

<style>
	.header {
		display: flex;
		align-items: baseline;
		gap: 12px;
		margin-bottom: 4px;
	}
	h1 { margin: 0; font-size: 18px; font-weight: 600; }
	.meta { color: #8b949e; font-size: 12px; }

	.explainer {
		color: #8b949e;
		font-size: 13px;
		line-height: 1.55;
		max-width: 880px;
		margin: 8px 0 24px;
	}
	.explainer code { background: #161b22; padding: 1px 6px; border-radius: 4px;
		font-size: 12px; color: #e6edf3; }

	.empty {
		background: #0d1117;
		border: 1px dashed #30363d;
		border-radius: 8px;
		padding: 24px;
		color: #c9d1d9;
	}
	.empty p { margin: 0 0 6px; }
	.muted { color: #6e7681; }

	.cards {
		display: grid;
		grid-template-columns: 1fr;
		gap: 16px;
	}

	.card {
		background: #0d1117;
		border: 1px solid #21262d;
		border-radius: 8px;
		padding: 14px 16px 12px;
	}
	.card.shared {
		border-color: #1f6feb55;
		box-shadow: 0 0 0 1px #1f6feb22 inset;
	}

	.card-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 16px;
		margin-bottom: 6px;
		flex-wrap: wrap;
	}
	.title { display: flex; align-items: baseline; gap: 8px; }
	.icon { color: #58a6ff; }
	.name { font-size: 15px; font-weight: 600; color: #e6edf3; }
	.tag {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.5px;
		padding: 2px 8px;
		border-radius: 10px;
		font-weight: 600;
	}
	.shared-tag {
		background: #1f6feb33;
		color: #79c0ff;
	}

	.ids {
		display: flex;
		gap: 12px;
		font-size: 12px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.mac { color: #8b949e; }
	.ip {
		color: #3fb950;
		background: #1a7f3722;
		padding: 1px 8px;
		border-radius: 4px;
	}

	.meta-row {
		display: flex;
		gap: 16px;
		flex-wrap: wrap;
		font-size: 12px;
		color: #c9d1d9;
		margin-bottom: 10px;
		padding-bottom: 10px;
		border-bottom: 1px solid #21262d;
	}
	.kv .k {
		color: #6e7681;
		margin-right: 4px;
	}

	.proto {
		display: inline-block;
		font-size: 10px;
		font-weight: 600;
		text-transform: uppercase;
		padding: 1px 6px;
		border-radius: 3px;
		letter-spacing: 0.5px;
	}
	.proto-lldp { background: #6f42c133; color: #d2a8ff; }
	.proto-cdp  { background: #d2992233; color: #d29922; }
	.proto-mndp { background: #1f6feb33; color: #79c0ff; }

	table.conns {
		width: 100%;
		border-collapse: collapse;
		font-size: 12px;
	}
	table.conns th {
		text-align: left;
		font-weight: 500;
		color: #6e7681;
		padding: 6px 8px;
		border-bottom: 1px solid #21262d;
	}
	table.conns td {
		padding: 7px 8px;
		border-bottom: 1px solid #161b22;
	}
	table.conns tr:last-child td { border-bottom: none; }

	.node-link { font-weight: 500; }
	code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 11px;
		background: #161b22;
		padding: 1px 6px;
		border-radius: 3px;
		color: #e6edf3;
	}
	.port-descr { color: #8b949e; font-size: 11px; margin-left: 4px; }
</style>
