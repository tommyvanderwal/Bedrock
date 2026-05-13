<script lang="ts">
	import { nodes, topology, type TopologySwitch,
		type TopologyConnection, type TopologyLink } from '$lib/stores';

	// ─── Focus / "core" node ────────────────────────────────────────────
	let coreNode = $state('');

	// ─── Hover state for highlighting + floating info card ──────────────
	type HoverInfo = { x: number; y: number; lines: string[] } | null;
	let hoverPortId = $state<string | null>(null);
	let hoverCableId = $state<string | null>(null);
	let hoverBusId = $state<string | null>(null);
	let hoverInfo = $state<HoverInfo>(null);

	function shortName(s: string): string { return s ? s.split('.')[0] : ''; }
	function fmtAge(ts: number | undefined): string {
		if (!ts) return '';
		const age = Date.now() / 1000 - ts;
		if (age < 60)  return `${Math.floor(age)}s ago`;
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
	// observations. br0 first (LAN), then alphabetical (enp2s0..).
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

	// Cable-kind inference. The mgmt rollup gives us connectivity but
	// not "what kind of network" each NIC sits on; we derive it client-
	// side from the per-NIC peer count.
	//   * 'lan'    → NIC is br0 (the operator LAN; LAN NICs are always
	//                bus-shaped via the home router or external switch)
	//   * 'shared' → NIC sees more than one peer (full-mesh bus)
	//   * 'p2p'   → NIC sees exactly one peer (direct cable)
	let kindByNic = $derived.by(() => {
		const peersPerNic = new Map<string, Set<string>>();
		for (const L of $topology.links) {
			const ka = `${L.node_a}|${L.nic_a}`;
			const kb = `${L.node_b}|${L.nic_b}`;
			(peersPerNic.get(ka) ?? peersPerNic.set(ka, new Set()).get(ka)!).add(L.node_b);
			(peersPerNic.get(kb) ?? peersPerNic.set(kb, new Set()).get(kb)!).add(L.node_a);
		}
		const m = new Map<string, 'lan' | 'shared' | 'p2p'>();
		for (const [key, peers] of peersPerNic) {
			const [, nic] = key.split('|');
			m.set(key, nic === 'br0' ? 'lan' : (peers.size > 1 ? 'shared' : 'p2p'));
		}
		return m;
	});

	// ─── Canvas geometry ────────────────────────────────────────────────
	// The device rectangle is now a thin "port array" — just contains
	// the port glyphs (vertically centred). The node's NAME / IP /
	// CORE-VIEW tag sit OUTSIDE the rectangle to the left, in a label
	// column that cables never cross.
	const SVG_W           = 1600;
	const SWITCH_Y        = 30;
	const SWITCH_W        = 240;
	const SWITCH_H        = 60;
	const NODE_Y_FLAT     = 300;   // top edge of node rect (flat layout)
	const NODE_W          = 230;
	const NODE_H          = 50;
	const NODE_PAD_X      = 12;
	const PORT_W          = 34;
	const PORT_H          = 18;
	const PORT_GAP        = 6;
	const LABEL_W         = 140;   // width of the label column (left of rect)
	const LABEL_GAP       = 14;    // gap between label column and rect
	const LANE_STEP       = 14;
	const SWITCH_CHANNEL_TOP_OFFSET = 22;
	const MESH_CHANNEL_TOP_OFFSET   = 22;

	// ─── Layout: where each node and switch RECTANGLE sits ─────────────
	// The label column hangs off the LEFT of each rectangle.
	function spread(count: number, w: number, rectW: number,
	                  labelLeft: number): number[] {
		// Place RECTANGLE left edges so neither the leftmost label nor
		// the rightmost rect edge overflows the canvas.
		if (count === 0) return [];
		const margin = 30;
		// First rect must clear: margin + labelLeft + LABEL_GAP
		// Last rect right edge must clear: w - margin
		const firstX = margin + labelLeft;
		const lastX  = w - margin - rectW;
		if (count === 1) return [(firstX + lastX) / 2];
		const step = (lastX - firstX) / (count - 1);
		return Array.from({ length: count }, (_, i) => firstX + step * i);
	}

	type Pos = { x: number; y: number };
	// In star mode, the core node sits in its OWN row above the peer
	// row, so cables core↔peer can route through a clean orthogonal
	// channel between the two rows. Switch uplinks still route through
	// the channel between the switch row and the core row.
	const NODE_Y_STAR_CORE = 220;     // core row y in star mode
	const NODE_Y_STAR_PEER = 480;     // peer row y in star mode

	let layout = $derived.by(() => {
		const switchPos = new Map<string, Pos>();
		// Switches also have a label column on their LEFT.
		const swx = spread(switchList.length, SVG_W, SWITCH_W, LABEL_W + LABEL_GAP);
		switchList.forEach((sw, i) => {
			switchPos.set(sw.device_key, { x: swx[i], y: SWITCH_Y });
		});

		const nodePos = new Map<string, Pos>();
		const isStar = !!coreNode && clusterNodes.includes(coreNode);
		if (isStar) {
			// Core dead-centre at its own row; peers spread evenly in a
			// row below. Both rows are flat so orthogonal routing keeps
			// working — we just have THREE channels now (above core for
			// switch uplinks, between core and peers for core↔peer
			// cables, below peers for any cables not touching the core).
			nodePos.set(coreNode!, {
				x: SVG_W / 2 - NODE_W / 2 + (LABEL_W + LABEL_GAP) / 2,
				y: NODE_Y_STAR_CORE,
			});
			const peers = clusterNodes.filter(n => n !== coreNode);
			const px = spread(peers.length, SVG_W, NODE_W, LABEL_W + LABEL_GAP);
			peers.forEach((peer, i) => {
				nodePos.set(peer, { x: px[i], y: NODE_Y_STAR_PEER });
			});
		} else {
			const nx = spread(clusterNodes.length, SVG_W, NODE_W, LABEL_W + LABEL_GAP);
			clusterNodes.forEach((name, i) => {
				nodePos.set(name, { x: nx[i], y: NODE_Y_FLAT });
			});
		}
		return { nodePos, switchPos, isStar };
	});

	// ─── Port positions ─────────────────────────────────────────────────
	// Each port sits flush with the top edge of its node box (cables
	// emerge UPWARD from the top edge into the routing channels). For
	// switches, ports sit on the BOTTOM edge.
	type PortDef = {
		id: string;
		owner: 'node' | 'switch';
		ownerId: string;
		nic: string;        // for nodes
		x: number;          // centre x of port glyph
		ownerY: number;     // top of owner rectangle
		ownerH: number;     // height of owner rectangle
		yLabel: number;     // y for the label text
		boxX: number;       // top-left of port rect
		boxY: number;
		labelText: string;
		kind: 'lan' | 'shared' | 'p2p' | 'switch';
	};
	let ports = $derived.by((): PortDef[] => {
		const out: PortDef[] = [];
		// Cluster-node ports: INSIDE the rectangle, vertically centred.
		// Cables exit the rectangle at top/bottom edges; the port glyph
		// is the visible "connection point" inside.
		for (const nodeName of clusterNodes) {
			const p = layout.nodePos.get(nodeName);
			if (!p) continue;
			const nics = nicsByNode.get(nodeName) ?? [];
			if (nics.length === 0) continue;
			const totalW = nics.length * PORT_W + (nics.length - 1) * PORT_GAP;
			const startX = p.x + (NODE_W - totalW) / 2;
			const portY = p.y + (NODE_H - PORT_H) / 2;     // vertically centred
			nics.forEach((nic, i) => {
				const px = startX + i * (PORT_W + PORT_GAP);
				const kind = kindByNic.get(`${nodeName}|${nic}`)
					?? (nic === 'br0' ? 'lan' : 'p2p');
				out.push({
					id: `node:${nodeName}|${nic}`,
					owner: 'node',
					ownerId: nodeName,
					nic,
					x: px + PORT_W / 2,
					ownerY: p.y,
					ownerH: NODE_H,
					yLabel: portY + PORT_H + 11,
					boxX: px,
					boxY: portY,                     // INSIDE the rect
					labelText: nic,
					kind,
				});
			});
		}
		// Switch ports: ONE port per switch (representative). Multiple
		// protocols reporting the same physical port (e.g. CDP saying
		// 'vlan1' and LLDP saying 'ether1' from the same wire) collapse
		// to one glyph. Prefer LLDP (physical port name), then CDP,
		// then MNDP.
		for (const sw of switchList) {
			const p = layout.switchPos.get(sw.device_key);
			if (!p || sw.connections.length === 0) continue;
			let rep = '';
			for (const pref of ['lldp', 'cdp', 'mndp']) {
				const c = sw.connections.find(c => c.protocol === pref && c.port_id);
				if (c) { rep = c.port_id; break; }
			}
			if (!rep) rep = sw.connections[0].port_id || '?';
			const portY = p.y + (SWITCH_H - PORT_H) / 2;
			const portX = p.x + SWITCH_W / 2;
			out.push({
				id: `switch:${sw.device_key}`,
				owner: 'switch',
				ownerId: sw.device_key,
				nic: rep,
				x: portX,
				ownerY: p.y,
				ownerH: SWITCH_H,
				yLabel: portY + PORT_H + 11,
				boxX: portX - PORT_W / 2,
				boxY: portY,
				labelText: rep,
				kind: 'switch',
			});
		}
		return out;
	});

	// Quick lookups
	let portById = $derived(new Map(ports.map(p => [p.id, p])));

	// ─── Cables ─────────────────────────────────────────────────────────
	type CableEnd = { portId: string; nodeOrDevice: string; nic: string };
	type Cable = {
		id: string;
		a: CableEnd;
		b: CableEnd;
		kind: 'lan' | 'shared' | 'p2p' | 'switch';
		// Title + tooltip body
		title: string;
		infoLines: string[];
		// Whether this cable touches the focused core node
		touchesCore: boolean;
	};
	// ─── Buses ──────────────────────────────────────────────────────────
	// A shared bus is a connected component of NICs all on the same
	// underlying medium (the operator LAN for br0, or the surviving
	// mesh-plane for enp3s0). We render it as ONE thick horizontal
	// trunk with thin vertical tails to each participating NIC, rather
	// than rendering N(N-1)/2 individual cables that visually masquerade
	// as a full mesh.
	type Bus = {
		id: string;
		kind: 'lan' | 'shared';
		memberPortIds: string[];     // 'node:X|nic' or 'switch:device_key'
		infoLines: string[];
	};
	let buses = $derived.by((): Bus[] => {
		// Connected components on node↔node lan/shared cables.
		const adj = new Map<string, Set<string>>();
		for (const L of $topology.links) {
			const aId = `node:${L.node_a}|${L.nic_a}`;
			const bId = `node:${L.node_b}|${L.nic_b}`;
			const aKind = kindByNic.get(`${L.node_a}|${L.nic_a}`);
			const bKind = kindByNic.get(`${L.node_b}|${L.nic_b}`);
			if (aKind !== bKind) continue;
			if (aKind !== 'lan' && aKind !== 'shared') continue;
			if (!adj.has(aId)) adj.set(aId, new Set());
			if (!adj.has(bId)) adj.set(bId, new Set());
			adj.get(aId)!.add(bId);
			adj.get(bId)!.add(aId);
		}
		const visited = new Set<string>();
		const result: Bus[] = [];
		for (const start of adj.keys()) {
			if (visited.has(start)) continue;
			const comp: string[] = [];
			const stack = [start];
			while (stack.length) {
				const k = stack.pop()!;
				if (visited.has(k)) continue;
				visited.add(k);
				comp.push(k);
				for (const n of adj.get(k) ?? []) stack.push(n);
			}
			if (comp.length < 2) continue;
			const firstKey = comp[0].slice('node:'.length);
			const kind = kindByNic.get(firstKey) as 'lan' | 'shared';
			const sorted = [...comp].sort();
			const nicName = firstKey.split('|')[1];
			const memberLines = sorted.map(c => {
				const noPfx = c.slice('node:'.length);
				const [node, nic] = noPfx.split('|');
				return `  ${shortName(node)} · ${nic}`;
			});
			result.push({
				id: `bus:${kind}:${sorted.join(',')}`,
				kind,
				memberPortIds: sorted,
				infoLines: [
					`${kind === 'lan' ? 'LAN (shared)' : 'Mesh shared bus'} · ${nicName}`,
					`${comp.length} NICs on this bus`,
					...memberLines,
				],
			});
		}

		// Fold switches into a bus when ALL of the switch's connections
		// target NICs that are already on the same bus. That's the case
		// when the switch IS the shared medium (e.g. an operator router
		// every sim shares the LAN through). In that case it doesn't
		// make sense to also draw N individual switch uplinks — one
		// switch tail off the bus says the same thing more cleanly.
		// Folding is restricted to the flat layout: in star mode the
		// switch sits far above and a fold would force a tail to pass
		// through the core box.
		if (!layout.isStar) {
			for (const sw of switchList) {
				if (sw.connections.length === 0) continue;
				const targetBuses = new Set<Bus>();
				let allOnBus = true;
				for (const c of sw.connections) {
					const targetId = `node:${c.node}|${c.my_nic}`;
					const b = result.find(bus => bus.memberPortIds.includes(targetId));
					if (!b) { allOnBus = false; break; }
					targetBuses.add(b);
				}
				if (allOnBus && targetBuses.size === 1) {
					const bus = [...targetBuses][0];
					const swPortId = `switch:${sw.device_key}`;
					if (!bus.memberPortIds.includes(swPortId)) {
						bus.memberPortIds.push(swPortId);
					}
					const swLine = `  ${sw.system_name || sw.device_key} (switch)`;
					if (!bus.infoLines.includes(swLine)) bus.infoLines.push(swLine);
				}
			}
		}
		return result;
	});
	let busByMember = $derived.by(() => {
		const m = new Map<string, Bus>();
		for (const b of buses) for (const k of b.memberPortIds) m.set(k, b);
		return m;
	});

	let cables = $derived.by((): Cable[] => {
		const out: Cable[] = [];
		// Switch↔node cables (from sw.connections).
		// One cable per (switch, node, my_nic) — multiple protocols
		// reporting the same physical wire (CDP+LLDP+MNDP) collapse to
		// one cable that lists all protocols in its tooltip. If the
		// switch was folded into a bus alongside the target NIC, skip:
		// the bus trunk + switch tail says it visually.
		for (const sw of switchList) {
			const swPortId = `switch:${sw.device_key}`;
			const swBus = busByMember.get(swPortId);
			const seen = new Set<string>();
			for (const c of sw.connections) {
				const key = `${sw.device_key}|${c.node}|${c.my_nic}`;
				if (seen.has(key)) continue;
				seen.add(key);
				const bp = `node:${c.node}|${c.my_nic}`;
				if (!portById.get(swPortId) || !portById.get(bp)) continue;
				// Folded into a shared bus → don't draw a separate cable.
				if (swBus && swBus === busByMember.get(bp)) continue;
				const matches = sw.connections.filter(
					x => x.node === c.node && x.my_nic === c.my_nic);
				const protos = Array.from(new Set(matches.map(x => x.protocol)));
				const portIds = Array.from(new Set(matches.map(x => x.port_id || '?')));
				out.push({
					id: `cable:sw|${key}`,
					a: { portId: swPortId, nodeOrDevice: sw.device_key,
					     nic: portIds.join('/') },
					b: { portId: bp, nodeOrDevice: c.node, nic: c.my_nic },
					kind: 'switch',
					title: `${shortName(c.node)}/${c.my_nic}  →  ${sw.system_name || sw.device_key} ${portIds.join('/')}`,
					infoLines: [
						`${shortName(c.node)} · ${c.my_nic}`,
						`↕`,
						`${sw.system_name || sw.device_key} · port ${portIds.join('/')}`,
						`heard via ${protos.join(' / ')}`,
						sw.mgmt_ip ? `mgmt: ${sw.mgmt_ip}` : '',
						`last seen ${fmtAge(c.last_seen)}`,
					].filter(Boolean) as string[],
					touchesCore: !!coreNode && c.node === coreNode,
				});
			}
		}
		// Node↔node cables (from topology.links).
		// Shared/LAN cables that belong to a multi-member bus are
		// rendered as part of the bus trunk + tails — exclude them
		// here so they're not also drawn as point-to-point cables.
		for (const L of $topology.links) {
			const ap = `node:${L.node_a}|${L.nic_a}`;
			const bp = `node:${L.node_b}|${L.nic_b}`;
			if (!portById.get(ap) || !portById.get(bp)) continue;
			const aKey = `${L.node_a}|${L.nic_a}`;
			const bKey = `${L.node_b}|${L.nic_b}`;
			const aBus = busByMember.get(ap);
			if (aBus && aBus === busByMember.get(bp)) continue;  // folded into bus
			const kindA = kindByNic.get(aKey) ?? 'p2p';
			const kindB = kindByNic.get(bKey) ?? 'p2p';
			const kind: 'lan' | 'shared' | 'p2p' =
				(kindA === 'lan' || kindB === 'lan') ? 'lan' :
				(kindA === 'shared' || kindB === 'shared') ? 'shared' : 'p2p';
			out.push({
				id: `cable:mesh|${L.node_a}|${L.nic_a}|${L.node_b}|${L.nic_b}`,
				a: { portId: ap, nodeOrDevice: L.node_a, nic: L.nic_a },
				b: { portId: bp, nodeOrDevice: L.node_b, nic: L.nic_b },
				kind,
				title: `${shortName(L.node_a)}/${L.nic_a}  ↔  ${shortName(L.node_b)}/${L.nic_b}`,
				infoLines: [
					`${shortName(L.node_a)} · ${L.nic_a}`,
					`↔`,
					`${shortName(L.node_b)} · ${L.nic_b}`,
					`type: ${kind === 'lan' ? 'LAN (shared)' :
						 kind === 'shared' ? 'mesh (shared bus)' :
						 'direct point-to-point'}`,
					L.speed_mbps ? `speed: ${fmtSpeed(L.speed_mbps)}` : '',
					L.rtt_us ? `RTT: ${L.rtt_us} µs` : '',
					L.blip_total ? `blips: ${L.blip_total}` : '',
					`last seen ${fmtAge(L.last_seen)}`,
				].filter(Boolean) as string[],
				touchesCore: !!coreNode &&
					(L.node_a === coreNode || L.node_b === coreNode),
			});
		}
		return out;
	});

	// ─── Channel lane allocation ────────────────────────────────────────
	// Each cable routes in one of two channels:
	//   * upper channel — between switch row and node row, for switch↔node cables
	//   * lower channel — below the node row, for node↔node cables
	// Within a channel, each cable is given a Y-track such that
	// horizontally-overlapping cables NEVER occupy the same track.
	type Routed = Cable & {
		ax: number; ay: number; bx: number; by: number;
		channelY: number;        // Y of the horizontal traversal segment
		path: string;            // SVG path string
	};
	type RoutedBus = {
		id: string;
		kind: 'lan' | 'shared';
		channelY: number;
		leftX: number;
		rightX: number;
		tails: { x: number; yEdge: number; portId: string }[];
		infoLines: string[];
		touchesCore: boolean;
	};

	let routedItems = $derived.by((): { cables: Routed[]; buses: RoutedBus[] } => {
		const pById = portById;
		// THREE channels:
		//   * upper — between switch row and the topmost node row; used
		//     for every switch uplink (sw ↔ node).
		//   * mid   — only meaningful in star mode: between the core row
		//     and the peer row, dedicated to core↔peer cables so they
		//     never cross through any node box.
		//   * lower — below the deepest node row; for any cable that
		//     wasn't placed in upper or mid (in flat mode that's every
		//     mesh cable; in star mode it's peer↔peer cables).
		type ItemCable = { kind: 'cable'; cable: Cable; xL: number; xR: number };
		type ItemBus   = { kind: 'bus';   bus:   Bus;   xL: number; xR: number;
			ports: PortDef[]; touchesCore: boolean };
		type Item = ItemCable | ItemBus;
		const buckets: { upper: Item[]; mid: Item[]; lower: Item[] } =
			{ upper: [], mid: [], lower: [] };

		for (const c of cables) {
			const ap = pById.get(c.a.portId);
			const bp = pById.get(c.b.portId);
			if (!ap || !bp) continue;
			const xL = Math.min(ap.x, bp.x), xR = Math.max(ap.x, bp.x);
			const item: ItemCable = { kind: 'cable', cable: c, xL, xR };
			if (c.kind === 'switch') buckets.upper.push(item);
			else if (layout.isStar && c.touchesCore) buckets.mid.push(item);
			else buckets.lower.push(item);
		}

		for (const b of buses) {
			const ports: PortDef[] = [];
			for (const k of b.memberPortIds) {
				const p = pById.get(k);
				if (p) ports.push(p);
			}
			if (ports.length < 2) continue;
			const xs = ports.map(p => p.x);
			const xL = Math.min(...xs), xR = Math.max(...xs);
			const touchesCore = !!coreNode &&
				b.memberPortIds.some(k => k === `node:${coreNode}|br0` ||
					k.startsWith(`node:${coreNode}|`));
			const hasSwitch = b.memberPortIds.some(k => k.startsWith('switch:'));
			const item: ItemBus = { kind: 'bus', bus: b, xL, xR, ports, touchesCore };
			// Channel placement rules:
			//   * Bus with a switch member → UPPER channel (just below
			//     switches), so the switch tail is short and node tails
			//     come up to it from the node row below.
			//   * Bus in star mode that includes the core → MID channel
			//     between core and peer rows.
			//   * Otherwise → LOWER channel.
			if (hasSwitch) buckets.upper.push(item);
			else if (layout.isStar && touchesCore) buckets.mid.push(item);
			else buckets.lower.push(item);
		}

		// Where does the cable enter/exit a given port's owner rect,
		// given the channel Y? If the channel is above the owner box,
		// exit at the top edge; if below, exit at the bottom edge.
		function yEdgeForChannel(port: PortDef, channelY: number): number {
			if (channelY <= port.ownerY) return port.ownerY;
			if (channelY >= port.ownerY + port.ownerH) return port.ownerY + port.ownerH;
			// channel inside the owner — fall back to top edge
			return port.ownerY;
		}

		function allocate(bucket: Item[], baseY: number): {
			cables: Routed[]; buses: RoutedBus[];
		} {
			// Buses first (they tend to be wider; placing them on the
			// outermost tracks keeps point-to-point cables in close
			// lanes near the boxes).
			const sorted = [...bucket].sort((a, b) => {
				if (a.kind !== b.kind) return a.kind === 'bus' ? -1 : 1;
				return a.xL - b.xL;
			});
			const trackRightEdge: number[] = [];
			const outC: Routed[] = [];
			const outB: RoutedBus[] = [];
			for (const item of sorted) {
				let track = -1;
				for (let t = 0; t < trackRightEdge.length; t++) {
					if (item.xL > trackRightEdge[t] + 30) { track = t; break; }
				}
				if (track === -1) {
					track = trackRightEdge.length;
					trackRightEdge.push(item.xR);
				} else {
					trackRightEdge[track] = item.xR;
				}
				const channelY = baseY + track * LANE_STEP;

				if (item.kind === 'cable') {
					const ap = pById.get(item.cable.a.portId)!;
					const bp = pById.get(item.cable.b.portId)!;
					const ayEdge = yEdgeForChannel(ap, channelY);
					const byEdge = yEdgeForChannel(bp, channelY);
					const path = [
						`M ${ap.x} ${ayEdge}`,
						`L ${ap.x} ${channelY}`,
						`L ${bp.x} ${channelY}`,
						`L ${bp.x} ${byEdge}`,
					].join(' ');
					outC.push({
						...item.cable,
						ax: ap.x, ay: ayEdge, bx: bp.x, by: byEdge,
						channelY, path,
					});
				} else {
					const tails = item.ports.map(p => ({
						x: p.x,
						yEdge: yEdgeForChannel(p, channelY),
						portId: `node:${p.ownerId}|${p.nic}`,
					}));
					outB.push({
						id: item.bus.id,
						kind: item.bus.kind,
						channelY,
						leftX: item.xL,
						rightX: item.xR,
						tails,
						infoLines: item.bus.infoLines,
						touchesCore: item.touchesCore,
					});
				}
			}
			return { cables: outC, buses: outB };
		}

		const upperBase = SWITCH_Y + SWITCH_H + SWITCH_CHANNEL_TOP_OFFSET;
		const midBase = NODE_Y_STAR_CORE + NODE_H + MESH_CHANNEL_TOP_OFFSET;
		let nodeBottomY = 0;
		for (const [, p] of layout.nodePos) {
			nodeBottomY = Math.max(nodeBottomY, p.y + NODE_H);
		}
		const lowerBase = nodeBottomY + MESH_CHANNEL_TOP_OFFSET;
		const r1 = allocate(buckets.upper, upperBase);
		const r2 = allocate(buckets.mid,   midBase);
		const r3 = allocate(buckets.lower, lowerBase);
		return {
			cables: [...r1.cables, ...r2.cables, ...r3.cables],
			buses:  [...r1.buses,  ...r2.buses,  ...r3.buses],
		};
	});

	let routedCables = $derived(routedItems.cables);
	let routedBuses  = $derived(routedItems.buses);

	// Compute total SVG height once we know how many tracks each channel needs.
	let svgH = $derived.by(() => {
		const maxY = routedCables.reduce((m, c) => Math.max(m, c.channelY), 0);
		return Math.max(560, maxY + 80);   // 80px bottom padding
	});

	// ─── Highlight logic ────────────────────────────────────────────────
	// Hovering a port → dim everything not touching it.
	// Hovering a cable → dim everything not on it.
	// No hover, no core → nothing dimmed.
	// No hover, core set → cables not touching core are dimmed.
	let dim = $derived.by(() => {
		return {
			port: (id: string): boolean => {
				if (hoverBusId) {
					const b = routedBuses.find(x => x.id === hoverBusId);
					return !b || !b.tails.some(t => t.portId === id);
				}
				if (hoverCableId) {
					const c = cables.find(x => x.id === hoverCableId);
					return !c || (c.a.portId !== id && c.b.portId !== id);
				}
				if (hoverPortId) {
					if (hoverPortId === id) return false;
					// Highlight ports connected via any cable OR via a bus
					const bus = routedBuses.find(b => b.tails.some(t => t.portId === hoverPortId));
					if (bus && bus.tails.some(t => t.portId === id)) return false;
					return !cables.some(c =>
						(c.a.portId === id && c.b.portId === hoverPortId) ||
						(c.b.portId === id && c.a.portId === hoverPortId));
				}
				return false;
			},
			cable: (c: Routed): boolean => {
				if (hoverBusId) return true;       // any bus hover dims all cables
				if (hoverCableId) return hoverCableId !== c.id;
				if (hoverPortId)
					return c.a.portId !== hoverPortId && c.b.portId !== hoverPortId;
				if (coreNode) return !c.touchesCore;
				return false;
			},
			bus: (b: RoutedBus): boolean => {
				if (hoverBusId) return hoverBusId !== b.id;
				if (hoverCableId) return true;     // cable hover dims buses
				if (hoverPortId) return !b.tails.some(t => t.portId === hoverPortId);
				if (coreNode) return !b.touchesCore;
				return false;
			},
		};
	});

	// ─── Mouse handlers ─────────────────────────────────────────────────
	function onPortEnter(ev: MouseEvent, port: PortDef) {
		hoverPortId = port.id;
		const peers: string[] = [];
		for (const c of cables) {
			if (c.a.portId === port.id) peers.push(`${shortName(c.b.nodeOrDevice)}/${c.b.nic}`);
			if (c.b.portId === port.id) peers.push(`${shortName(c.a.nodeOrDevice)}/${c.a.nic}`);
		}
		hoverInfo = {
			x: ev.clientX, y: ev.clientY,
			lines: [
				`${port.owner === 'node' ? shortName(port.ownerId) : (switchList.find(s => s.device_key === port.ownerId)?.system_name || port.ownerId)}`,
				`port ${port.nic}`,
				port.kind === 'lan'    ? 'kind: LAN (shared)' :
				port.kind === 'shared' ? 'kind: mesh (shared bus)' :
				port.kind === 'p2p'    ? 'kind: direct point-to-point' :
				'kind: switch port',
				`${peers.length} cable${peers.length === 1 ? '' : 's'}: ${peers.join(', ') || '—'}`,
			],
		};
	}
	function onCableEnter(ev: MouseEvent, cable: Routed) {
		hoverCableId = cable.id;
		hoverInfo = { x: ev.clientX, y: ev.clientY, lines: cable.infoLines };
	}
	function onBusEnter(ev: MouseEvent, bus: RoutedBus) {
		hoverBusId = bus.id;
		hoverInfo = { x: ev.clientX, y: ev.clientY, lines: bus.infoLines };
	}
	function onLeave() {
		hoverPortId = null;
		hoverCableId = null;
		hoverBusId = null;
		hoverInfo = null;
	}
	function onMouseMove(ev: MouseEvent) {
		if (hoverInfo) hoverInfo = { ...hoverInfo, x: ev.clientX, y: ev.clientY };
	}
	function toggleCore(name: string) {
		coreNode = (coreNode === name) ? '' : name;
	}

	// ─── Peer-pair grouping for the detail table ────────────────────────
	type PairRow = { a: string; b: string; links: TopologyLink[] };
	let pairRows = $derived.by((): PairRow[] => {
		const m = new Map<string, PairRow>();
		for (const L of $topology.links) {
			const key = `${L.node_a}|${L.node_b}`;
			let r = m.get(key);
			if (!r) { r = { a: L.node_a, b: L.node_b, links: [] }; m.set(key, r); }
			r.links.push(L);
		}
		const out = Array.from(m.values());
		out.sort((x, y) => (x.a + '|' + x.b).localeCompare(y.a + '|' + y.b));
		for (const r of out) {
			r.links.sort((p, q) => {
				if (p.nic_a === 'br0' && q.nic_a !== 'br0') return -1;
				if (q.nic_a === 'br0' && p.nic_a !== 'br0') return 1;
				return (p.nic_a + p.nic_b).localeCompare(q.nic_a + q.nic_b);
			});
		}
		return out;
	});
</script>

<svelte:head><title>Topology — Bedrock</title></svelte:head>

<div class="header">
	<h1>Physical topology</h1>
	<span class="meta">
		{$topology.switch_count} switch{$topology.switch_count === 1 ? '' : 'es'}
		· {$topology.link_count} cluster cable{$topology.link_count === 1 ? '' : 's'}
		· {$topology.node_count} node{$topology.node_count === 1 ? '' : 's'}
		reporting · refreshed {fmtAge($topology.computed_at)}
	</span>
</div>

<p class="explainer">
	Patch-panel schematic. Switches across the top, cluster nodes across
	the middle. Every NIC is drawn as a labeled port; cables route
	orthogonally through dedicated channels so they never overlap.
	Hover a port or a cable for details.
	{#if coreNode}
		<strong>Showing only cables touching {shortName(coreNode)}</strong> —
		click the box again or press <em>Reset · show all</em> to release.
	{:else}
		Click a node box to centre it.
	{/if}
</p>

<div class="legend">
	<span class="legend-item"><svg width="32" height="10" viewBox="0 0 32 10"><line x1="2" y1="5" x2="30" y2="5" class="lk lk-switch"/></svg> switch uplink (LLDP / CDP / MNDP)</span>
	<span class="legend-item"><svg width="32" height="10" viewBox="0 0 32 10"><line x1="2" y1="5" x2="30" y2="5" class="lk lk-lan"/></svg> LAN (shared via operator router)</span>
	<span class="legend-item"><svg width="32" height="10" viewBox="0 0 32 10"><line x1="2" y1="5" x2="30" y2="5" class="lk lk-shared"/></svg> mesh shared bus</span>
	<span class="legend-item"><svg width="32" height="10" viewBox="0 0 32 10"><line x1="2" y1="5" x2="30" y2="5" class="lk lk-p2p"/></svg> direct point-to-point</span>
	{#if coreNode}
		<button class="reset-btn" onclick={() => coreNode = ''}>Reset · show all</button>
	{/if}
</div>

<div class="diagram-wrap" onmousemove={onMouseMove}>
	<svg viewBox="0 0 {SVG_W} {svgH}" xmlns="http://www.w3.org/2000/svg"
		role="img" aria-label="Physical topology"
		onmouseleave={onLeave}>

		<!-- Cables (drawn first, boxes lie on top) -->
		{#each routedCables as c (c.id)}
			<g class="cable-group" class:dim={dim.cable(c)} class:hot={hoverCableId === c.id}>
				<path d={c.path} class="cable-hit"
					onmouseenter={(e) => onCableEnter(e, c)}
					onmouseleave={onLeave}/>
				<path d={c.path} class="cable cable-{c.kind}"/>
			</g>
		{/each}

		<!-- Buses: ONE thick horizontal trunk per bus + thin vertical
		     tails to each member port, replacing the N(N-1)/2 cables. -->
		{#each routedBuses as b (b.id)}
			<g class="bus-group" class:dim={dim.bus(b)} class:hot={hoverBusId === b.id}>
				<!-- Fat hit targets (invisible) so hover is forgiving -->
				<line x1={b.leftX} y1={b.channelY} x2={b.rightX} y2={b.channelY}
					class="bus-hit"
					onmouseenter={(e) => onBusEnter(e, b)}
					onmouseleave={onLeave}/>
				{#each b.tails as t}
					<line x1={t.x} y1={t.yEdge} x2={t.x} y2={b.channelY}
						class="bus-hit"
						onmouseenter={(e) => onBusEnter(e, b)}
						onmouseleave={onLeave}/>
				{/each}
				<!-- Visible trunk (thick) -->
				<line x1={b.leftX} y1={b.channelY} x2={b.rightX} y2={b.channelY}
					class="bus-trunk bus-{b.kind}"/>
				<!-- Visible thin tails -->
				{#each b.tails as t}
					<line x1={t.x} y1={t.yEdge} x2={t.x} y2={b.channelY}
						class="bus-tail bus-{b.kind}"/>
				{/each}
				<!-- Tee dots at trunk/tail junctions -->
				{#each b.tails as t}
					<circle cx={t.x} cy={b.channelY} r="3"
						class="bus-tee bus-{b.kind}"/>
				{/each}
			</g>
		{/each}

		<!-- Switch boxes (rectangle = port array only) + LEFT-side label -->
		{#each switchList as sw}
			{@const p = layout.switchPos.get(sw.device_key)}
			{#if p}
				<g class="switch-box">
					<rect x={p.x} y={p.y} width={SWITCH_W} height={SWITCH_H} rx="6"
						class="switch-rect"/>
				</g>
				<!-- Label column to the LEFT of the rectangle. Right-aligned
				     so each line butts up to the rect. -->
				<g class="device-label switch-label">
					<text x={p.x - LABEL_GAP} y={p.y + SWITCH_H / 2 - 10}
						text-anchor="end" class="device-name">
						{sw.system_name || sw.device_key.toUpperCase()}
					</text>
					<text x={p.x - LABEL_GAP} y={p.y + SWITCH_H / 2 + 6}
						text-anchor="end" class="device-meta">
						{sw.mgmt_ip || sw.device_key}
					</text>
					<text x={p.x - LABEL_GAP} y={p.y + SWITCH_H / 2 + 20}
						text-anchor="end" class="device-proto">
						{sw.protocols.join(' · ')}
					</text>
				</g>
			{/if}
		{/each}

		<!-- Cluster-node boxes (rectangle = port array only) + LEFT-side label -->
		{#each clusterNodes as nodeName}
			{@const p = layout.nodePos.get(nodeName)}
			{@const isCore = coreNode === nodeName}
			{#if p}
				<g class="node-box" class:core={isCore}
					tabindex="0" role="button"
					aria-label="Focus on {nodeName}"
					onclick={() => toggleCore(nodeName)}
					onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCore(nodeName); } }}>
					<rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx="6"
						class="node-rect"/>
				</g>
				<g class="device-label node-label" class:core={isCore}
					onclick={() => toggleCore(nodeName)}>
					<text x={p.x - LABEL_GAP} y={p.y + NODE_H / 2 - 6}
						text-anchor="end" class="device-name">
						{shortName(nodeName)}
					</text>
					<text x={p.x - LABEL_GAP} y={p.y + NODE_H / 2 + 10}
						text-anchor="end" class="device-meta">
						{($nodes[nodeName]?.host) || ''}
					</text>
					{#if isCore}
						<text x={p.x - LABEL_GAP} y={p.y + NODE_H / 2 + 26}
							text-anchor="end" class="core-tag">▼ CORE VIEW</text>
					{/if}
				</g>
			{/if}
		{/each}

		<!-- Ports (drawn last so they sit on top of cable hit-targets) -->
		{#each ports as port (port.id)}
			<g class="port-group" class:dim={dim.port(port.id)}
				class:hot={hoverPortId === port.id}>
				<rect x={port.boxX} y={port.boxY}
					width={PORT_W} height={PORT_H} rx="2"
					class="port-rect port-{port.kind}"
					onmouseenter={(e) => onPortEnter(e, port)}
					onmouseleave={onLeave}/>
				<text x={port.x} y={port.boxY + PORT_H / 2 + 3}
					class="port-label" text-anchor="middle"
					pointer-events="none">
					{port.labelText}
				</text>
			</g>
		{/each}
	</svg>

	<!-- Floating info card -->
	{#if hoverInfo}
		<div class="tooltip"
			style="left:{hoverInfo.x + 14}px; top:{hoverInfo.y + 14}px">
			{#each hoverInfo.lines as L}
				<div>{L}</div>
			{/each}
		</div>
	{/if}
</div>

{#if $topology.switch_count === 0 && $topology.link_count === 0}
	<div class="empty">
		<p>No physical topology data yet.</p>
		<p class="muted">Cluster cables appear once bedrock-net's discovery
		hysteresis passes (~5 s after each cable comes up). Switches and
		routers appear once one full LLDP / CDP / MNDP cycle (~30–60 s)
		has flowed to the cluster's nodes.</p>
	</div>
{/if}

<!-- Detail panel: per-pair cables -->
{#if pairRows.length > 0}
	<h2 class="section">Cluster cables (node ↔ node) — detail</h2>
	<div class="cards">
		{#each pairRows as pair (pair.a + '|' + pair.b)}
			{@const dimmed = !!coreNode && pair.a !== coreNode && pair.b !== coreNode}
			<div class="card" class:dim={dimmed}>
				<div class="card-head">
					<div class="title">
						<span class="icon">↔</span>
						<span class="name">{shortName(pair.a)} ↔ {shortName(pair.b)}</span>
						<span class="tag pair-tag">{pair.links.length} cable{pair.links.length === 1 ? '' : 's'}</span>
					</div>
				</div>
				<table class="conns">
					<thead>
						<tr><th>{shortName(pair.a)} NIC</th><th>↔</th>
							<th>{shortName(pair.b)} NIC</th>
							<th>Type</th><th>Speed</th><th>RTT</th>
							<th>Blips</th><th>Last seen</th></tr>
					</thead>
					<tbody>
						{#each pair.links as L (L.nic_a + L.nic_b)}
							{@const k = kindByNic.get(`${L.node_a}|${L.nic_a}`) ?? 'p2p'}
							<tr>
								<td><code>{L.nic_a}</code></td>
								<td class="muted">↔</td>
								<td><code>{L.nic_b}</code></td>
								<td><span class="kind-tag kind-{k}">{
									k === 'lan' ? 'LAN' :
									k === 'shared' ? 'shared' :
									'direct'
								}</span></td>
								<td>{fmtSpeed(L.speed_mbps)}</td>
								<td>{L.rtt_us ? `${L.rtt_us} µs` : ''}</td>
								<td class:warn={L.blip_total > 0}>{L.blip_total > 0 ? L.blip_total : '0'}</td>
								<td class="muted">{fmtAge(L.last_seen)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/each}
	</div>
{/if}

<!-- Detail panel: switches -->
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
						{#if shared}<span class="tag shared-tag">shared by {peers.size} nodes</span>{/if}
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
		max-width: 920px; margin: 8px 0 12px;
	}

	.legend {
		display: flex; gap: 18px; align-items: center;
		font-size: 12px; color: #8b949e;
		margin: 4px 0 6px; flex-wrap: wrap;
	}
	.legend-item { display: inline-flex; align-items: center; gap: 6px; }
	.legend svg { vertical-align: middle; }
	.reset-btn {
		margin-left: auto;
		background: #d29922; color: #000; border: none;
		border-radius: 4px; padding: 3px 10px; cursor: pointer;
		font-size: 12px; font-weight: 600;
	}
	.reset-btn:hover { background: #f0b942; }

	.diagram-wrap {
		position: relative;
		background: #0d1117;
		border: 1px solid #21262d;
		border-radius: 8px;
		padding: 12px;
		margin-bottom: 16px;
		overflow-x: auto;            /* horizontal scroll if narrow viewport */
	}
	.diagram-wrap > svg {
		display: block;
		width: 100%;
		min-width: 1100px;           /* but never squish below readable */
		height: auto;
	}

	/* ─── Cables ─── */
	.cable {
		fill: none;
		stroke-width: 1.8;
		stroke-linejoin: round;
		stroke-linecap: round;
		pointer-events: none;
	}
	.cable-lan    { stroke: #8b949e; }
	.cable-shared { stroke: #3fb950; }
	.cable-p2p    { stroke: #d29922; }
	.cable-switch { stroke: #79c0ff; stroke-dasharray: 5 4; }

	.cable-hit {
		fill: none;
		stroke: transparent;
		stroke-width: 12;     /* fat invisible hit target */
		pointer-events: stroke;
		cursor: pointer;
	}
	.cable-group.dim { opacity: 0.10; }
	.cable-group.hot .cable { stroke-width: 3.5; filter: drop-shadow(0 0 4px currentColor); }

	/* ─── Bus (trunk + tails) ─── */
	.bus-trunk {
		fill: none;
		stroke-linecap: round;
		stroke-width: 5;
	}
	.bus-tail {
		fill: none;
		stroke-linecap: round;
		stroke-width: 2;
	}
	.bus-tee {
		stroke: none;
	}
	.bus-shared      .bus-trunk,
	.bus-shared.bus-trunk { stroke: #3fb950; }
	.bus-shared.bus-tail  { stroke: #3fb950; }
	.bus-shared.bus-tee   { fill:   #3fb950; }
	.bus-lan.bus-trunk    { stroke: #8b949e; }
	.bus-lan.bus-tail     { stroke: #8b949e; }
	.bus-lan.bus-tee      { fill:   #8b949e; }
	.bus-hit {
		fill: none;
		stroke: transparent;
		stroke-width: 14;
		pointer-events: stroke;
		cursor: pointer;
	}
	.bus-group.dim { opacity: 0.10; }
	.bus-group.hot .bus-trunk { stroke-width: 7; filter: drop-shadow(0 0 5px currentColor); }
	.bus-group.hot .bus-tail  { stroke-width: 3; }

	/* legend swatches */
	.lk { fill: none; stroke-width: 2; stroke-linecap: round; }
	.lk-lan    { stroke: #8b949e; }
	.lk-shared { stroke: #3fb950; }
	.lk-p2p    { stroke: #d29922; }
	.lk-switch { stroke: #79c0ff; stroke-dasharray: 5 4; }

	/* ─── Switch + node boxes ─── */
	.switch-rect {
		fill: #1f6feb22;
		stroke: #1f6feb;
		stroke-width: 1.5;
	}
	.node-rect {
		fill: #161b22;
		stroke: #30363d;
		stroke-width: 1.5;
		cursor: pointer;
	}
	.node-box.core .node-rect {
		fill: #d2992222;
		stroke: #d29922;
		stroke-width: 2;
	}
	.node-box:hover .node-rect { stroke: #58a6ff; }

	/* Device labels (name + IP + protos/core-tag) sit to the LEFT of
	   the rectangle, right-aligned so each line butts up to the rect. */
	.device-label { cursor: pointer; }
	.device-name {
		fill: #e6edf3; font-size: 13px; font-weight: 600;
		font-family: -apple-system, BlinkMacSystemFont, sans-serif;
	}
	.switch-label .device-name { fill: #79c0ff; }
	.node-label.core .device-name { fill: #d29922; }
	.device-meta {
		fill: #8b949e; font-size: 11px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.device-proto {
		fill: #6e7681; font-size: 10px; text-transform: uppercase;
		letter-spacing: 1px;
		font-family: -apple-system, BlinkMacSystemFont, sans-serif;
	}
	.core-tag {
		fill: #d29922; font-size: 10px; font-weight: 700;
		text-transform: uppercase; letter-spacing: 1px;
	}

	/* ─── Ports ─── */
	.port-rect {
		fill: #21262d;
		stroke: #30363d;
		stroke-width: 1.2;
		cursor: pointer;
	}
	.port-lan    { stroke: #8b949e; }
	.port-shared { stroke: #3fb950; }
	.port-p2p    { stroke: #d29922; }
	.port-switch { stroke: #79c0ff; }
	.port-group:hover .port-rect,
	.port-group.hot .port-rect {
		stroke-width: 2.4;
		fill: #30363d;
		filter: drop-shadow(0 0 4px currentColor);
	}
	.port-group.dim { opacity: 0.15; }

	.port-label {
		fill: #e6edf3; font-size: 9px; font-weight: 500;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		pointer-events: none;
		letter-spacing: -0.3px;
	}

	/* ─── Floating tooltip ─── */
	.tooltip {
		position: fixed;
		background: #0d1117f0;
		border: 1px solid #58a6ff;
		border-radius: 6px;
		padding: 8px 12px;
		font-size: 12px;
		color: #e6edf3;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		box-shadow: 0 4px 16px #0008;
		pointer-events: none;
		z-index: 10;
		max-width: 320px;
		line-height: 1.55;
	}
	.tooltip div:first-child { font-weight: 600; color: #79c0ff; font-size: 13px; }

	/* ─── Empty state ─── */
	.empty {
		background: #0d1117; border: 1px dashed #30363d; border-radius: 8px;
		padding: 24px; color: #c9d1d9;
	}
	.empty p { margin: 0 0 6px; }
	.muted { color: #6e7681; }

	/* ─── Detail cards ─── */
	.cards { display: grid; grid-template-columns: 1fr; gap: 16px; }
	.card {
		background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
		padding: 14px 16px 12px;
	}
	.card.shared {
		border-color: #1f6feb55;
		box-shadow: 0 0 0 1px #1f6feb22 inset;
	}
	.card.dim { opacity: 0.45; }
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
	.pair-tag   { background: #3fb95033; color: #56d364; }

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

	.kind-tag {
		display: inline-block; font-size: 10px; font-weight: 600;
		padding: 1px 6px; border-radius: 3px; letter-spacing: 0.5px;
	}
	.kind-lan    { background: #8b949e33; color: #c9d1d9; }
	.kind-shared { background: #3fb95033; color: #56d364; }
	.kind-p2p    { background: #d2992233; color: #d29922; }
	.warn { color: #d29922; font-weight: 600; }

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
