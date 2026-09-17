# EPA Orchestrator Snap

This repository contains the source for the EPA Orchestrator snap.

**EPA Orchestrator** is designed to provide secure, policy-driven resource orchestration for snaps and workloads on Linux systems. Its vision is to enable fine-grained, dynamic allocation and management of system resources—starting with CPU pinning and memory management, with plans to expand to other resource types and orchestration policies. The orchestrator exposes a secure Unix socket API for resource allocation and introspection, making it easy for other snaps (such as openstack-hypervisor) and workloads to request and manage dedicated or shared resources in a controlled manner.

## Features

- **CPU Pinning and Allocation**: Allocate isolated and shared CPU sets to snaps and workloads, supporting both dedicated and shared CPU usage models with basic system-size heuristics.
- **Memory Management and Hugepage Tracking**: Introspect NUMA hugepages and track hugepage allocations across NUMA nodes with per-service allocation tracking.
- **NUMA-Aware Core Allocation**: Request a specific number of cores from a particular NUMA node with override/append semantics and exact-count guarantees.
- **Non-preemptive CPU Ownership**: Opt in with `preemption_policy: "non-preemptive"` to retain claims until coordinated owner release/replacement, independently of NUMA placement. [Client and recovery contract](docs/nonpreemptive-allocations.md).
- **Resource Introspection**: Query current allocations and available resources via a secure API.
- **Secure Unix Socket API**: All orchestration actions are performed via a secure, local Unix socket with JSON-based requests and responses.
- **Basic Allocation Heuristics**: Automatic allocation based on system size (small vs large systems) when no specific core count is requested.

### Non-preemptive Client Requests

Before allocating, require `non-preemptive-allocations` in the `list_allocations`
response's `supported_cpu_features`. Send `preemption_policy: "non-preemptive"`
and verify that exact policy in the successful response before applying affinity.
Older daemons ignore unknown request fields, so the request field alone is unsafe.
Existing clients retain legacy behavior; omitted policy inherits existing protection.
See the [integration, release, downgrade, and recovery guide](docs/nonpreemptive-allocations.md).
Protection works with either the default isolated pool or an explicitly configured
ordinary CPU pool. Selection, pool-conflict validation, ownership changes and the
final online check share one state transaction; a persistence failure returns an
error, never a successful grant. See [combined validation](docs/allocation-integration-validation.md).

### CPU Allocation by Percentage

Use the `allocate_cores_percent` action to request a percentage (0–100) of the eligible CPU pool. The orchestrator computes the count from the complete eligible pool, not the currently free CPUs (ceiling-rounded so small percentages yield at least 1 logical CPU). A request fails if other owners leave insufficient capacity. Use `percent: 0` or `percent: -1` to deallocate.

### CPU Allocation Policy: Small vs. Large Systems

When a client requests core allocation with `num_of_cores: 0`, EPA Orchestrator applies the existing heuristic to eligible CPUs available to that service (including its own previous allocation, excluding other owners):

- **Small systems (≤100 CPUs):**
  - By default, 80% of the available CPUs are allocated to the requesting snap or workload.
  - The remaining 20% are left unallocated (shared).
- **Large systems (>100 CPUs):**
  - By default, 16 CPUs are always reserved (left unallocated/shared).
  - All other CPUs are allocated to the requesting snap or workload.

This heuristic leaves CPUs unallocated within EPA's accounting; it does not reserve CPUs against other host workloads.

### NUMA-Aware Core Allocation Policy

The NUMA-aware allocation action allows services to request a specific number of cores from a particular NUMA node:

