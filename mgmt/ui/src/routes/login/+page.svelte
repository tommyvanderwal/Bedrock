<script lang="ts">
	import { onMount } from 'svelte';
	import { setToken, apiPostPublic } from '$lib/api';

	let username = $state('root');
	let password = $state('');
	let err = $state('');
	let busy = $state(false);
	let nextPath = $state('/');

	onMount(() => {
		const u = new URL(window.location.href);
		nextPath = u.searchParams.get('next') || '/';
	});

	async function submit(e: Event) {
		e.preventDefault();
		err = '';
		busy = true;
		try {
			const r = await apiPostPublic('/api/login', { username, password });
			setToken(r.token, r.exp);
			window.location.href = nextPath;
		} catch (e: any) {
			err = (e?.message || '').replace(/^\d+:\s*/, '') || 'login failed';
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head><title>Bedrock — Sign in</title></svelte:head>

<div class="wrap">
	<form class="card" onsubmit={submit}>
		<h1>Bedrock</h1>
		<p class="sub">Sign in to continue</p>

		<label>
			<span>Username</span>
			<input type="text" bind:value={username} autocomplete="username" required />
		</label>

		<label>
			<span>Password</span>
			<input type="password" bind:value={password} autocomplete="current-password" required />
		</label>

		{#if err}<div class="err">{err}</div>{/if}

		<button type="submit" disabled={busy || !username || !password}>
			{busy ? 'Signing in…' : 'Sign in'}
		</button>
	</form>
</div>

<style>
	.wrap {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: #0d1117;
		color: #e6edf3;
	}
	.card {
		background: #161b22;
		border: 1px solid #30363d;
		border-radius: 8px;
		padding: 28px 32px;
		width: 340px;
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	h1 { margin: 0; font-size: 22px; color: #58a6ff; }
	.sub { margin: 0 0 4px 0; color: #8b949e; font-size: 13px; }
	label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #8b949e; }
	input {
		background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
		padding: 8px 10px; color: #e6edf3; font-size: 14px;
	}
	input:focus { outline: 2px solid #58a6ff; border-color: #58a6ff; }
	button {
		background: #238636; border: 1px solid #2ea043; color: #fff;
		padding: 9px 12px; border-radius: 6px; font-weight: 600;
		cursor: pointer; margin-top: 6px;
	}
	button:disabled { opacity: 0.55; cursor: not-allowed; }
	button:hover:enabled { background: #2ea043; }
	.err {
		background: #f8514922; border-left: 3px solid #f85149;
		padding: 8px 10px; font-size: 13px; color: #ffa198;
		border-radius: 4px;
	}
</style>
