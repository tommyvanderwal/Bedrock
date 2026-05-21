"""bedrock-d's calm orchestration loop.

Owns: rqlite revision watcher, saga executor, service reconciler,
membership rebalancer. Runs as the asyncio task inside bedrock-d
(alongside the netd thread and the FastAPI app).
"""
