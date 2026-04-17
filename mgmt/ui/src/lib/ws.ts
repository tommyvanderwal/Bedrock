/**
 * Bedrock WebSocket client — multiplexed channels.
 * One connection carries: metrics, vm state, events, RPC responses.
 */

type MessageHandler = (data: any) => void;

class BedrockWS {
	private ws: WebSocket | null = null;
	private handlers: Map<string, MessageHandler[]> = new Map();
	private rpcId = 0;
	private rpcCallbacks: Map<number, (result: any) => void> = new Map();
	private reconnectTimer: number | null = null;

	connect() {
		const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
		this.ws = new WebSocket(`${proto}//${location.host}/ws`);

		this.ws.onmessage = (e) => {
			try {
				const msg = JSON.parse(e.data);
				const channel = msg.channel || 'unknown';

				// Handle RPC responses
				if (channel === 'rpc.response' && msg.id !== undefined) {
					const cb = this.rpcCallbacks.get(msg.id);
					if (cb) {
						cb(msg.result || msg.error);
						this.rpcCallbacks.delete(msg.id);
					}
					return;
				}

				// Dispatch to channel handlers
				const fns = this.handlers.get(channel);
				if (fns) {
					for (const fn of fns) fn(msg);
				}

				// Also dispatch to wildcard handlers
				const wildcard = this.handlers.get('*');
				if (wildcard) {
					for (const fn of wildcard) fn(msg);
				}
			} catch (err) {
				console.error('WS parse error:', err);
			}
		};

		this.ws.onclose = () => {
			this.scheduleReconnect();
		};

		this.ws.onerror = () => {
			this.ws?.close();
		};
	}

	private scheduleReconnect() {
		if (this.reconnectTimer) return;
		this.reconnectTimer = window.setTimeout(() => {
			this.reconnectTimer = null;
			this.connect();
		}, 2000);
	}

	on(channel: string, handler: MessageHandler) {
		if (!this.handlers.has(channel)) {
			this.handlers.set(channel, []);
		}
		this.handlers.get(channel)!.push(handler);
	}

	off(channel: string, handler: MessageHandler) {
		const fns = this.handlers.get(channel);
		if (fns) {
			const idx = fns.indexOf(handler);
			if (idx >= 0) fns.splice(idx, 1);
		}
	}

	/** Send an RPC command, returns a promise with the result */
	rpc(method: string, params: Record<string, any> = {}): Promise<any> {
		return new Promise((resolve, reject) => {
			const id = ++this.rpcId;
			this.rpcCallbacks.set(id, resolve);
			this.send({ channel: 'rpc', method, params, id });

			// Timeout after 30s
			setTimeout(() => {
				if (this.rpcCallbacks.has(id)) {
					this.rpcCallbacks.delete(id);
					reject(new Error('RPC timeout'));
				}
			}, 30000);
		});
	}

	send(data: any) {
		if (this.ws?.readyState === WebSocket.OPEN) {
			this.ws.send(JSON.stringify(data));
		}
	}

	get connected(): boolean {
		return this.ws?.readyState === WebSocket.OPEN;
	}
}

export const ws = new BedrockWS();
