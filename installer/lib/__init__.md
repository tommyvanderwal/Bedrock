# installer/lib/__init__.py

Package marker for `installer.lib`, the Bedrock installer + cluster-protocol
library. It holds the modules that build and run a node: install/join flows
(`agent_install`, `mgmt_install`, `join_handshake`, `daemon_setup`,
`os_setup`, `packages`, `hardware`); the unified `bedrock-d` daemon and its
parts (`netd`, `election`, `witness`, `cluster_arbiter`, `cluster_addr`);
state and consensus (`cluster_state`, `bedrock_state`, `state`,
`state_shared`, `rqlite_client`, `rqlite_setup`); storage and workloads
(`tier_storage`, `seaweedfs`, `workload`); auth and certs (`operator_auth`,
`peer_auth`, `cert_manager`, `cluster_ca`); discovery (`discovery`, `l2disc`,
`mdns_responder`); plus dashboard, observability, exporters, HTTP redirect,
and view-building helpers.

The `__init__.py` declares no code and re-exports nothing; importers reference
the submodules directly (e.g. `from installer.lib import cluster_state`). Each
`*.py` file has a companion `*.md` beside it.
