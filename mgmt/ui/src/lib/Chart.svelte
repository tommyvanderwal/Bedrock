<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import uPlot from 'uplot';
	import 'uplot/dist/uPlot.min.css';

	let { title = '', data = [], series = [], width = 400, height = 150 } = $props();

	let container: HTMLDivElement;
	let chart: uPlot | null = null;

	const colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#ff7b72'];

	function buildOpts(): uPlot.Options {
		const s: uPlot.Series[] = [
			{}, // x-axis (timestamps)
			...series.map((name: string, i: number) => ({
				label: name,
				stroke: colors[i % colors.length],
				width: 1.5,
			})),
		];

		return {
			width,
			height,
			title,
			cursor: { show: true },
			scales: { x: { time: true } },
			axes: [
				{ stroke: '#8b949e', grid: { stroke: '#21262d' } },
				{ stroke: '#8b949e', grid: { stroke: '#21262d' }, size: 50 },
			],
			series: s,
		};
	}

	function buildData(): uPlot.AlignedData {
		if (!data || data.length === 0) return [[], []];
		// data is expected as: [[ts, val], [ts, val], ...] per series
		// We need to align timestamps across series
		// For simplicity, assume all series share the same timestamps (from same query)
		const firstSeries = Object.values(data)[0] as [number, number][];
		if (!firstSeries || firstSeries.length === 0) return [[], []];

		const timestamps = firstSeries.map(p => p[0]);
		const aligned: number[][] = [timestamps];
		for (const points of Object.values(data) as [number, number][][]) {
			aligned.push(points.map(p => p[1]));
		}
		return aligned as uPlot.AlignedData;
	}

	onMount(() => {
		if (container && data && Object.keys(data).length > 0) {
			chart = new uPlot(buildOpts(), buildData(), container);
		}
	});

	// Reactively update when data changes
	$effect(() => {
		if (chart && data && Object.keys(data).length > 0) {
			chart.setData(buildData());
		} else if (!chart && container && data && Object.keys(data).length > 0) {
			chart = new uPlot(buildOpts(), buildData(), container);
		}
	});

	onDestroy(() => {
		chart?.destroy();
	});
</script>

<div class="chart-wrapper">
	<div bind:this={container}></div>
</div>

<style>
	.chart-wrapper {
		background: #161b22;
		border: 1px solid #30363d;
		border-radius: 8px;
		padding: 8px;
		overflow: hidden;
	}
	.chart-wrapper :global(.u-title) {
		font-size: 12px !important;
		color: #8b949e !important;
	}
	.chart-wrapper :global(.u-legend) {
		font-size: 11px !important;
	}
	.chart-wrapper :global(.u-legend .u-label) {
		color: #e6edf3 !important;
	}
	.chart-wrapper :global(.u-legend .u-value) {
		color: #8b949e !important;
	}
</style>
