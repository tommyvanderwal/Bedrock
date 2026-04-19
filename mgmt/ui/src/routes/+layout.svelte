<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { ws } from '$lib/ws';
	import { nodes, vms, witness, connected, lastUpdate, events, tasks,
		type TaskInfo } from '$lib/stores';
	import { apiGet } from '$lib/api';

	let { children } = $props();

	let hostsOpen = $state(true);
	let vmsOpen = $state(true);
	let taskDrawerOpen = $state(false);

	// Reactive view of tasks: actives first, recents below. Subscribe
	// explicitly so the badge count reacts (Svelte 5 $derived + store gotcha).
	let tasksSnapshot = $state<TaskInfo[]>([]);
	let activeCount = $derived(tasksSnapshot.filter(t => t.state === 'running' || t.state === 'pending').length);

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

		// Tasks: live updates from WS, seeded via REST on mount.
		ws.on('task', (msg: any) => {
			if (!msg?.task) return;
			tasks.update(t => { t[msg.task.id] = msg.task; return t; });
		});
		const unsubTasks = tasks.subscribe(map => {
			tasksSnapshot = Object.values(map)
				.sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''));
		});
		apiGet('/api/tasks').then((initial: TaskInfo[]) => {
			const map: Record<string, TaskInfo> = {};
			for (const t of initial) map[t.id] = t;
			tasks.set(map);
		}).catch(() => {});

		const checkConn = setInterval(() => connected.set(ws.connected), 1000);
		return () => { clearInterval(checkConn); unsubTasks(); };
	});

	function fmtDur(ms: number | undefined): string {
		if (!ms) return '';
		if (ms < 1000) return `${ms} ms`;
		if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
		return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
	}
	function fmtAge(from: string | undefined): string {
		if (!from) return '';
		const t = Date.parse(from);
		if (isNaN(t)) return '';
		const ago = Math.max(0, (Date.now() - t) / 1000);
		if (ago < 60) return `${Math.floor(ago)}s ago`;
		if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
		return `${Math.floor(ago / 3600)}h ago`;
	}

	let sortedNodes = $derived(Object.entries($nodes).sort((a, b) => a[0].localeCompare(b[0])));
	let sortedVms = $derived(Object.entries($vms).sort((a, b) => a[0].localeCompare(b[0])));

	let curPath = $derived($page.url.pathname);
	function isActive(path: string): boolean { return curPath === path; }
</script>

