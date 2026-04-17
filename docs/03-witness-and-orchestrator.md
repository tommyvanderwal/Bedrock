# Bedrock Witness & Failover Orchestrator

## Architecture Overview

```
                    ┌─────────────────────────┐
                    │    MikroTik CRS310       │
                    │    192.168.2.253         │
                    │                         │
                    │  ┌───────────────────┐  │
                    │  │ bedrock-witness    │  │
                    │  │ container          │  │
                    │  │ 192.168.2.252:9443 │  │
                    │  │                   │  │
                    │  │ 611KB Rust binary  │  │
                    │  │ ARM32, static     │  │
                    │  └───────────────────┘  │
                    └────────┬────────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              │              ▼
  ┌───────────────────┐      │   ┌───────────────────┐
  │      NODE 1       │      │   │      NODE 2       │
  │  192.168.2.141    │      │   │  192.168.2.142    │
  │                   │      │   │                   │
  │  bedrock-failover │      │   │  bedrock-failover │
  │  (systemd service)│      │   │  (systemd service)│
  │                   │      │   │                   │
  │  Sends heartbeat ─┼──────┘───┼── Sends heartbeat │
  │  every 3 seconds  │          │  every 3 seconds  │
  │                   │          │                   │
  │  Queries status ──┼──────────┼── Queries status  │
  │  every 2 seconds  │          │  every 2 seconds  │
  └───────────────────┘          └───────────────────┘
```

## Witness Service — What It Does

The witness is a **passive heartbeat tracker**. It does NOT make decisions.
It only answers: "Who have I heard from recently?"

### Endpoints

```
  POST /heartbeat/{node}              Register heartbeat from a node
  POST /heartbeat/{node}/{resource}   Register heartbeat for a specific
                                      DRBD resource on a node
  GET  /status                        Return all nodes' liveness
  GET  /health                        Simple health check (200 OK)
```

### Status Response Example

```json
  {
    "nodes": {
      "node1": {
        "alive": true,
        "last_seen_ms_ago": 1500,
        "resources": {
          "vm-test-disk0": { "alive": true, "last_seen_ms_ago": 1500 },
          "vm-win-disk0":  { "alive": true, "last_seen_ms_ago": 1500 }
        }
      },
      "node2": {
        "alive": false,
        "last_seen_ms_ago": 15000,
        "resources": {
          "vm-test-disk0": { "alive": false, "last_seen_ms_ago": 15000 },
          "vm-win-disk0":  { "alive": false, "last_seen_ms_ago": 15000 }
        }
      }
    },
    "witness_uptime_secs": 3600
  }
```

### Liveness Logic

```
  A node is "alive" if:  last_heartbeat < TIMEOUT ago  (default: 10s)
  A node is "dead" if:   last_heartbeat >= TIMEOUT ago

  The witness does NOT:
  - Make failover decisions
  - Contact the nodes
  - Store state to disk (all in-memory, restarts clean)
  - Require any configuration (nodes register themselves)
```

## Failover Orchestrator — What It Does

The orchestrator is a **Python script** running as a systemd service
on each node. It is the decision maker.

### Service Details

```
  Service:     bedrock-failover.service
  Binary:      /usr/local/bin/bedrock-failover.py
  Auto-start:  yes (WantedBy=multi-user.target)
  Restart:     always (5s delay)

  Node 1 runs:  --node node1 --peer node2
  Node 2 runs:  --node node2 --peer node1
```

### Quorum Model — 2 of 3 Votes

```
  Three voters in the cluster:

  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  NODE 1  │     │  NODE 2  │     │ WITNESS  │
  │  Vote 1  │     │  Vote 2  │     │  Vote 3  │
  └──────────┘     └──────────┘     └──────────┘

  A node needs 2 of 3 votes to have quorum.
  It always has its own vote (1). It needs ONE more:
    - Peer reachable (via direct cable OR switch) = vote 2
    - Witness reachable + confirms peer dead     = vote 3

  Quorum scenarios:
  ┌───────────────────────────────────────────────────────────┐
  │ Direct cable  Switch LAN  Witness    Quorum?  Action      │
  │─────────────────────────────────────────────────────────── │
  │    OK           OK         OK        YES      Normal      │
  │    FAIL         OK         OK        YES      Normal*     │
  │    OK           FAIL       FAIL      YES      Normal*     │
  │    FAIL         FAIL       OK        YES**    TAKEOVER    │
  │    FAIL         FAIL       FAIL      NO       FREEZE      │
  │─────────────────────────────────────────────────────────── │
  │  * DRBD still replicating via the surviving path          │
  │ ** Peer confirmed dead by witness = safe to take over     │
  └───────────────────────────────────────────────────────────┘
```

### Decision Loop