- **NUMA Locality**: Cores are allocated from the specified NUMA node to ensure optimal memory access patterns.
- **Legacy Reallocation**: Legacy NUMA requests may reclaim non-explicit legacy allocations from other services. Non-preemptive requests never reclaim foreign claims, and protected claims cannot be reclaimed by any requester.
- **Atomic Exact-Count**: If fewer than the requested number of cores are available in the NUMA node, the request fails with an error; no partial allocation occurs.
- **Protection**: Explicit NUMA allocations remain protected from other services. Non-preemptive policy additionally protects ordinary/percentage claims.
- **Per-NUMA override/append semantics**: If the same service requests the same NUMA node again, it overrides previous cores from that node. If it requests a different NUMA node, the new cores are appended so the service may hold allocations across multiple NUMA nodes.
- **Per-NUMA deallocation**: Sending `num_of_cores = -1` for a node deallocates any existing cores for that service in that node. `num_of_cores = 0` is invalid for NUMA.
 - **SMT/Hyperthreading-aware allocation**: Within the requested NUMA node, allocation prefers full physical cores (both hyperthreads) when available, and then fills any remainder with single logical CPUs from other cores.

### Planned Features

- **Hugepage Introspection and Tracking**: ✅ **Implemented** - NUMA hugepage introspection and tracking via `get_memory_info` and `allocate_hugepages` actions.

## Getting Started

To get started with the EPA Orchestrator, install the snap using snapd:

```bash
sudo snap install epa-orchestrator --dangerous --devmode
```

The snap runs a daemon that listens on a Unix domain socket and provides a JSON API for CPU allocation and introspection.

## Configuration Reference

The snap can be integrated with other snaps (e.g., openstack-hypervisor) via the slot/plug mechanism for EPA information sharing.

### CPU allocation pool (`cpu-pool`)

By default (unset or `isolated`), EPA uses the CPUs listed in
`/sys/devices/system/cpu/isolated`. An empty isolated list means zero capacity;
there is **no fallback to all host CPUs**. To allocate ordinary online CPUs instead:

```sh
sudo snap set epa-orchestrator cpu-pool='2-15,18-31'
sudo snap restart epa-orchestrator.daemon
```

Choose CPU IDs present on your machine. Lists accept non-negative IDs and inclusive
ranges; ordering, duplicates and surrounding whitespace are normalized. Empty
components, descending ranges, negative IDs, nonexistent CPUs and kernel stride/group
expressions are rejected. An explicit empty string is invalid. Restore the default with:

```sh
sudo snap unset epa-orchestrator cpu-pool
sudo snap restart epa-orchestrator.daemon
```

The configured pool is fixed at daemon startup. Online status is refreshed per request:
`eligible = configured ∩ online`, `free = eligible − claimed`. Offline configured CPUs
remain in the configuration but cannot be granted. A final online check rejects a
selected CPU that became unavailable with an error asking the client to retry, without
changing allocations. CPUs can still go offline after a successful response.

**Pool eligibility is accounting, not kernel isolation.** EPA does not activate scheduler
isolation or IRQ isolation. CPU affinity, IRQ placement, kernel housekeeping and exclusion
of unrelated host workloads remain deployment responsibilities. Counts are logical CPUs;
NUMA placement retains sibling-aware selection but never adds siblings outside the pool.

Before shrinking a pool (including returning to `isolated`), coordinate workload release
or migration. The configure hook rejects settings that exclude any saved owner, even if
that CPU is offline. Supersets are allowed. A claim made between hook validation and restart
can still conflict: the restarted daemon then serves listing/releases but blocks new CPU
allocations until the conflict is resolved. No claims are erased. Full-service release
(`allocate_cores` with `num_of_cores: -1`, or percentage `0`/`-1`) works with an empty pool.
NUMA release ignores pool membership but requires node topology; if topology disappears,
use full-service release. Replacing a service with unavailable existing claims is
rejected rather than silently discarding those claims: explicitly coordinate their
release first. An uncertain state commit blocks all mutations, including releases,
until the documented storage recovery procedure completes.

Capacity fields retain their names:

- `total_available_cpus`: the complete eligible pool size, including owned CPUs.
- `remaining_available_cpus`: unclaimed eligible CPUs; never negative.
- `total_allocated_cpus`: all recorded claims, including currently unavailable CPUs, so
  it can exceed current capacity. Offlining does not release ownership; a returning CPU
  remains owned.
