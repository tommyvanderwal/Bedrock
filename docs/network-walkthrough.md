# Cluster networking — a guided tour

This document explains what the cluster's networking is, what
happens when you plug nodes together, and how every device decides
which wire to send each packet over. It is meant for anyone
working on Bedrock — including people who have never touched a
routing protocol before. The companion to this doc is
`06-mesh-network.md`, which is the precise technical spec.

If at any point you want the deeper machinery (which RFC, which
sysctl, which line of code), that's the doc to flip to. This one
sticks to *what happens and why*.

---

## 1. Why we even need this

A Bedrock cluster is a few computers (call them **nodes**) that
together pretend to be one big computer. To do that, every node
needs to talk to every other node — all the time, fast, and
without operator hand-holding.

If we just plugged in one cable per pair, the cluster would work
until that one cable broke. Then half of it would go silent and
your VMs would freeze. To avoid that, we plug in **several
cables** between every pair of nodes:

```
                ┌────────────── home / office LAN (DHCP) ───────────────┐
                │   192.168.x.x — operator's normal flat network         │
                │                                                        │
   ┌────────┐ ┌─┴──────┐   ┌────────┐   ┌────────┐   ┌────────┐         │
   │ router │ │ node A │   │ node B │   │ node C │   │ node D │  …      │
   └────────┘ │  br0   │   │  br0   │   │  br0   │   │  br0   │  ←──────┘
              └────────┘   └────────┘   └────────┘   └────────┘
                  ║            ║            ║            ║
                  ╠════════════╬════════════╬════════════╣   mesh-plane 1 (10 G)
                  ║            ║            ║            ║
                  ╠════════════╬════════════╬════════════╣   mesh-plane 2 (10 G)
                  ║            ║            ║            ║
                  ╚════════════╩════════════╩════════════╝   mesh-plane 3 (10 G)
```

Every node has:

  * `br0` — a bridge that holds the **LAN** wire (the one to the
    home router). This is where DHCP gives it a normal address
    like `192.168.2.62`. SSH, the web dashboard, NFS for ISOs,
    and node bootstrap all use this wire.
  * `enp2s0`, `enp3s0`, `enp4s0` — the **mesh** wires. Each one
    goes to a separate switch (a "plane") so that one switch
    failure cannot cut the cluster apart.

So a 4-node cluster typically has **4 cables per node** (1 LAN +
3 mesh planes — you don't need a cable to yourself, and one
plane per pair-redundancy-level is enough). Between every pair
of nodes there are **4 distinct paths**. Lose one, the other
three keep working.

> The bedrock testbed actually runs 4 mesh planes (an extra one
> for stress-testing the multi-NIC code), which is why testbed
> verification output you'll see further down shows five paths
> per peer rather than four. The shape of the design is identical
> either way — just add or remove a plane.

That's the goal. The rest of this doc is about how a piece of
software called **bedrock-net** turns all that physical
redundancy into something the rest of the cluster can actually
use without thinking.

---

## 2. Two kinds of address, and why both

Real-world cluster software (DRBD for replication, libvirt for
VM migration, NFS, SSH, the dashboard …) wants to say "send this
to node B" and have it just work — without picking a specific
cable.

But TCP/IP doesn't work like that. Every packet has one
destination IP, and the kernel routing table picks one wire to
shove it down. So we have a layering problem: software addresses
*nodes*; networks address *cables*. Bedrock solves it by giving
every node both.

```
   ┌──────────────────────────────────────────────────────┐
   │                       node B                         │
   │                                                      │
   │   ┌──────────────────────────────────────────────┐   │
   │   │  Identity layer  —  who I am                 │   │
   │   │                                              │   │
   │   │       lo:  100.104.109.2/32                  │   │
   │   │                                              │   │
   │   │  Only one IP. Lives on the loopback device.  │   │
   │   │  Doesn't depend on any cable being plugged.  │   │
   │   │  Every cluster-internal protocol uses this.  │   │
   │   └──────────────────────────────────────────────┘   │
   │                                                      │
   │   ┌──────────────────────────────────────────────┐   │
   │   │  Reachability layer  —  where to find me     │   │
   │   │                                              │   │
   │   │       br0:    192.168.2.62/24    (LAN)       │   │
   │   │       enp2s0: 169.254.151.72/16  (mesh-1)    │   │
   │   │       enp3s0: 169.254.49.209/16  (mesh-2)    │   │
   │   │       enp4s0: 169.254.11.214/16  (mesh-3)    │   │
   │   │                                              │   │
   │   │  One IP per cable. These can change when a   │   │
   │   │  cable moves between switches.               │   │
   │   └──────────────────────────────────────────────┘   │
   └──────────────────────────────────────────────────────┘
```

### The identity address: 100.X.Y.N

The cluster picks one `/24` (an address block of 256 addresses)
for itself at install time, out of the **100.64.0.0/10** range
that IANA reserved for shared internal use. This range cannot
collide with an operator's normal LAN because no normal LAN is
ever allowed to use it.

The exact block — for example `100.104.109.0/24` — comes from
hashing the cluster's UUID. Two clusters in the same building
will almost certainly pick different blocks; the chance of a
collision is about 0.006 %.

Inside that block, the first node ("master") gets `.1`, the next
to join gets `.2`, etc. Once assigned, a node's identity address
never changes for the life of the cluster.

### The reachability addresses: 169.254.x.y

The mesh wires each get a **link-local** address from the
`169.254.0.0/16` range. This is the same range Windows /
macOS / Linux pick from when DHCP isn't available — IANA
reserved it for the "I have no DHCP, just give me *some* IP so I
can talk to whoever is on this cable" case. That's exactly what
mesh planes are: isolated cables with no DHCP and no operator
configuration.

