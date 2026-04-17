<script lang="ts">
	let { logs = [] } = $props();
</script>

<div class="log-list">
	{#if logs.length === 0}
		<div class="empty">No log entries</div>
	{:else}
		{#each logs as entry}
			<div class="log-entry" class:warn={entry.level === 'warn' || entry.priority === '4'}
				class:error={entry.level === 'error' || entry.priority === '3' || entry.priority === '2'}>
				<span class="log-time">{(entry._time || '').substring(11, 19)}</span>
				<span class="log-host">{entry.hostname || '-'}</span>
				<span class="log-app">{entry.app || entry.appname || '-'}</span>
				<span class="log-msg">{entry._msg || ''}</span>
			</div>
		{/each}
	{/if}
</div>

<style>
	.log-list {
		background: #161b22; border: 1px solid #30363d; border-radius: 8px;
		overflow: hidden; max-height: 400px; overflow-y: auto;
	}
	.empty { padding: 20px; text-align: center; color: #8b949e; font-size: 13px; }
	.log-entry {
		display: flex; gap: 8px; padding: 4px 12px; font-size: 12px;
		border-bottom: 1px solid #21262d; font-family: monospace;
	}
	.log-entry.warn { border-left: 3px solid #d29922; }
	.log-entry.error { border-left: 3px solid #f85149; }
	.log-time { color: #8b949e; min-width: 65px; flex-shrink: 0; }
	.log-host { color: #58a6ff; min-width: 50px; flex-shrink: 0; }
	.log-app { color: #bc8cff; min-width: 80px; flex-shrink: 0; }
	.log-msg { color: #e6edf3; word-break: break-all; }
</style>