- `shared_cpus`: unallocated eligible CPUs, **not** all remaining host CPUs.
- `list_allocations.cpu_pool`: active source (`isolated` or `configured`),
  `configured_cpus`, `eligible_cpus`, and `unavailable_allocated_cpus`, using normalized
  CPU range strings. This reports the daemon's active choice, not a pending snap setting.
  Saved allocations remain visible even when no CPUs are eligible.

### API Usage

The daemon listens on:

```
$SNAP_DATA/data/epa.sock
```

Clients can connect to this socket and send JSON requests. The supported actions are:

#### 1. Allocate Cores (`allocate_cores`)

Request CPU allocation for a specific service:

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "action": "allocate_cores",
  "num_of_cores": 2
}
```

- `num_of_cores`: Number of logical CPUs to allocate. `0` uses the small/large-system heuristic; `-1` releases all of this service's CPU claims.
- `numa_node` is not allowed for this action and will be rejected.

#### Response Example (Success)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "num_of_cores": 2,
  "cores_allocated": 2,
  "allocated_cores": "0-1",
  "shared_cpus": "2-19",
  "total_available_cpus": 20,
  "remaining_available_cpus": 18
}
```

#### Response Example (Error)

```json
{
  "version": "1.0",
  "error": "Insufficient CPUs available. Requested: 100, Available: 20"
}
```

#### 2. Allocate Cores Percent (`allocate_cores_percent`)

Request CPU allocation as a percentage of the eligible pool:

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "action": "allocate_cores_percent",
  "percent": 50
}
```

- `percent`: Percentage of eligible CPUs to allocate (1–100). Use `0` or `-1` to deallocate the service's cores.

#### Response Example (Success)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "cores_allocated_count": 10,
  "allocated_cores": "0-9",
  "total_available_cpus": 20,
  "remaining_available_cpus": 10
}
```

#### Response Example (Error)

```json
{
  "version": "1.0",
  "error": "Insufficient CPUs available. Requested: 3, Available: 0"
}
```

#### 3. Allocate NUMA Cores (`allocate_numa_cores`)

Request a specific number of cores from a particular NUMA node:

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "action": "allocate_numa_cores",
  "numa_node": 1,
  "num_of_cores": 5
}
```

- `numa_node`: NUMA node ID to allocate cores from (0-based)
- `num_of_cores`: Number of cores to allocate from the specified NUMA node
  - `> 0` allocates exactly that many cores
  - `-1` deallocates any existing cores for that service in that node
  - `0` is invalid

#### Response Example (Success)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "numa_node": 1,
  "num_of_cores": 5,
  "cores_allocated": "4-8",
  "total_available_cpus": 20,
  "remaining_available_cpus": 15
}
```

#### Response Example (Insufficient Cores Error)

```json
{
  "version": "1.0",
  "error": "NUMA node 1 only has 3 eligible CPUs, but 5 were requested"
}
```

#### Response Example (Per-NUMA Deallocation)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "numa_node": 1,
  "num_of_cores": -1,
  "cores_allocated": "",
  "total_available_cpus": 20,
  "remaining_available_cpus": 20
}
```

#### 4. List Allocations (`list_allocations`)

Get all current service allocations:

```json
{
  "version": "1.0",
  "service_name": "any-service",
  "action": "list_allocations"
}
```

#### Response Example (Success)

```json
{
  "version": "1.0",
  "total_allocations": 2,
  "total_allocated_cpus": 4,
  "total_available_cpus": 20,
  "remaining_available_cpus": 16,
  "cpu_pool": {
    "source": "isolated",
    "configured_cpus": "0-19",
    "eligible_cpus": "0-19",
    "unavailable_allocated_cpus": ""
  },
  "allocations": [
    {
      "service_name": "my-service",
      "allocated_cores": "0-1",
      "cores_count": 2,
      "is_explicit": false
    },
    {
      "service_name": "another-service",
      "allocated_cores": "2-3",
      "cores_count": 2,
      "is_explicit": true
    }
  ]
}
```

#### Response Example (Empty Default Pool, No Saved Owners)

```json
{
  "version": "1.0",
  "total_allocations": 0,
  "total_allocated_cpus": 0,
  "total_available_cpus": 0,
  "remaining_available_cpus": 0,
  "cpu_pool": {
    "source": "isolated",
    "configured_cpus": "",
    "eligible_cpus": "",
    "unavailable_allocated_cpus": ""
  },
  "allocations": []
}
```

#### 5. Get Memory Info (`get_memory_info`)

Get NUMA hugepage information (with EPA-tracked overlay), keyed by node name with capacity lists:

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "action": "get_memory_info"
}
```