NetworkManager picks the actual address: it sends a few ARP
probes to make sure nobody else on the same cable is using the
address it wants, then claims it and writes it to disk so the
same NIC keeps the same address across reboots.

### Why we need both layers

When the dashboard says "connect to node B", we want it to write
`https://100.104.109.2:8080` — that always works regardless of
which cables are healthy. The Linux kernel handles the actual
choice of cable, because:

  * The cluster identity addresses are `/32` — single-host —
    routes.
  * Each `/32` is installed with **one entry per cable to that
    node** (so four in our example: one for the LAN and one for
    each mesh plane), ordered by quality. (The exact ordering
    rule is in section 4.)
  * If the best cable goes dead, the kernel falls over to the
    next-best entry. Instantly. No application restart, no
    reconnect logic.

The cluster log (which is a separate Raft-style replicated log
that records "node B has joined", "node B's loopback IP is …")
tells the daemon *who is a member*. It does **not** tell anyone
which cable to use; that's decided fresh every second from
in-memory state.

---

## 3. The three jobs

`bedrock-net` is a small daemon that runs on every node. It does
three things, each on its own channel, each independent of the
other two. If any one of the three breaks, the cluster doesn't
fall apart — the other two keep working.

```
   ┌─────────────────────┬────────────────────┬─────────────────────┐
   │ Job 1               │ Job 2              │ Job 3               │
   │ Discovery           │ Latency            │ Routing             │
   │                     │                    │                     │
   │ "Who is on this     │ "How fast can I    │ "What's the best    │
   │  cable?"            │  reach them?"      │  path to every      │
   │                     │                    │  node?"             │
   ├─────────────────────┼────────────────────┼─────────────────────┤
   │ UDP multicast       │ ICMP echo (ping)   │ UDP unicast         │
   │ on port 7732        │ at the kernel      │ on port 7733        │
   │                     │ level              │                     │
   │ every 1 sec per     │ every 2 sec per    │ every 2 sec per     │
   │ cable               │ (peer, cable)      │ peer (any cable)    │
   │                     │                    │                     │
   │ HMAC-signed by      │ kernel timestamps  │ HMAC-signed by      │
   │ cluster_key         │ both ends          │ cluster_key         │
   └─────────────────────┴────────────────────┴─────────────────────┘
```

### Job 1: Discovery — "Anyone home?"

Every second, on every mesh cable, the daemon shouts:

> "I am node A on cable enp3s0. My loopback is 100.104.109.1.
>  Talk to me at 169.254.47.76 on this cable. Here is the time."

The shout is a **multicast** UDP packet sent to `239.7.7.7:7732`.
"Multicast" means everyone on the cable hears it without anyone
having to know everyone else's address up front. The packet is
signed with a secret key (`cluster_key`) so an unrelated machine
on the same physical wire can't fake one — the receiver verifies
the signature and silently drops anything it can't verify.

Receivers learn three things from a single probe:

  1. "Node A exists, and is reachable via this physical cable of
     mine."
  2. "On the other end of this cable, node A's interface is at
     IP 169.254.47.76."
  3. "Node A's cluster identity is 100.104.109.1."

The daemon stores this as a **Neighbour** record. It does *not*
trust it immediately: a single packet could be a transient.
After 5 seconds of continuous, gap-free probes, the neighbour
is marked `logged_up` — *now* it counts as a real path. This
delay is called **hysteresis** and exists because a flapping
cable should not produce 30 events per minute.

If the probes stop coming for **30 seconds**, the link is
declared down.

### Job 2: Latency — "How fast?"

Once a neighbour is logged up, the daemon starts pinging it —
literally, with ICMP echo, the same thing `ping` on your terminal
uses. Every 2 seconds, on each cable that connects us to that
neighbour, we send one ICMP echo request to the neighbour's
per-cable IP. The neighbour's kernel replies automatically; no
extra software is needed on the other side.

Why ICMP and not our own protocol? Three reasons:

  * **It's fast.** The kernel answers an echo request in
    microseconds. Our own protocol would have to wake up a
    Python process, parse a packet, and craft a reply — adding
    milliseconds of jitter.
  * **It's understood.** Operators can run `tcpdump`, `mtr`,
    `ping` and see exactly what we're doing.
  * **It's free.** No userspace echoer running on the peer.

```
            send_at:  t = 100 µs   (we record this)
                            │
                            ▼
        ┌───────────────────────────────────────────┐
        │  node A    icmp echo request   →   node B │
        │                                           │
        │       Linux kernel               Linux    │
        │       hot path                   kernel   │
        │                                  hot path │
        │                                           │
        │  ←   icmp echo reply                      │
        └───────────────────────────────────────────┘
                            ▲
                            │
            recv_at:  t = 280 µs   (we record this)

         RTT = 280 - 100 = 180 µs
```

The daemon then averages this round-trip time over many samples
using the **same formula TCP uses for its retransmit timer**
(RFC 6298, in case you want to look it up). The short version:

  * The smoothed value moves slowly toward each new sample, so
    one weird measurement can't yank the average around.
  * The daemon also tracks *variance* — how jittery the line is
    — so it knows what counts as "weird".

### Outliers, and why they don't matter

Every now and then a measurement will look insane: a 100 µs link
suddenly shows 230 ms. Almost always this is because the Linux
scheduler was busy, or a CPU was offline reordering memory, or a
network buffer briefly filled. It is **not** because the link
got 2000× slower for one packet.

