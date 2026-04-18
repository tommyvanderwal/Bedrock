<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { ws } from '$lib/ws';
	import { nodes, vms, witness, connected, lastUpdate, events } from '$lib/stores';

	let { children } = $props();

	let hostsOpen = $state(true);
	let vmsOpen = $state(true);

	onMount(() => {
		ws.connect();

		ws.on('cluster', (msg) => {
			if (msg.nodes) nodes.set(msg.nodes);
			if (msg.vms) vms.set(msg.vms);
			if (msg.witness) witness.set(msg.witness);
			lastUpdate.set(new Date().toLocaleTimeString());
		});

		ws.on('vm.state', (msg) => {
			vms.update(v => {
				if (v[msg.name]) v[msg.name] = { ...v[msg.name], ...msg };
				return v;
			});
		});

		ws.on('event', (msg) => {
			events.update(e => [msg, ...e].slice(0, 100));
		});

		const checkConn = setInterval(() => connected.set(ws.connected), 1000);
		return () => clearInterval(checkConn);
	});

	let sortedNodes = $derived(Object.entries($nodes).sort((a, b) => a[0].localeCompare(b[0])));
	let sortedVms = $derived(Object.entries($vms).sort((a, b) => a[0].localeCompare(b[0])));

	let curPath = $derived($page.url.pathname);
	function isActive(path: string): boolean { return curPath === path; }
</script>

<div class="app">
	<aside class="sidebar">
		<div class="brand">
			<a href="/">Bedrock</a>
			<span class="conn-dot" class:online={$connected}
				title={$connected ? 'connected' : 'disconnected'}></span>
		</div>

		<nav class="tree">
			<a class="tree-top" class:active={isActive('/')} href="/">
				<span class="tree-icon">☰</span> Datacenter
			</a>
			<a class="tree-top" class:active={isActive('/isos')} href="/isos">
				<span class="tree-icon">⊙</span> ISOs
			</a>

			<div class="tree-group">
				<div class="tree-header" class:active={isActive('/hosts')}>
					<button class="caret-btn" onclick={() => hostsOpen = !hostsOpen}
						aria-label="Toggle hosts">
						<span class="caret" class:open={hostsOpen}>▸</span>
					</button>
					<a href="/hosts" class="tree-header-link">
						<span class="tree-icon">▣</span> Hosts
						<span class="count">{Object.keys($nodes).length}</span>
					</a>
				</div>
				{#if hostsOpen}
					{#each sortedNodes as [name, node]}
						<a class="tree-item" class:active={isActive(`/node/${name}`)}
							href="/node/{name}">
							<span class="status-dot" class:green={node.online}
								class:red={!node.online}></span>
							<span class="name" title={name}>{name.split('.')[0]}</span>
						</a>
					{/each}
					{#if Object.keys($nodes).length === 0}
						<div class="tree-empty">no hosts</div>
					{/if}
				{/if}
			</div>

			<div class="tree-group">
				<div class="tree-header" class:active={isActive('/vms')}>
					<button class="caret-btn" onclick={() => vmsOpen = !vmsOpen}
						aria-label="Toggle VMs">
						<span class="caret" class:open={vmsOpen}>▸</span>
					</button>
					<a href="/vms" class="tree-header-link">
						<span class="tree-icon">◧</span> VMs
						<span class="count">{Object.keys($vms).length}</span>
					</a>
				</div>
				{#if vmsOpen}
					<a class="tree-item tree-new" class:active={isActive('/vms/new')}
						href="/vms/new">
						<span class="plus">+</span>
						<span class="name">New VM</span>
					</a>
					{#each sortedVms as [name, vm]}
						<a class="tree-item" class:active={isActive(`/vm/${name}`)}
							href="/vm/{name}">
							<span class="status-dot" class:green={vm.state === 'running'}
								class:gray={vm.state !== 'running'}></span>
							<span class="name">{name}</span>
						</a>
					{/each}
				{/if}
			</div>
		</nav>

		<div class="sidebar-foot">
			<div class="meta">{$lastUpdate || 'Connecting...'}</div>
		</div>
	</aside>

	<main>
		{@render children()}
	</main>
</div>

<style>
	:global(body) {
		margin: 0;
		font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
		background: #0d1117;
		color: #e6edf3;
	}
	:global(*) { box-sizing: border-box; }
	:global(a) { color: #58a6ff; text-decoration: none; }
	:global(a:hover) { text-decoration: underline; }

	.app {
		display: grid;
		grid-template-columns: 240px 1fr;
		min-height: 100vh;
	}

	.sidebar {
		background: #0b0f14;
		border-right: 1px solid #21262d;
		display: flex;
		flex-direction: column;
		position: sticky;
		top: 0;
		max-height: 100vh;
		overflow-y: auto;
	}

	.brand {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		border-bottom: 1px solid #21262d;
	}
	.brand a { font-size: 16px; font-weight: 700; color: #58a6ff; }
	.brand a:hover { text-decoration: none; }
	.conn-dot { width: 8px; height: 8px; border-radius: 50%; background: #f85149; }
	.conn-dot.online { background: #3fb950; }

	.tree { padding: 8px 0; flex: 1; }
	.tree-top, .tree-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 14px;
		font-size: 13px;
		color: #c9d1d9;
	}
	.tree-top { font-weight: 600; }
	.tree-top:hover, .tree-item:hover {
		background: #161b22;
		text-decoration: none;
	}
	.tree-top.active, .tree-item.active, .tree-header.active {
		background: #1f6feb22;
		color: #58a6ff;
	}
	.tree-top.active, .tree-item.active {
		border-left: 2px solid #58a6ff;
		padding-left: 12px;
	}
	.tree-header.active { border-left: 2px solid #58a6ff; }
	.tree-header.active .tree-header-link { padding-left: 0; }

	.tree-icon { width: 14px; color: #8b949e; font-size: 12px; }

	.tree-header {
		display: flex;
		align-items: center;
		padding-left: 6px;
	}
	.caret-btn {
		background: none;
		border: none;
		padding: 4px 4px;
		cursor: pointer;
		display: flex;
		align-items: center;
	}
	.caret {
		display: inline-block;
		width: 10px;
		color: #8b949e;
		transition: transform 0.15s;
	}
	.caret.open { transform: rotate(90deg); }
	.tree-header-link {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		padding: 6px 14px 6px 2px;
		font-size: 13px;
		color: #c9d1d9;
		font-weight: 600;
	}
	.tree-header-link:hover {
		background: #161b22;
		text-decoration: none;
	}

	.tree-group { margin: 4px 0; }
	.tree-item { padding-left: 32px; }

	.status-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: #6e7681;
		flex-shrink: 0;
	}
	.status-dot.green { background: #3fb950; }
	.status-dot.red { background: #f85149; }
	.status-dot.gray { background: #6e7681; }

	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
	}
	.count {
		margin-left: auto;
		font-size: 11px;
		color: #6e7681;
		background: #161b22;
		padding: 1px 6px;
		border-radius: 8px;
	}

	.tree-empty {
		padding: 4px 14px 4px 32px;
		font-size: 12px;
		color: #6e7681;
		font-style: italic;
	}

	.tree-new { color: #3fb950; }
	.tree-new:hover { background: #1a7f3722; text-decoration: none; }
	.tree-new .plus { width: 6px; text-align: center; color: #3fb950; font-weight: 600; }

	.sidebar-foot {
		padding: 10px 14px;
		border-top: 1px solid #21262d;
	}
	.meta { font-size: 11px; color: #6e7681; }

	main {
		padding: 20px;
		max-width: 1600px;
	}
</style>
