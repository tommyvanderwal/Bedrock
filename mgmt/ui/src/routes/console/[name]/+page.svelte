<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import { apiGet } from '$lib/api';

	let vmName = $derived($page.params.name);
	let status = $state('Loading...');
	let iframeSrc = $state('');

	onMount(async () => {
		try {
			const cluster = await apiGet('/api/cluster');
			const vm = cluster.vms[vmName];
			if (!vm || vm.state !== 'running') {
				status = 'VM is not running';
				return;
			}
			if (!vm.vnc_ws_url) {
				status = 'No VNC available';
				return;
			}
			// vnc_ws_url is a path like "/vnc/<name>" served by the mgmt proxy.
			// noVNC builds ws(s)://window.location.host + path automatically.
			const path = vm.vnc_ws_url.replace(/^\//, '');
			iframeSrc = `/novnc/vnc.html?path=${encodeURIComponent(path)}&autoconnect=true&resize=scale&reconnect=true`;
			status = 'Connected';
		} catch (e: any) {
			status = `Error: ${e.message}`;
		}
	});
</script>

<svelte:head><title>Console: {vmName}</title></svelte:head>

<div class="console-page">
	<div class="console-bar">
		<a href="/">&larr; Dashboard</a>
		<span class="vm-name">{vmName}</span>
		<span class="status" class:ok={status === 'Connected'}>{status}</span>
	</div>
	{#if iframeSrc}
		<iframe src={iframeSrc} title="VNC Console for {vmName}"></iframe>
	{:else}
		<div class="placeholder">{status}</div>
	{/if}
</div>

<style>
	.console-page {
		position: fixed; top: 0; left: 0; right: 0; bottom: 0;
		display: flex; flex-direction: column; background: #000;
	}
	.console-bar {
		display: flex; gap: 16px; align-items: center;
		padding: 8px 16px; background: #161b22; border-bottom: 1px solid #30363d;
		font-size: 14px;
	}
	.vm-name { font-weight: 600; color: #e6edf3; }
	.status { margin-left: auto; font-size: 12px; color: #8b949e; }
	.status.ok { color: #3fb950; }
	iframe {
		flex: 1; border: none; width: 100%; height: 100%;
	}
	.placeholder {
		flex: 1; display: flex; align-items: center; justify-content: center;
		font-size: 18px; color: #8b949e;
	}
</style>