So the daemon uses three rules to throw out junk before
averaging:

```
   ┌────────────────────────────────────────────────────────────┐
   │  Reject this sample if any of these are true:              │
   │                                                            │
   │    1. It's more than 4× the variance away from the         │
   │       running average. (Same rule TCP uses.)               │
   │                                                            │
   │    2. We already have a stable value over 100 µs, and      │
   │       this sample is 10× that. (Multiplicative cap.)       │
   │                                                            │
   │    3. We're on a sub-millisecond LAN, and this sample      │
   │       is over 100 ms. (Absolute floor.)                    │
   │                                                            │
   │  …unless we've rejected 3 in a row. Then the line has      │
   │  genuinely changed, and we trust the new value.            │
   └────────────────────────────────────────────────────────────┘
```

A 230 ms blip on a 100 µs LAN trips all three rules at once. The
average stays at 100 µs. No route change. No measurement
poisoning.

But — and this matters on a cluster that should be perfect most
of the time — the blip is **counted**. Each rejected sample
bumps a per-(peer, cable) counter, and the daemon's 30-second
status line reports the cluster-wide total and the most-recent
offender:

```
status neighbours=15 (logged_up=15); advertisers=[…]; transit_dests=[…];
       blips_total=105 last=314051us@18s_ago(bedrock-0acd31/enp5s0)
```

A clean cluster shows `blips_total=0` indefinitely. Anything
else is a signal — usually small (a kernel pause, a momentary
CPU contention), occasionally meaningful (a marginal switch
port, a flapping NIC, a thermal-throttled CPU starting to fail).

Each blip also prints a structured journal line:

```
bedrock-net: BLIP peer=bedrock-X my_nic=enp3s0 sample_us=230000
             srtt_us=120 rule=absolute streak=1 total=7
```

…which the per-node VLagent forwards to both redundant
VictoriaLogs backends.
Operators query

```
_msg:BLIP peer:bedrock-X | stats by (my_nic) count()
```

in LogsQL to see how often a specific link is misbehaving, or

```
_msg:BLIP | stats by (peer, my_nic) count()
```

for a cluster-wide ranking of trouble-spots.

To keep the log server sane, BLIP lines are **rate-limited to
one emit per (peer, cable) per 5 minutes**. Subsequent blips on
the same path during that window still bump the counter (and the
status line keeps reporting the rising total) — they just don't
each get their own journal entry. A flapping path can't flood
the log; a one-off blip still gets noticed.

### Job 3: Routing advertisement — "Best path?"

Every 2 seconds, each node sends one **signed unicast UDP
packet** to every other node it knows about, on port 7733. The
packet contains a list of paths the sender currently believes it
has:

```
   From: node A   (cluster identity 100.104.109.1)
   To:   node B   (cluster identity 100.104.109.2)
   Seq:  42       (incremented every cycle — receivers throw out replays)

   Paths I know:
     ─ dest: node B     via_chain: [A, B]       bw: 9400 Mbps   lat: 100 µs
     ─ dest: node C     via_chain: [A, C]       bw: 9300 Mbps   lat: 110 µs
     ─ dest: node D     via_chain: [A, D]       bw: 9400 Mbps   lat: 105 µs

   Signature: HMAC-SHA256(cluster_key, …)
```

This is sent **once per peer, no matter how many cables connect
us to that peer.** That's the architectural rule that keeps
this protocol small: if A is connected to B by four cables, A
still sends just **one** advertisement, and the kernel routing
table picks the best cable to send it on. (If the best cable
dies between sends, the kernel auto-fails-over and the next
advertisement goes via the next-best cable. No retry logic, no
application awareness.)

#### The `via_chain` and why it stops loops

Each advertised path carries the full list of nodes it claims to
go through. If node A says "I can reach node D via me → B → C →
D", and node C later receives that advertisement (after it's
been propagated), node C would see its own name *C* in the
via_chain and immediately drop the advertisement. Otherwise C
could end up installing a route to D that goes via A, which goes
via B, which goes via C, which goes via A, which … you get the
idea. The TTL would eventually save us, but loops in the routing
table are still horrible to operate around. The via_chain
prevents them by construction.

The internet's BGP routing protocol uses the exact same trick;
this is a stripped-down version of it for the inside of a
cluster.

#### Bandwidth and latency stack differently

Notice the advertisement carries both `bw` (bandwidth) and `lat`
(latency) as *raw, separate* numbers — not a pre-mixed quality
score. That's because:

  * **Bandwidth is the bottleneck.** If a path is A → 10G →
    B → 1G → C, the path's bandwidth is 1G, not the average.
    A receiver composes bandwidth with **min**.
  * **Latency adds up.** If A → B takes 100 µs and B → C takes
    200 µs, then A → B → C takes 300 µs. A receiver composes
    latency with **sum**.

If we mixed them on the advertiser's side, the receiver couldn't
do either correctly. Keeping them raw lets every receiver
compute its own score with its own observed bandwidth and
latency *to the advertiser*.

---

## 4. The metric — picking the best path

Every receiver computes a score for every (destination, next-hop)
combination it knows about. **Lower is better.** The formula is:

```
   ┌──────────────────────────────────────────────────────────┐
   │   score  =       1 000 000 / bandwidth_in_Mbps           │
   │                                                          │
   │              +   latency_in_µs / 100                     │
   │                                                          │
   │              +   50  if the link came up <60 s ago       │
   │                                                          │
   │              +   500 × min(1, loss × 20)                 │
   └──────────────────────────────────────────────────────────┘
```

Translated:

```
   bandwidth term            latency term
   ─────────────────         ─────────────────
   1 Gbps    → 1000           every 100 µs    → 1
   2.5 Gbps  →  400           every 1 ms      → 10
   10 Gbps   →  100           every 10 ms     → 100
   40 Gbps   →   25           every 100 ms    → 1000
   80 Gbps   →   12           every 1 s       → 10 000
   
   flap penalty              loss penalty
   ─────────────────         ─────────────────
   age < 60 s   +50          0% loss          → 0
   age ≥ 60 s   +0           1% loss          → 100
                             3% loss          → 300
                             5%+ loss         → 500 (capped)
```

So on a 4-node cluster with five 10 Gbps cables between every
pair, the score for every path is roughly:

```
   100 (bandwidth term) + 1 (latency term at 100 µs) + 0 + 0  ≈  101
```

…and the daemon picks one of them as the primary, the rest as
backups in score order. Where the scores tie, it tiebreaks by
RTT and then by cable name, so all nodes agree on the order
without negotiating.

### Why this formula?

It's a tuned version of EIGRP, an old Cisco routing protocol
from the 1990s. The original EIGRP weights were calibrated for
links between 56 kbps modems and 1.5 Mbps T1 lines. On a modern
LAN with 10G everywhere those weights would call every link
"infinitely fast" and stop discriminating. The numbers above are
the same shape but recalibrated so 1G feels slow, 10G is
average, and 80G feels fast — which matches today's real
hardware.

The **flap penalty** is additive (`+50`), not multiplicative
(`× 1.5`). That means it adds a *fixed* discouragement to brand-
new paths regardless of how good they look — predictable. A
fancy 80G path that just came up is still preferred over a 1G
path that's been stable for hours, because `12 + 50 = 62` beats
`1000`. But a new 10G path is *not* preferred over a stable 10G
path of equal quality, because `100 + 50 > 100`. Exactly the
behaviour we want.

---

## 5. The full convergence timeline

Now we can walk through what actually happens when a node powers
on, in time order.

```
   t = 0 s   ─┬─  bedrock-net.service starts
              │
              │   ├─  Reads /etc/bedrock/cluster.key   (32-byte HMAC key)
              │   ├─  Reads /etc/bedrock/state.json   (cluster_uuid, node_name)
              │   ├─  Reads /etc/bedrock/cluster.json (loopback_ip)
              │   ├─  Adds 100.X.Y.N/32 to lo  (identity)
              │   ├─  Opens UDP socket on port 7732   (discovery in/out)
              │   └─  Opens UDP socket on port 7733   (advertisement in/out)
              │
   t ≈ 1 s   ─┼─  Daemon walks /sys/class/net, sees the mesh NICs
              │   (enp2s0, enp3s0, enp4s0) plus br0 (the LAN bridge).
              │
              │   For each mesh cable: tells NetworkManager
              │       "give this NIC a link-local IP".
              │   NM does an ARP probe to make sure no one else has the
              │   address it wants, then claims it.
              │
              │   First discovery probes (Job 1) go out on every cable.
              │
   t ≈ 2 s   ─┼─  Discovery probes arrive from peers.
              │
              │   For every (peer_node, peer_cable, my_cable) the daemon
              │   sees a probe from, it creates a Neighbour record.
              │
              │   First ICMP echoes (Job 2) go out for those neighbours,
              │   but the first RTT samples are still settling.
              │
   t ≈ 3 s   ─┼─  First ICMP replies arrive. Per-(peer, my_cable) RTT
              │   averages start filling in.
              │
   t ≈ 5 s   ─┼─  LINK_UP hysteresis met: a Neighbour has been seen
              │   continuously for 5 seconds.
              │
              │   `logged_up` becomes true.
              │   On the mgmt master, a LINK_UP entry is appended to the
              │   cluster log so the rest of the cluster knows this path
              │   exists.
              │   `emit_routes` installs:
              │       • /32 link-local host route to the peer's per-cable IP
              │       • /32 route to the peer's loopback IP via that link,
              │         at metric 10 (the best of N direct paths)
              │       • /32 routes for other direct cables to the same peer
              │         at metrics 11, 12, 13 (backups; one per remaining
              │         cable — three in a 4-cable-per-node cluster)
              │       • a panic catch-all route for the cluster /24 at
              │         metric 999, via the freshest neighbour overall
              │
   t ≈ 7 s   ─┼─  First Job 3 advertisement round runs.
              │
              │   Each node sends one signed unicast to each peer it knows.
              │   Payload contains paths to every direct neighbour.
              │
   t ≈ 9 s   ─┼─  Second advertisement round. Now the adv_table is
              │   populated everywhere.
              │
              │   `recompute_best_transit_paths` runs every tick (250 ms).
              │   If any destination is NOT a direct neighbour, the best
              │   transit advertisement is selected; emit_routes installs
              │   a /32 to that destination at metric 100, via the chosen
              │   next-hop's link IP.
              │
   t = 10 s+ ─┴─  Cluster mesh ready.
              
                   • Every node has a /32 route to every other node's
                     loopback, with N backup metrics (failover free).
                   • Every node has a (smoothed, junk-free) RTT for every
                     direct path.
                   • Every node has each peer's view of the cluster, so
                     it can route around partial failures.
                   • DRBD's config has one `path` block per direct cable
                     plus a loopback fallback (built off the same data).
```

In a fully-meshed setup (every node has a cable to every other),
the *transit* part of step 9 produces no new routes — every
destination is already direct, with five backups. The transit
machinery exists for partial meshes (e.g. five nodes where only
some pairs are directly cabled) and for the moment after a real
failure when the transit path is what saves you.