<div class="app">
	<aside class="sidebar">
		<div class="brand">
			<a href="/">Bedrock</a>
			<button class="task-badge" class:active={activeCount > 0}
				onclick={() => taskDrawerOpen = !taskDrawerOpen}
				title="Tasks">
				<span class="task-icon">⏳</span>
				{#if activeCount > 0}<span class="task-count">{activeCount}</span>{/if}
			</button>
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
			<a class="tree-top" class:active={isActive('/imports')} href="/imports">
				<span class="tree-icon">↥</span> Imports
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

	{#if taskDrawerOpen}
		<aside class="task-drawer" role="complementary">
			<div class="task-drawer-head">
				<h3>Tasks</h3>
				<button class="drawer-close" onclick={() => taskDrawerOpen = false}>×</button>
			</div>
			{#if tasksSnapshot.length === 0}
				<p class="muted" style="padding:12px">No tasks.</p>
			{:else}
				<div class="task-list">
					{#each tasksSnapshot as t (t.id)}
						<div class="task-row" class:running={t.state === 'running'}
							class:failed={t.state === 'failed'}
							class:succeeded={t.state === 'succeeded'}>
							<div class="task-head">
								<span class="task-state task-state-{t.state}">{t.state}</span>
								<span class="task-subject" title={t.type}>{t.subject}</span>
								<span class="task-age">{fmtAge(t.started_at)}</span>
							</div>
							{#if t.steps && t.steps.length > 0}
								<div class="task-steps">
									{#each t.steps as s}
										<div class="task-step step-{s.state}">
											<span class="step-dot step-dot-{s.state}"></span>
											<span class="step-name">{s.name}</span>
											{#if s.duration_ms}<span class="step-dur">{fmtDur(s.duration_ms)}</span>{/if}
											{#if s.progress !== null && s.progress !== undefined && s.state === 'running'}
												<span class="step-prog">{s.progress}%</span>
											{/if}
											{#if s.error}<span class="step-err" title={s.error}>⚠</span>{/if}
										</div>
									{/each}
								</div>
							{/if}
							{#if t.error}
								<div class="task-error" title={t.error}>{t.error}</div>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</aside>
	{/if}
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

	/* Task badge next to the brand */
	.task-badge {
		background: none; border: 1px solid #30363d; border-radius: 12px;
		padding: 2px 8px; cursor: pointer; font-size: 12px; color: #8b949e;
		display: inline-flex; align-items: center; gap: 4px; margin-left: auto;
	}
	.task-badge:hover { background: #161b22; color: #e6edf3; }
	.task-badge.active { border-color: #d29922; color: #d29922; }
	.task-icon { font-size: 11px; }
	.task-count {
		background: #d29922; color: #000; font-weight: 600;
		border-radius: 8px; padding: 0 5px; min-width: 14px; text-align: center;
	}

	/* Task drawer */
	.task-drawer {
		position: fixed; top: 0; right: 0; height: 100vh; width: 380px;
		background: #0d1117; border-left: 1px solid #30363d; z-index: 900;
		display: flex; flex-direction: column;
		box-shadow: -4px 0 12px #0006;
	}
	.task-drawer-head {
		display: flex; align-items: center; justify-content: space-between;
		padding: 12px 16px; border-bottom: 1px solid #21262d;
	}
	.task-drawer-head h3 {
		margin: 0; font-size: 14px; text-transform: uppercase;
		letter-spacing: 1px; color: #8b949e;
	}
	.drawer-close {
		background: none; border: none; color: #8b949e; font-size: 22px;
		cursor: pointer; padding: 0; line-height: 1;
	}
	.drawer-close:hover { color: #e6edf3; }
	.task-list { overflow-y: auto; flex: 1; padding: 8px; }
	.task-row {
		background: #161b22; border: 1px solid #21262d; border-radius: 6px;
		padding: 10px 12px; margin-bottom: 8px;
	}
	.task-row.running { border-left: 3px solid #d29922; }
	.task-row.failed { border-left: 3px solid #f85149; }
	.task-row.succeeded { border-left: 3px solid #3fb950; }
	.task-head { display: flex; align-items: center; gap: 8px; font-size: 12px; }
	.task-subject { flex: 1; color: #c9d1d9; }
	.task-age { color: #6e7681; font-size: 11px; }
	.task-state {
		font-size: 10px; padding: 1px 6px; border-radius: 8px; font-weight: 600;
		text-transform: uppercase;
	}
	.task-state-running { background: #d2992244; color: #d29922; }
	.task-state-succeeded { background: #1a7f3744; color: #3fb950; }
	.task-state-failed { background: #f8514944; color: #f85149; }
	.task-state-pending { background: #30363d; color: #8b949e; }
	.task-state-cancelled { background: #30363d; color: #8b949e; }
	.task-steps { margin-top: 6px; }
	.task-step {
		display: flex; align-items: center; gap: 6px; font-size: 11px;
		padding: 2px 0; color: #8b949e;
	}
	.step-dot {
		width: 6px; height: 6px; border-radius: 50%; background: #30363d;
	}
	.step-dot-running { background: #d29922; animation: pulse 1.2s infinite; }
	.step-dot-done { background: #3fb950; }
	.step-dot-failed { background: #f85149; }
	.step-dot-skipped { background: #6e7681; }
	.step-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.step-dur { color: #6e7681; font-variant-numeric: tabular-nums; }
	.step-prog { color: #d29922; font-variant-numeric: tabular-nums; }
	.step-err { color: #f85149; }
	.task-error {
		margin-top: 4px; font-size: 11px; color: #f85149;
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
</style>