#### Response Example (Success)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "numa_hugepages": {
    "node0": {
      "capacity": [
        { "total": 100, "free": 60, "size": 2048 },
        { "total": 4, "free": 1, "size": 1048576 }
      ],
      "allocations": {
        "openstack-hypervisor": { "2048": 20, "1048576": 2 },
        "database-service": { "2048": 15, "1048576": 1 },
        "my-service": { "2048": 5 }
      }
    }
  }
}
```

#### Response Example (No Hugepages)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "numa_hugepages": {}
}
```

#### 6. Allocate Hugepages (`allocate_hugepages`)

Record hugepage allocation request (tracking-only) for a specific NUMA node and size:

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "action": "allocate_hugepages",
  "hugepages_requested": 2,
  "node_id": 0,
  "size_kb": 2048
}
```

- `hugepages_requested`: Number of hugepages to record (>0), use `-1` to deallocate, `0` is invalid
- `node_id`: NUMA node ID for per-node tracking
- `size_kb`: Hugepage size in KB (e.g., 2048 for 2MB, 1048576 for 1GB)

#### Response Example (Success)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "hugepages_requested": 2,
  "allocation_successful": true,
  "message": "Successfully set allocation request to 2 hugepages",
  "node_id": 0,
  "size_kb": 2048
}
```

#### Response Example (Deallocate)

```json
{
  "version": "1.0",
  "service_name": "my-service",
  "hugepages_requested": -1,
  "allocation_successful": true,
  "message": "Removed recorded hugepage allocation",
  "node_id": 0,
  "size_kb": 2048
}
```

#### Response Examples (Errors)

```json
{ "version": "1.0", "error": "NUMA node 3 not found" }
```
```json
{ "version": "1.0", "error": "Hugepage size 1048576 KB not found on node 0" }
```
```json
{ "version": "1.0", "error": "NUMA node 0 size 2048 KB only has 5 free hugepages, requested 10" }
```

## Build

To build and test the snap, see CONTRIBUTING.md for full details. Typical steps:

```bash
# Build the snap
snapcraft -v pack --use-lxd

# Install the snap
sudo snap install --dangerous epa-orchestrator_*.snap
```

## Testing

The project includes unit, integration, and functional tests.

```bash
tox -e unit
tox -e integration
tox -e functional
tox -e lint
tox -e fmt
tox -e mypy
```

**Note:** Functional tests require sudo privileges for snap installation and management.
The configured-pool success tests opt in with `EPA_TEST_CPU_POOL` and must run in a
disposable guest with an empty isolated list, a matching active pool on NUMA node 0,
and free capacity:

```sh
EPA_TEST_CPU_POOL=2-5 SOCKET_PATH=/var/snap/epa-orchestrator/current/data/epa.sock \
  python3 -m pytest tests/functional -v --import-mode=importlib --confcutdir=tests/functional
```

These tests require successful grants and verify returned CPU IDs; they do not
accept no-CPU errors. See [CPU pool validation](docs/cpu-pool-validation.md) for
installed-snap evidence and remaining release dependencies.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for details on how to contribute to this project.

## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.