---

## 6. A worked example: path-vector in motion

Let's take a three-node setup where A and C have **no direct
cable** — they can only reach each other through B. ASCII it:

```
        ┌─── 10G ───┐                ┌─── 10G ───┐
        │           │                │           │
    ┌───┴────┐   ┌──┴─────┐      ┌───┴────┐
    │ node A │═══╪ node B ╪══════╣ node C │
    └────────┘   └────────┘      └────────┘
                       (no direct cable from A to C)
```

### Round 1 — B advertises its direct neighbours

Both A and C receive B's advertisement, which lists:

```
   paths:
     - dest: A   via_chain: [B, A]   bw: 10000 Mbps   lat: 80 µs
     - dest: C   via_chain: [B, C]   bw: 10000 Mbps   lat: 75 µs
```

A reads this:

  * "Path to A via [B, A]" — that says *I*, A, am the destination.
    A is in the via_chain. Skip it (a route to yourself is
    nonsense).
  * "Path to C via [B, C]" — A's own name is NOT in the chain.
    Accept. Compute the cost:
        bw = min(10 000 Mbps, A's bw to B) = 10 000
        lat = 75 µs + A's RTT to B (say 90 µs) = 165 µs
        score = 100 + 1 = 101 (plus zero penalties)
    Install: `100.X.Y.C/32 via 169.254.??? dev <A's link to B> metric 100`

C performs the symmetric calculation and installs a route to A
via B. Done.

### Round 2 — A advertises what it knows

A's advertisement now contains:

```
   paths:
     - dest: B   via_chain: [A, B]            bw: 10000   lat: 90 µs   (direct)
     - dest: C   via_chain: [A, B, C]         bw: 10000   lat: 165 µs  (transit)
```

When this arrives at B, B reads the second entry, sees its own
name (B) inside the via_chain, drops it. **B does not learn a
weird "C via A" loop**. B sticks with its own direct path to C.

When this arrives at C, C sees the path "dest: C, via: [A, B,
C]" — C is the destination, skip. And the first path (dest: B,
direct) is already known via B's own advertisements.

### What happens when the A↔B cable is cut?

  * **t=0**: cable yanked.
  * **t≤30 s**: A's daemon stops seeing discovery probes from B
    on that cable. Hysteresis countdown.
  * Meanwhile, A's *other* cables to B (if any) still work — the
    kernel route table for B has additional backup entries at
    higher metrics (three more in a 4-cable cluster). The kernel
    auto-fails-over to the next-best. **Sub-second.** The
    application (libvirt, DRBD, dashboard) sees no error; the
    TCP connection keeps going.
  * **t=30 s**: LINK_DOWN hysteresis fires for that specific
    cable. The /32 route for that cable is removed. The route to
    B's loopback via that cable is also removed. Other cables to
    B keep their routes.

If A and B had **only that one cable**, then:

  * **t=30 s**: LINK_DOWN fires.
  * The daemon checks if any transit path to B exists. It
    looks at `best_transit_paths[B]`. If C's last advertisement
    (≤6 s old) listed a path to B, it's still in there.
  * `emit_routes` installs `B/32 via C at metric 100`.
  * Traffic flows A → C → B from now on. Slightly higher latency
    (one extra hop), but otherwise normal.

### What happens when a *node* dies?

Say B is power-cycled. From A's point of view:

  * t=0: B is gone.
  * t≤30 s: probes stop on every cable. Same hysteresis as before.
  * t=30 s: one LINK_DOWN event fires per cable to B (so four
    in our 4-cable example). The /32 routes to B's loopback all
    disappear.
  * Meanwhile, B's advertisements stop arriving. After 6 s the
    `adv_table[B]` entry is considered stale and falls out of
    `best_transit_paths`. So nothing tries to route via B for
    other peers either.
  * The cluster eventually records B as down via the witness
    mechanism (different system, see `cluster-quorum-spec.md`).

The whole thing self-heals, no operator intervention required.

---

## 7. Failure scenarios at a glance

The table below is a quick reference for "if X breaks, what
happens?":

```
   ┌─────────────────────────┬───────────────┬──────────────────────────┐
   │ Failure                 │ How detected  │ Recovery                 │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ One cable unplugged     │ LINK_DOWN     │ Kernel uses backup       │
   │                         │ at 30 s OR    │ /32 route immediately;   │
   │                         │ ICMP timeouts │ daemon cleans up         │
   │                         │ before that   │ metadata at 30 s.        │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ Cable cut silently      │ ICMP echoes   │ Same as above — once     │
   │ (link still "up" but    │ stop coming   │ ICMP loss is detectable, │
   │ no packets flow)        │ back; outlier │ kernel routes through    │
   │                         │ streak ⇒ slow │ a healthier path.        │
   │                         │ degradation   │                          │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ Switch dies (kills      │ All cables on │ Kernel fails over to     │
   │ one entire mesh plane)  │ that plane go │ a different plane on     │
   │                         │ silent at the │ every node simultaneously│
   │                         │ same time     │ — sub-second.            │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ Node power-cycle        │ Probes stop   │ Witness arbitration      │
   │                         │ on every NIC; │ kicks in for cluster     │
   │                         │ 30 s LINK_DOWN│ membership; mesh-side    │
   │                         │ ×N            │ withdraws all routes     │
   │                         │               │ to that node.            │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ Bridge merges two       │ Same neighbour│ Daemon detects "same     │
   │ mesh planes (operator   │ visible on    │ peer interface seen on   │
   │ patch cable)            │ two of our    │ two of my cables" — this │
   │                         │ cables        │ is a merge, NOT a real   │
   │                         │               │ collision. No action;    │
   │                         │               │ both paths stay routed.  │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ Two different peers     │ Same link-    │ ARP-defense countermea-  │
   │ pick the same           │ local IP on   │ sure: send 3 frames      │
   │ link-local IP across    │ two different │ 0.5 s apart claiming the │
   │ separate cables (cross- │ (peer_node,   │ address from our MAC.    │
   │ segment collision —     │ peer_nic)     │ Loser sees a different   │
   │ extremely rare)         │ tuples        │ MAC asserting "its" IP   │
   │                         │               │ and renumbers (RFC 3927  │
   │                         │               │ §2.5 behaviour).         │
   ├─────────────────────────┼───────────────┼──────────────────────────┤
   │ A 230 ms latency blip   │ ICMP echo     │ Sample rejected before   │
   │ on a 100 µs LAN         │ returns junk  │ averaging. No metric     │
   │                         │ for one round │ change. No routing       │
   │                         │               │ change. No log entry.    │
   │                         │               │ (Unless it persists 3    │
   │                         │               │ rounds — then real.)     │
   └─────────────────────────┴───────────────┴──────────────────────────┘
```

