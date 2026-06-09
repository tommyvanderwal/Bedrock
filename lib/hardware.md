# installer/lib/hardware.py

Local hardware inventory for one node — CPU, virtualization support, vCPU count, RAM, physical NICs (with their IPv4 addresses), and the root disk. A read-only probe (reads `/proc`, runs `ip`/`df`); it changes nothing on the box. It is a leaf utility — no rqlite, no services, no network calls. Callers use it during install/onboarding to learn what they're running on and to pick a primary NIC. Runs standalone too: `python3 hardware.py` prints the inventory as indented JSON.

## Functions / Classes

### `detect() -> dict`
Probe this node and return a flat hardware inventory.
- **In:** none.
- **Out:** dict with `hostname` (from `socket.gethostname()`), `cpu_model` (first `model name` in `/proc/cpuinfo`, `""` if unreadable), `vcpus` (`os.cpu_count()` or `1`), `ram_mb` (`MemTotal` kB // 1024, `0` if unreadable), `has_virt` (bool — any `flags` line has ` svm ` or ` vmx `), `root_disk_gb` (`/` size in GiB, `0` on parse failure), `root_device` (block device backing `/`), and `nics`. Each NIC entry is `{name, state, mac, ip, ips}` where `ips` is every IPv4 on the NIC and `ip` is the chosen primary (first non-`169.254.*`, else first, else `""`). Side effects: reads `/proc/cpuinfo` and `/proc/meminfo`; runs `ip -o -br link`, one `ip -o -br addr show <nic>` per physical NIC, and two `df` calls via `subprocess.run(..., shell=True)`. No files written.

### `primary_nic(hw: dict) -> str`
Pick the main NIC name from a `detect()` result.
- **In:** `hw` — a dict from `detect()` (only `hw["nics"]` is read).
- **Out:** NIC name string; `""` if none qualifies. No side effects.

### `run(cmd) -> str`
Helper (effectively module-internal): run a shell command, return stripped stdout.
- **In:** `cmd` — shell string.
- **Out:** stdout, `.strip()`ed. Stderr and exit code are discarded. Side effect: `subprocess.run(..., shell=True)`.

## How it works

`detect()` builds the dict with safe defaults first, then fills each field independently and guarded, so a missing file or unparseable line degrades to a default rather than aborting the rest:

```
detect()
  ├─ hostname  ← socket.gethostname()
  ├─ vcpus     ← os.cpu_count() or 1
  ├─ /proc/cpuinfo  (FileNotFoundError → skip)
  │     • first "model name" line          → cpu_model
  │     • any "flags" with " svm "/" vmx "  → has_virt = True
  ├─ /proc/meminfo  (FileNotFoundError → skip)
  │     • first "MemTotal" kB // 1024       → ram_mb
  ├─ ip -o -br link            → nics[]
  ├─ df -BG --output=size /    → root_disk_gb (strip "G", int; ValueError → 0)
  └─ df --output=source /      → root_device  (raw, unparsed)
```

CPU flags are scanned across the whole file rather than just the first core, but `cpu_model` locks to the first `model name` seen.

**NIC filtering** keeps only physical interfaces: a line is skipped when its name is `lo` or starts with `virbr`, `veth`, `docker`, `br-`, `tap`, or `vnet`, and when its brief-link output has fewer than 3 fields. For each survivor it runs `ip -o -br addr show <name>` and collects every token that looks like IPv4 CIDR (`"/" in tok and tok.count(".") == 3`), stripping the prefix into `ips`.

```
NIC primary-ip pick:
  ips = [all IPv4 on nic]
  ip  = first ips[i] NOT 169.254.*   -> else ips[0]   -> else ""
```

`primary_nic()` is a two-pass preference over `hw["nics"]`: first NIC that is both `UP` and has an `ip`; if none, first NIC merely `UP`; otherwise `""`.

## Why

The NIC pick prefers a routable address over a `169.254.*` link-local but keeps the full `ips` list, so callers that care about a specific address (e.g. picking the cluster loopback `/32`) aren't forced to trust the heuristic.