```
  Every 2 seconds:
  ┌──────────────────────────────────────────────────┐
  │                                                  │
  │  1. Send heartbeat to witness                    │
  │     POST /heartbeat/{my-node}                    │
  │     POST /heartbeat/{my-node}/{res}              │
  │                                                  │
  │  2. TCP ping peer on BOTH paths                  │
  │     10.99.0.x:22  (direct cable)                 │
  │     192.168.2.x:22 (via switch)                  │
  │                                                  │
  │  3. Query witness: GET /status                   │
  │                                                  │
  │  4. Evaluate quorum:                             │
  │                                                  │
  │     ┌─────────────────────────────┐              │
  │     │ Peer reachable              │              │
  │     │ (either path)?              │              │
  │     └────┬──────────────┬─────────┘              │
  │          │YES           │NO                      │
  │          ▼              ▼                         │
  │     ┌─────────┐   ┌────────────────────┐         │
  │     │ ALL OK  │   │ Witness reachable? │         │
  │     │ reset   │   └──┬─────────┬───────┘         │
  │     │ counter │    YES│         │NO               │
  │     └─────────┘       ▼         ▼                │
  │                  ┌─────────┐ ┌──────────────┐    │
  │                  │ Witness │ │ ISOLATED     │    │
  │                  │ says    │ │ No quorum    │    │
  │                  │ peer    │ │ (1 of 3)     │    │
  │                  │ dead?   │ │ DO NOTHING   │    │
  │                  └──┬───┬──┘ └──────────────┘    │
  │                   YES  NO                        │
  │                    │    │                         │
  │                    ▼    ▼                         │
  │               ┌────────┐ ┌───────────────┐       │
  │               │ COUNT  │ │ Network issue │       │
  │               │ +1     │ │ (we can't see │       │
  │               └───┬────┘ │ peer but it's │       │
  │                   │      │ alive) HOLD   │       │
  │                   ▼      └───────────────┘       │
  │           ┌──────────────┐                       │
  │           │ count >= 3 ? │                       │
  │           └──┬────────┬──┘                       │
  │            NO│        │YES                       │
  │              ▼        ▼                          │
  │           ┌──────┐ ┌───────────────────┐         │
  │           │ WAIT │ │ TAKEOVER          │         │
  │           └──────┘ │ quorum: self +    │         │
  │                    │ witness = 2 of 3  │         │
  │                    │                   │         │
  │                    │ For each Secondary │         │
  │                    │ resource:          │         │
  │                    │  1. promote DRBD   │         │
  │                    │  2. start VM       │         │
  │                    └───────────────────┘         │
  └──────────────────────────────────────────────────┘
```

## Failure Scenarios

### Scenario 1: Node Crash (Power Failure)

```
  Time  Event
  ──────────────────────────────────────────────────────────
  T+0s  Node2 loses power
  T+0s  Node2's heartbeats stop
  T+10s Witness timeout: marks node2 dead
  T+10s Node1 orchestrator sees "peer dead" (1/3)
  T+12s Node1: "peer dead" (2/3)
  T+14s Node1: "peer dead" (3/3) → TAKEOVER
  T+14s Node1: drbdadm primary vm-win-disk0
  T+14s Node1: virsh start vm-win
  T+16s Windows VM booting on node1
  T+30s Windows VM fully operational

  Total: ~14s to takeover, ~30s to full service
```

### Scenario 2: Network Partition (Switch Failure)

```
  If the MikroTik switch fails:
  - Both nodes lose contact with witness
  - Both orchestrators see: "Cannot reach witness"
  - NEITHER node takes action
  - VMs keep running where they are
  - DRBD replication pauses (will resync when switch returns)

  This prevents split-brain: no witness = no promotion.
```

### Scenario 3: Node Returns After Failure

```
  Time  Event
  ──────────────────────────────────────────────────────────
  T+0s  Node2 powered back on
  T+30s Node2 boots, DRBD auto-starts as Secondary
  T+30s DRBD connects to node1, begins resync
  T+30s Node2 orchestrator starts, sends heartbeats
  T+30s Witness marks node2 alive again
  T+30s Node1 orchestrator sees "peer alive" — resets counter

  NO automatic failback. VMs stay on node1.
  Admin decides when to migrate VMs back to node2.
```

### Scenario 4: Orchestrator Crashes

```
  If the orchestrator crashes on a node:
  - systemd restarts it in 5 seconds
  - VMs keep running (orchestrator doesn't manage running VMs)
  - Heartbeats resume after restart
  - Worst case: 5s gap in heartbeats (within 10s timeout)

  If orchestrator is permanently broken:
  - VMs on that node keep running
  - Failover FROM that node still works (witness detects
    missing heartbeats, other node takes over)
  - Failover TO that node won't happen (but manual
    migration still works)
```

### Scenario 5: Witness Container Crashes

```
  If the witness crashes/restarts:
  - MikroTik auto-restarts the container (start-on-boot)
  - During restart (~1-2s):
    Both orchestrators see "cannot reach witness"
    Both do NOTHING (safe default)
  - After restart:
    Witness has empty state (in-memory only)
    Nodes re-register via heartbeats within 3s
    Normal operation resumes

  The witness is stateless by design. Restart is harmless.
```

## State Machine Summary

```
  ┌──────────────┐    peer heartbeat    ┌──────────────┐
  │              │    stops for 10s     │              │
  │  BOTH NODES  ├─────────────────────►│  ONE NODE    │
  │  HEALTHY     │                      │  DOWN        │
  │              │◄─────────────────────┤              │
  │  Each node   │    peer returns      │  Survivor    │
  │  runs its    │    resync begins     │  runs all    │
  │  own VMs     │    admin migrates    │  VMs         │
  └──────┬───────┘         back         └──────────────┘
         │
         │ switch/witness
         │ unreachable
         ▼
  ┌──────────────┐
  │              │
  │  ISOLATED    │
  │  (FREEZE)    │
  │              │
  │  No changes  │
  │  VMs keep    │
  │  running     │
  │  where they  │
  │  are         │
  └──────────────┘
```

## Timing Parameters

```
  Parameter                   Value    Where
  ──────────────────────────────────────────────────────
  Heartbeat interval          3s       orchestrator
  Witness timeout             10s      witness (env var)
  Peer check interval         2s       orchestrator
  Dead confirmations needed   3        orchestrator
  Orchestrator restart delay  5s       systemd
  DRBD resync speed           ~200MB/s over direct cable

  Worst-case takeover time:   10 + (3 × 2) = 16 seconds
  Best-case takeover time:    10 + (3 × 2) = 16 seconds
  (deterministic — not probabilistic)
```