---

## 8. Why the three jobs are separate

If discovery, latency, and routing were one combined protocol,
*any* failure in the combined channel would cripple all three.
Real-world examples of why that matters:

  * If a router or firewall blocks ICMP (some corporate networks
    do this), the latency job stops working — but discovery and
    routing keep working. The daemon falls back to using only
    link speed in the metric. Cluster keeps running, slightly
    less optimally.
  * If multicast is blocked (some virtual-network setups do
    this), discovery stops working — but the cluster already
    converged and the routing advertisements alone are enough to
    keep paths refreshed for the 6-second stale window. Operator
    has time to notice and fix.
  * If for some reason the routing advertisement gets dropped
    (firewall misconfiguration), discovery and ICMP keep the
    direct paths alive. Only *transit* paths are lost. Direct
    paths cover most failures already.

Three independent channels means three independent failure
domains. No cascade.

The same logic applies to the cluster log. The log knows which
nodes are members; the mesh knows which cables work. The mesh
does **not** read the log to decide routing, and the log does
**not** depend on routing being optimal. If the log gets stuck
(consensus dies — say, two-of-four nodes are offline) the mesh
keeps routing around the survivors. If the mesh gets confused,
the log keeps recording membership. Each layer can survive the
other being broken.

---

## 9. Switch / router identity — the side-quest

The three protocols above tell us everything about Bedrock-on-
Bedrock: which nodes exist, how fast each path is, how to route
around failures. They tell us **nothing** about what the wires
are connected TO on the other end. Is `enp3s0` plugged into
office-switch port 7, or office-switch port 23, or a totally
different switch?

That's a question every operator eventually wants to answer.
Bedrock listens to three commonly-spoken switch-discovery
protocols so it can answer it without having to walk to the
switch console:

```
   ┌────────────────────┬───────────────────┬──────────────────┐
   │ LLDP               │ CDP               │ MNDP             │
   │ vendor-neutral     │ Cisco's,          │ MikroTik's       │
   │ IEEE standard      │ widely copied     │                  │
   │                    │                   │                  │
   │ EtherType 0x88CC,  │ 802.3 SNAP frame, │ UDP broadcast    │
   │ multicast to       │ multicast to      │ on port 5678     │
   │ 01:80:c2:00:00:0e  │ 01:00:0c:cc:cc:cc │                  │
   │                    │                   │                  │
   │ every ~30 s        │ every ~60 s       │ every ~30 s      │
   └────────────────────┴───────────────────┴──────────────────┘
```

All three carry roughly the same information: who I am (chassis
ID, system name), which port of mine you're plugged into (port
ID, port description), and how to reach me at my management IP.
Bedrock parses all three into a single shape so the rest of the
system doesn't care which protocol the switch happens to speak.

**Receive-only.** Bedrock never sends LLDP / CDP / MNDP frames
back. We just listen. That keeps this layer purely diagnostic:
swap a switch, move a cable, the parser notices within one TTL
cycle (≤ 2 minutes).

**Per-NIC table.** Each NIC keeps a tiny dict of
`{protocol → switch info}`. Two protocols from the same switch
(e.g. Aruba sends both LLDP and CDP) produce two entries — they
agree on the chassis ID so the dashboard groups them. A NIC
seeing two *different* chassis IDs is a misconfiguration worth
flagging.

**Live state file.** Every ~5 seconds the daemon rewrites
`/run/bedrock/switch_neighbors.json`:

```json
{
  "br0": {
    "cdp": {
      "chassis_id":   "office-sw-01",
      "system_name":  "office-sw-01",
      "port_id":      "vlan1",
      "mgmt_ip":      "192.168.2.253",
      "platform":     "MikroTik",
      "ttl_s":        121
    },
    "mndp": {
      "chassis_id":   "d4:01:c3:0e:7b:36",
      "system_name":  "office-sw-01",
      "port_id":      "vlan1",
      "system_descr": "RouterOS 7.20.6",
      "platform":     "MikroTik"
    }
  }
}
```

The mgmt master scrapes this file from every node every 3 s (via
the same SSH fan-out that already gathers DRBD / virsh / load
state for the dashboard), assembles a cluster-wide rollup **in
memory**, and ALSO caches it to `/run/bedrock/physical_topology.json`
on the mgmt node so a post-mortem inspection without the mgmt
service running is still possible. The live data is reachable
via `GET /api/topology`.

