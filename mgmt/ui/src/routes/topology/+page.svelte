<script lang="ts">
	import { nodes, topology, type TopologySwitch,
		type TopologyConnection, type TopologyLink } from '$lib/stores';

	// ─── Focus / "core" node ────────────────────────────────────────────
	// Empty string == show every cable. Clicking a node sets it as the
	// focus and the diagram draws only the cables that touch it
	// (i.e. that node's view of the cluster).
	let coreNode = $state('');

	function shortName(s: string): string {
		return s ? s.split('.')[0] : '';
	}
	function fmtAge(ts: number | undefined): string {
		if (!ts) return '';
		const age = Date.now() / 1000 - ts;
		if (age < 60) return `${Math.floor(age)}s ago`;
		if (age < 3600) return `${Math.floor(age / 60)}m ago`;
		if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
		return `${Math.floor(age / 86400)}d ago`;
	}
	function fmtSpeed(mbps: number): string {
		if (!mbps) return '?';
		if (mbps >= 1000) return `${(mbps / 1000).toFixed(mbps % 1000 ? 1 : 0)}G`;
		return `${mbps}M`;
	}

	// ─── Derived data ───────────────────────────────────────────────────
	let clusterNodes = $derived(
		Object.keys($nodes).sort((a, b) => a.localeCompare(b))
	);
	let switchList = $derived(
		Object.values($topology.switches).sort((a, b) =>
			(a.system_name || a.device_key).localeCompare(b.system_name || b.device_key))
	);

	// Per-node NIC list, learned from any source: mesh links + switch
	// observations. We sort with br0 first then alphabetical so the LAN
	// NIC is always at the left of each node card.
	let nicsByNode = $derived.by(() => {
		const m = new Map<string, string[]>();
		const add = (node: string, nic: string) => {
			if (!nic) return;
			const s = m.get(node) ?? [];
			if (!s.includes(nic)) s.push(nic);
			m.set(node, s);
		};
		for (const link of $topology.links) {
			add(link.node_a, link.nic_a);
			add(link.node_b, link.nic_b);
		}
		for (const sw of switchList) {
			for (const c of sw.connections) add(c.node, c.my_nic);
		}
		for (const arr of m.values()) {
			arr.sort((a, b) => {
				if (a === 'br0' && b !== 'br0') return -1;
				if (b === 'br0' && a !== 'br0') return 1;
				return a.localeCompare(b);
			});
		}
		return m;
	});

	// ─── Diagram layout ─────────────────────────────────────────────────
	const SVG_W = 1200;
	const SVG_H = 540;
	const SWITCH_Y = 60;
	const SWITCH_W = 200;
	const SWITCH_H = 80;
	const NODE_Y  = 360;
	const NODE_W  = 220;
	const NODE_H  = 140;

	type Pos = { x: number; y: number };

	function spread(count: number, w: number, boxW: number): number[] {
		if (count === 0) return [];
		// Even spacing across the canvas leaving a small margin.
		const margin = 60;
		const span = w - 2 * margin;
		if (count === 1) return [w / 2 - boxW / 2];
		const step = span / (count - 1);
		return Array.from({ length: count }, (_, i) =>
			margin + step * i - boxW / 2);
	}

	let nodeX = $derived(spread(clusterNodes.length, SVG_W, NODE_W));
	let switchX = $derived(spread(switchList.length, SVG_W, SWITCH_W));

	function nodePos(node: string): Pos | null {
		const idx = clusterNodes.indexOf(node);
		if (idx < 0) return null;
		return { x: nodeX[idx], y: NODE_Y };
	}
	function switchPos(deviceKey: string): Pos | null {
		const idx = switchList.findIndex(s => s.device_key === deviceKey);
		if (idx < 0) return null;
		return { x: switchX[idx], y: SWITCH_Y };
	}

	function nicAnchor(node: string, nic: string): Pos | null {
		// Anchor at the *top* edge of the node box, slot for this NIC.
		const p = nodePos(node);
		if (!p) return null;
		const nics = nicsByNode.get(node) || [];
		const idx = nics.indexOf(nic);
		if (idx < 0) return null;
		const slotW = NODE_W / nics.length;
		return { x: p.x + slotW * (idx + 0.5), y: p.y };
	}
	function switchAnchor(deviceKey: string): Pos | null {
		const p = switchPos(deviceKey);
		if (!p) return null;
		return { x: p.x + SWITCH_W / 2, y: p.y + SWITCH_H };
	}

	type Cable = {
		x1: number; y1: number; x2: number; y2: number;
		kind: 'mesh' | 'switch';
		dim: boolean;
		key: string;
		label: string;
	};

	let cables = $derived.by((): Cable[] => {
		const out: Cable[] = [];
		// Node ↔ Switch
		for (const sw of switchList) {
			const sa = switchAnchor(sw.device_key);
			if (!sa) continue;
			// Dedup: one cable per (node, nic) regardless of how many
			// protocols carried the same observation.
			const seen = new Set<string>();
			for (const c of sw.connections) {
				const key = `${sw.device_key}|${c.node}|${c.my_nic}`;
				if (seen.has(key)) continue;
				seen.add(key);
				const na = nicAnchor(c.node, c.my_nic);
				if (!na) continue;
				const touches = !coreNode || c.node === coreNode;
				out.push({
					x1: sa.x, y1: sa.y, x2: na.x, y2: na.y,
					kind: 'switch', dim: !touches, key,
					label: `${shortName(c.node)}/${c.my_nic} → ` +
						`${sw.system_name || sw.device_key} ${c.port_id || '?'}` +
						`  (${sw.protocols.join('+')})`,
				});
			}
		}
		// Node ↔ Node
		for (const l of $topology.links) {
			const a = nicAnchor(l.node_a, l.nic_a);
			const b = nicAnchor(l.node_b, l.nic_b);
			if (!a || !b) continue;
			const touches = !coreNode || l.node_a === coreNode || l.node_b === coreNode;
			out.push({
				x1: a.x, y1: a.y, x2: b.x, y2: b.y,
				kind: 'mesh', dim: !touches,
				key: `${l.node_a}/${l.nic_a}↔${l.node_b}/${l.nic_b}`,
				label: `${shortName(l.node_a)}/${l.nic_a} ↔ ` +
					`${shortName(l.node_b)}/${l.nic_b}` +
					`  ${fmtSpeed(l.speed_mbps)} · ${l.rtt_us}µs` +
					(l.blip_total ? ` · ${l.blip_total} blips` : ''),
			});
		}
		// Render dimmed cables first so the bright ones lie on top.
		out.sort((a, b) => Number(b.dim) - Number(a.dim));
		return out;
	});

	function toggleCore(name: string) {
		coreNode = (coreNode === name) ? '' : name;
	}