**It is never folded into `cluster.json`.** That file is reserved
for materialised cluster-log state — things the cluster has
reached consensus on (membership, master role, loopback
assignment). Switch identity is per-node local reality; it
doesn't need cluster consensus, so it stays out.

From this rollup a question like

> *Both `node A enp2s0` and `node B enp2s0` are plugged into
>  `office-sw-01`, ports `7` and `23` respectively.*

falls out by grouping per-node entries on `chassis_id` and
listing `(node, nic, port_id)` tuples per group. For dead-node
history (e.g. "what was node X's enp2s0 connected to last
Tuesday?") the dashboard queries VictoriaLogs LogsQL on the
`NIC_SWITCH` event stream instead.

**First-seen logging.** When a NIC first sees a switch (or when
the chassis ID under it changes — cable moved, switch replaced),
the daemon emits a structured `NIC_SWITCH` line to the journal:

```
bedrock-net: NIC_SWITCH my_nic=br0 protocol=cdp chassis=MikroTik
             system=MikroTik port=vlan1 mgmt=192.168.2.253
             platform=MikroTik ttl_s=121 reason=new
```

The per-node VLagent forwards that line to **both** redundant
VictoriaLogs backends (every Bedrock cluster runs two for
durability). Operators query in LogsQL:

```
_msg:NIC_SWITCH chassis:"office-sw-01" | stats by (my_nic, port) count()
```

…to see, across the whole cluster's history, which NICs have
ever been connected where. A node that's currently dead still
shows up — its last `NIC_SWITCH` emit is on file at both backends.

To keep the log server tidy, lines are rate-limited: a given
`(NIC, chassis_id)` pair re-emits only on **first observation**,
on **chassis change** (cable moved or switch swapped), or as a
**24-hour refresh**. Continuous steady-state operation produces
one journal line per NIC per day — exactly the heartbeat
operators want.

**Big picture:**

```
   ┌─────────┐                                           ┌─────────┐
   │ switch  │  ─ LLDP frame every 30 s ──────────────▶  │ node X  │
   │ (any)   │  ─ CDP frame every 60 s ───────────────▶  │ enp3s0  │
   └─────────┘                                           └────┬────┘
                                                              │
                                                              ▼
                                        ┌──────────────────────────────────┐
                                        │ bedrock-net daemon               │
                                        │  • parse, dedup by chassis_id    │
                                        │  • update switch_neighbors map   │
                                        │  • emit NIC_SWITCH on first-seen │
                                        │    or 24h refresh                │
                                        │  • write state file every 5 s    │
                                        └────────────┬─────────────────────┘
                                                     │
                            ┌────────────────────────┴───────────────────┐
                            ▼                                            ▼
              /run/bedrock/switch_neighbors.json            VLagent → 2× VictoriaLogs
              (per-node live view; mgmt master            (durable cluster-wide
              scrapes + rolls up in-memory                  history; queries via
              for the dashboard; never                       LogsQL — including for
              folded into cluster.json)                      dead-node lookups)
```

## 10. What an operator actually sees

This section is "I have a terminal, what should I look at?" —
in roughly the order you'd run things to verify everything is
healthy.

### Health-at-a-glance: the status line

`bedrock-net` prints a one-liner every 30 seconds:

```bash
journalctl -u bedrock-net | grep status
```

You should see something like:

```
status neighbours=15 (logged_up=15);
       advertisers=[bedrock-X(3p),bedrock-Y(3p),bedrock-Z(3p)];
       transit_dests=[bedrock-Y,bedrock-Z,bedrock-X]
```

Translation:

  * **neighbours=15 (logged_up=15)**: I see 15 cable endpoints
    (3 peers × 5 NICs each) and all of them have been stable for
    the 5-second up-hysteresis.
  * **advertisers=[…]**: Three other nodes are sending me
    advertisements, and each one currently has 3 paths it knows
    about. (Exactly what we'd expect for a 4-node, fully meshed
    setup — each peer can reach the 3 other peers.)
  * **transit_dests=[…]**: Three destinations have transit-path
    candidates in my table. (In a fully meshed cluster these
    aren't actually installed since direct routes win; they
    exist as live failover candidates.)

If `neighbours` drops or `advertisers` shrinks, something has
broken. The cluster log + the dashboard health page will tell
you what.

### The address layer

```bash
ip -br -4 addr | grep -v "127\|UNKNOWN.*lo\b"
```

Expected output (varies per node):

```
lo               UNKNOWN        100.104.109.1/32 …
br0              UP             192.168.2.60/24
enp2s0           UP             169.254.165.122/16
enp3s0           UP             169.254.47.76/16
enp4s0           UP             169.254.141.91/16
enp5s0           UP             169.254.29.52/16
```

One identity address (lo), one LAN address (br0), one mesh
address per mesh cable. If any of these are missing, that's the
first thing to fix.

### The route table

```bash
ip -4 route show | grep "^100\."
```

Expected output (some of):

```
100.104.109.0/24 via 169.254.72.115 dev enp5s0 metric 999
100.104.109.2 via 192.168.2.62 dev br0 metric 10
100.104.109.2 via 169.254.151.72 dev enp2s0 metric 11
100.104.109.2 via 169.254.49.209 dev enp3s0 metric 12
100.104.109.2 via 169.254.11.214 dev enp4s0 metric 13
100.104.109.2 via 169.254.248.218 dev enp5s0 metric 14
100.104.109.3 via …                 (same pattern, 5 paths)
100.104.109.4 via …                 (same pattern, 5 paths)
```

Reading this:

  * **`100.104.109.0/24 … metric 999`** — the panic catch-all.
    Used only if nothing else matches. Last resort.
  * Five entries per peer at metrics 10, 11, 12, 13, 14. The
    kernel uses metric 10 (the best) first. If that path goes
    silent (the device fails or the cable unplugs), the kernel
    automatically uses the metric-11 path for the next packet.
    No application restart, no awareness.

In a partial mesh you would also see entries at metric 100 —
those are transit routes via another node. They appear (and
disappear) automatically as paths come and go.

### The three protocols in action

In one terminal:

```bash
tcpdump -i any -nn 'udp port 7732 or icmp or udp port 7733' 2>/dev/null
```

You'll see, every cycle:

  * **Multicast probes** on port 7732 going out on each NIC at
    roughly 1-second intervals (Job 1).
  * **ICMP echo request/reply** pairs to per-cable IPs (Job 2).
  * **Three UDP unicasts** on port 7733 (one per peer in a
    4-node cluster) at roughly 2-second intervals (Job 3),
    sent to the peer's loopback `100.X.Y.N`.

If any of those streams is missing, you've found the broken job.

### What if there's no LINK_UP yet?

In the early seconds of a join, you might see
`neighbours=0 (logged_up=0)` even though probes are flying. That
is normal: hysteresis is waiting for 5 seconds of continuous
visibility. Re-check after 10 seconds.

If after 30 seconds you still see `neighbours=0`:

  * Is multicast working on the cable? Bridges with `multicast_
    snooping` enabled will silently drop our multicast. The
    testbed spawn script disables this; on real hardware,
    operators may need to too.
  * Is the cluster_key the same on both ends? A mismatched key
    means every probe fails HMAC verification and is silently
    dropped.
  * Is `ping_group_range` set? Job 2 uses unprivileged ICMP,
    which is gated by `/proc/sys/net/ipv4/ping_group_range`.

---

## 11. The big picture in one image

```
   ┌───────────────────────────────────────────────────────────────────┐
   │                       cluster.json                                │
   │                                                                   │
   │  "who is a member, what is their loopback IP, what tier are       │
   │   they in" — the cluster log, replicated via bedrock-rust         │
   │                                                                   │
   │   (membership-of-record; NOT routing-of-record)                   │
   └─────────────────┬─────────────────────────────────────────────────┘
                     │  consulted at startup + on every join/leave
                     ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                       bedrock-net daemon (per node)               │
   │                                                                   │
   │   ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐ │
   │   │ Job 1: Discovery │  │ Job 2: Latency   │  │ Job 3: Routes   │ │
   │   │ UDP multicast    │  │ ICMP echo        │  │ UDP unicast     │ │
   │   │ port 7732        │  │ kernel timing    │  │ port 7733       │ │
   │   │ every 1 s        │  │ every 2 s        │  │ every 2 s       │ │
   │   └──────────────────┘  └──────────────────┘  └─────────────────┘ │
   │             │                    │                     │          │
   │             ▼                    ▼                     ▼          │
   │   ┌────────────────────────────────────────────────────────────┐  │
   │   │   in-memory state (rebuilt every ~250 ms tick)             │  │
   │   │                                                            │  │
   │   │   Neighbours: who's on each cable, how long they've        │  │
   │   │   been there, what their RTT is                            │  │
   │   │                                                            │  │
   │   │   adv_table: each peer's view of the cluster               │  │
   │   │                                                            │  │
   │   │   best_transit_paths: lowest-score next-hop for every      │  │
   │   │   non-direct destination                                   │  │
   │   └────────────────────────────────────────────────────────────┘  │
   │             │                                                     │
   │             ▼                                                     │
   │   ┌────────────────────────────────────────────────────────────┐  │
   │   │   emit_routes(): write Linux kernel routing table          │  │
   │   │                                                            │  │
   │   │   • /32 for every peer's loopback, one entry per direct    │  │
   │   │     cable (e.g. metrics 10..13 in a 4-cable cluster)       │  │
   │   │     — kernel auto-fails-over                               │  │
   │   │                                                            │  │
   │   │   • /32 for transit destinations, metric 100               │  │
   │   │                                                            │  │
   │   │   • Panic /24 catch-all, metric 999                        │  │
   │   └─────────┬──────────────────────────────────────────────────┘  │
   └─────────────┼─────────────────────────────────────────────────────┘
                 ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                       Linux kernel routing                        │
   │                                                                   │
   │   DRBD, libvirt, NFS, SSH, dashboard, bedrock-rust …              │
   │   all of them just say "talk to 100.104.109.2" and the kernel     │
   │   picks the right cable, fails over for free, recovers in         │
   │   sub-second time. None of them know any of this exists.          │
   └───────────────────────────────────────────────────────────────────┘
```

That's the whole picture. The cluster log decides *who is a
member*; the mesh layer decides *how to reach them*; the kernel
decides *which cable to use right now*; and the application
layer doesn't know or care about any of it.

---

## 12. Where to dig deeper

| You want to know | Look at |
|---|---|
| The precise wire format and field-by-field spec of each protocol | `docs/06-mesh-network.md` |
| The function-by-function implementation reference | `installer/lib/netd.md` |
| Open issues, edge cases the design is aware of | `docs/mesh-network-v1-uncertainties.md` |
| Past surprises and what was learned from them | `docs/lessons-log.md` |
| How DRBD's multi-path config is built from these routes | `installer/lib/tier_storage.py::regen_drbd_configs_from_snapshot` |
| The actual Python that runs this daemon | `installer/lib/netd.py` |
| The cluster identity address derivation | `installer/lib/cluster_addr.py` |