</script>

<svelte:head><title>Topology — Bedrock</title></svelte:head>

<div class="header">
	<h1>Physical topology</h1>
	<span class="meta">
		{$topology.switch_count} switch{$topology.switch_count === 1 ? '' : 'es'}
		· {$topology.link_count} mesh cable{$topology.link_count === 1 ? '' : 's'}
		· {$topology.node_count} node{$topology.node_count === 1 ? '' : 's'}
		reporting · refreshed {fmtAge($topology.computed_at)}
	</span>
</div>

<p class="explainer">
	Schematic of what's physically cabled. Switch boxes at the top
	(learned from <strong>LLDP</strong> / <strong>CDP</strong> /
	<strong>MNDP</strong> frames). Cluster nodes at the bottom (each box
	shows its NICs along the top edge). Lines are cables.
	{#if coreNode}
		Showing only cables that touch <strong>{shortName(coreNode)}</strong> —
		click it again to release.
	{:else}
		Click any node to focus on its connections.
	{/if}
</p>

<div class="diagram-wrap">
	<svg viewBox="0 0 {SVG_W} {SVG_H}" xmlns="http://www.w3.org/2000/svg"
		role="img" aria-label="Physical topology">
		<!-- Cables (drawn first so boxes lie on top) -->
		{#each cables as c (c.key)}
			<line x1={c.x1} y1={c.y1} x2={c.x2} y2={c.y2}
				class="cable cable-{c.kind}" class:dim={c.dim}>
				<title>{c.label}</title>
			</line>
		{/each}

		<!-- Switch boxes -->
		{#each switchList as sw}
			{@const p = switchPos(sw.device_key)}
			{#if p}
				<g class="switch-box">
					<rect x={p.x} y={p.y} width={SWITCH_W} height={SWITCH_H} rx="6"/>
					<text x={p.x + SWITCH_W / 2} y={p.y + 22}
						class="switch-name" text-anchor="middle">
						{sw.system_name || sw.device_key.toUpperCase()}
					</text>
					<text x={p.x + SWITCH_W / 2} y={p.y + 40}
						class="meta-text" text-anchor="middle">
						{sw.mgmt_ip || sw.device_key}
					</text>
					<text x={p.x + SWITCH_W / 2} y={p.y + 60}
						class="proto-text" text-anchor="middle">
						{sw.protocols.join(' · ')}
					</text>
					<title>{sw.system_name || sw.device_key}
{sw.device_key}
{#if sw.mgmt_ip}mgmt: {sw.mgmt_ip}{/if}
{#if sw.platform}platform: {sw.platform}{/if}
heard via {sw.protocols.join(', ')}</title>
				</g>
			{/if}
		{/each}

		<!-- Cluster-node boxes (clickable) -->
		{#each clusterNodes as nodeName}
			{@const p = nodePos(nodeName)}
			{@const nics = nicsByNode.get(nodeName) || []}
			{@const isCore = coreNode === nodeName}
			{#if p}
				<g class="node-box" class:core={isCore}
					tabindex="0" role="button"
					aria-label="Focus on {nodeName}"
					onclick={() => toggleCore(nodeName)}
					onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCore(nodeName); } }}>
					<rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx="6"/>
					<!-- NIC strip along the top edge -->
					{#each nics as nic, i}
						{@const slotW = NODE_W / nics.length}
						<rect class="nic-tab"
							x={p.x + slotW * i + 2}
							y={p.y - 10}
							width={Math.max(6, slotW - 4)}
							height="14" rx="2"/>
						<text class="nic-label"
							x={p.x + slotW * (i + 0.5)}
							y={p.y - 14}
							text-anchor="middle">{nic}</text>
					{/each}
					<text class="node-name"
						x={p.x + NODE_W / 2} y={p.y + 50}
						text-anchor="middle">{shortName(nodeName)}</text>
					<text class="meta-text"
						x={p.x + NODE_W / 2} y={p.y + 72}
						text-anchor="middle">
						{($nodes[nodeName]?.host) || ''}
					</text>
					{#if isCore}
						<text class="core-tag"
							x={p.x + NODE_W / 2} y={p.y + 100}
							text-anchor="middle">▼ core view</text>
					{/if}
				</g>
			{/if}
		{/each}
	</svg>
</div>

{#if $topology.switch_count === 0 && $topology.link_count === 0}
	<div class="empty">
		<p>No physical topology data yet.</p>
		<p class="muted">Cluster-internal cables show up once bedrock-net's
		discovery hysteresis passes (~5 s after each cable comes up).
		Switches/routers show up once one full LLDP / CDP / MNDP cycle
		(~30–60 s) has flowed through to the cluster's nodes.</p>
	</div>
{/if}

<!-- Detail panel: per-switch breakdown — keeps the previous card-list
     view for operators who want the text version. -->
{#if switchList.length > 0}
	<h2 class="section">Switches &amp; routers — detail</h2>
	<div class="cards">
		{#each switchList as sw (sw.device_key)}
			{@const peers = new Set(sw.connections.map(c => c.node))}
			{@const shared = peers.size > 1}
			<div class="card" class:shared>
				<div class="card-head">
					<div class="title">
						<span class="icon">▣</span>
						<span class="name">{sw.system_name || sw.device_key.toUpperCase()}</span>
						{#if shared}
							<span class="tag shared-tag">shared by {peers.size} nodes</span>
						{/if}
					</div>
					<div class="ids">
						<span class="mac">{sw.device_key}</span>
						{#if sw.mgmt_ip}<span class="ip">{sw.mgmt_ip}</span>{/if}
					</div>
				</div>
				<div class="meta-row">
					{#if sw.platform}<span class="kv"><span class="k">platform</span> <span class="v">{sw.platform}</span></span>{/if}
					{#if sw.aliases.length > 0}<span class="kv"><span class="k">aliases</span> <span class="v">{sw.aliases.join(' · ')}</span></span>{/if}
					<span class="kv"><span class="k">via</span>
						<span class="v">
							{#each sw.protocols as p, i}
								<span class="proto proto-{p}">{p}</span>{i < sw.protocols.length - 1 ? ' ' : ''}
							{/each}
						</span>
					</span>
				</div>
				<table class="conns">
					<thead>
						<tr><th>Node</th><th>NIC</th><th>Switch port</th><th>via</th><th>Last seen</th></tr>
					</thead>
					<tbody>
						{#each sw.connections as c (c.node + c.my_nic + c.protocol)}
							<tr>
								<td><a href="/node/{c.node}">{shortName(c.node)}</a></td>
								<td><code>{c.my_nic}</code></td>
								<td>{#if c.port_id}<code>{c.port_id}</code>{:else}<span class="muted">unknown</span>{/if}
									{#if c.port_descr}<span class="port-descr">— {c.port_descr}</span>{/if}</td>
								<td><span class="proto proto-{c.protocol}">{c.protocol}</span></td>
								<td class="muted">{fmtAge(c.last_seen)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/each}
	</div>
{/if}

<style>
	.header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
	h1 { margin: 0; font-size: 18px; font-weight: 600; }
	h2.section { font-size: 14px; font-weight: 600; color: #c9d1d9;
		text-transform: uppercase; letter-spacing: 1px; margin: 24px 0 12px; }
	.meta { color: #8b949e; font-size: 12px; }

	.explainer {
		color: #8b949e; font-size: 13px; line-height: 1.55;
		max-width: 880px; margin: 8px 0 16px;
	}

	.diagram-wrap {
		background: #0d1117;
		border: 1px solid #21262d;
		border-radius: 8px;
		padding: 8px;
		margin-bottom: 16px;
	}
	svg { width: 100%; height: auto; display: block; }

	/* Cable styling */
	.cable {
		stroke-width: 1.8;
		fill: none;
		cursor: default;
	}
	.cable-mesh   { stroke: #3fb950; opacity: 0.85; }
	.cable-switch { stroke: #79c0ff; opacity: 0.9; stroke-dasharray: 5 3; }
	.cable.dim    { opacity: 0.12; }
	.cable:hover  { stroke-width: 3; opacity: 1; }

	/* Switch boxes */
	.switch-box rect {
		fill: #1f6feb22;
		stroke: #1f6feb;
		stroke-width: 1.5;
	}
	.switch-name { fill: #79c0ff; font-size: 14px; font-weight: 600;
		font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
	.meta-text { fill: #8b949e; font-size: 11px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
	.proto-text { fill: #6e7681; font-size: 10px; text-transform: uppercase;
		letter-spacing: 1px; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }

	/* Node boxes */
	.node-box rect {
		fill: #161b22;
		stroke: #30363d;
		stroke-width: 1.5;
		cursor: pointer;
	}
	.node-box.core rect {
		fill: #d2992222;
		stroke: #d29922;
		stroke-width: 2;
	}
	.node-box:hover rect { stroke: #58a6ff; }
	.node-name { fill: #e6edf3; font-size: 14px; font-weight: 600;
		font-family: -apple-system, BlinkMacSystemFont, sans-serif;
		pointer-events: none; }
	.core-tag { fill: #d29922; font-size: 10px; font-weight: 700;
		text-transform: uppercase; letter-spacing: 1px;
		pointer-events: none; }

	/* NIC tabs along the top edge of each node */
	.nic-tab {
		fill: #30363d;
		stroke: #58a6ff;
		stroke-width: 0.8;
	}
	.node-box.core .nic-tab { stroke: #d29922; }
	.nic-label {
		fill: #c9d1d9; font-size: 9px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		pointer-events: none;
	}

	/* Empty state */
	.empty {
		background: #0d1117; border: 1px dashed #30363d; border-radius: 8px;
		padding: 24px; color: #c9d1d9;
	}
	.empty p { margin: 0 0 6px; }
	.muted { color: #6e7681; }

	/* Detail cards (carried over from the previous version) */
	.cards { display: grid; grid-template-columns: 1fr; gap: 16px; }
	.card {
		background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
		padding: 14px 16px 12px;
	}
	.card.shared {
		border-color: #1f6feb55;
		box-shadow: 0 0 0 1px #1f6feb22 inset;
	}
	.card-head {
		display: flex; justify-content: space-between; align-items: baseline;
		gap: 16px; margin-bottom: 6px; flex-wrap: wrap;
	}
	.title { display: flex; align-items: baseline; gap: 8px; }
	.icon { color: #58a6ff; }
	.name { font-size: 15px; font-weight: 600; color: #e6edf3; }
	.tag {
		font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
		padding: 2px 8px; border-radius: 10px; font-weight: 600;
	}
	.shared-tag { background: #1f6feb33; color: #79c0ff; }

	.ids {
		display: flex; gap: 12px; font-size: 12px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.mac { color: #8b949e; }
	.ip {
		color: #3fb950; background: #1a7f3722;
		padding: 1px 8px; border-radius: 4px;
	}

	.meta-row {
		display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px;
		color: #c9d1d9; margin-bottom: 10px; padding-bottom: 10px;
		border-bottom: 1px solid #21262d;
	}
	.kv .k { color: #6e7681; margin-right: 4px; }

	.proto {
		display: inline-block; font-size: 10px; font-weight: 600;
		text-transform: uppercase; padding: 1px 6px; border-radius: 3px;
		letter-spacing: 0.5px;
	}
	.proto-lldp { background: #6f42c133; color: #d2a8ff; }
	.proto-cdp  { background: #d2992233; color: #d29922; }
	.proto-mndp { background: #1f6feb33; color: #79c0ff; }

	table.conns {
		width: 100%; border-collapse: collapse; font-size: 12px;
	}
	table.conns th {
		text-align: left; font-weight: 500; color: #6e7681;
		padding: 6px 8px; border-bottom: 1px solid #21262d;
	}
	table.conns td {
		padding: 7px 8px; border-bottom: 1px solid #161b22;
	}
	table.conns tr:last-child td { border-bottom: none; }
	code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 11px; background: #161b22; padding: 1px 6px;
		border-radius: 3px; color: #e6edf3;
	}
	.port-descr { color: #8b949e; font-size: 11px; margin-left: 4px; }
</style>
