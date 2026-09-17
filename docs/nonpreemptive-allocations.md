# Non-preemptive CPU allocations (protocol 1.0)

`preemption_policy` is optional on `allocate_cores`, `allocate_cores_percent`, and
`allocate_numa_cores`. Values are `legacy` and `non-preemptive`; unknown values are
errors. This is ownership accounting, **not** Linux scheduler preemption control.
EPA does not move processes or apply affinity on behalf of a client.

## Safe client integration, including external Ceph charms

Older daemons silently ignore unknown request fields. **Do not send a protected
allocation before checking support**, and do not infer support from version 1.0.

1. Send `{"version":"1.0","action":"list_allocations"}`. Require
   `supported_cpu_features` to contain `non-preemptive-allocations`. This field is
   present even with zero eligible CPUs. Missing capability means unsupported:
   fail/block rather than fall back to an ordinary allocation.
2. Use a stable, distinct `service_name` for each independently managed OSD (for
   example `ceph-osd.0`). EPA retains the existing caller-supplied identity model;
   it does not authenticate entitlement to that name.
3. Request `preemption_policy: "non-preemptive"`. Require a non-error response
   with **exactly** `preemption_policy: "non-preemptive"` before applying returned
   IDs. A missing or different confirmation is unsupported, not success. Do not
   apply those IDs; explicitly release the newly created claim after ensuring no
   workload uses it. Do not blindly release an existing live claim during error
   handling: reconcile the service's previous and observed state first.
4. Retain profile-specific locality checks (performance, balanced, minimal).
   Ownership protection is independent of NUMA locality. Check count and placement
   against the profile before applying affinity.
5. After client/daemon restart, list and reconcile claimed CPU IDs and policy
   before resuming work. A failed/uncertain allocation or lost reply is **not**
   evidence that the daemon retained the old state. Observe and reconcile; do not
   assume CPUs are free, and never apply IDs from an error response.
6. Move or stop workloads **before** shrinking, replacing with different IDs, or
   releasing their claims. There is no reserve/accept/commit lifecycle and no
   workload-migration transaction. Use one coordinated writer per service identity.

This repository does not contain the Ceph charm/client implementation. These are
integration requirements for that external client, not a claim it is updated.

### Count-only protection

```json
{
  "version": "1.0",
  "action": "allocate_cores",
  "service_name": "ceph-osd.0",
  "num_of_cores": 4,
  "preemption_policy": "non-preemptive"
}
```

### NUMA-constrained protection

```json
{
  "version": "1.0",
  "action": "allocate_numa_cores",
  "service_name": "ceph-osd.0",
  "numa_node": 0,
  "num_of_cores": 4,
  "preemption_policy": "non-preemptive"
}
```

For percentage allocation, use `action: "allocate_cores_percent"`, `percent: 50`,
and the same policy. Percentages remain ceiling-rounded against the complete eligible
pool (`configured ∩ online`), not the free subset.
Count/percentage responses use `allocated_cores`; NUMA uses `cores_allocated`.
All three confirm the effective policy in `preemption_policy`.

## Policy and compatibility rules

- New services default to `legacy`. Omitting the field on an existing service
  inherits its policy across all three allocation APIs.
- A successful upgrade protects **all** the service's claims, across NUMA nodes.
  Failed upgrades change neither policy nor claims. Downgrading a protected
  service with claims is rejected; fully release it first.
- Non-preemptive requests cannot reclaim *any* other owner's CPUs, even ordinary
  legacy claims. Legacy NUMA requests can still reclaim ordinary legacy CPUs but
  not explicit NUMA CPUs or any protected service's CPUs. Count/percentage
  requests never reclaim foreign ownership.
- Protected retries retain eligible own IDs; growth keeps own IDs first and uses
  the SMT preference for additional free IDs. Shrink removes only the reduced
  portion. Unsatisfiable requests fail without changing owners.
- `is_explicit` retains its old meaning (NUMA metadata); it is not a protection
  indicator. Listing entries separately include `preemption_policy`.
- `num_of_cores: -1` releases the whole service for ordinary requests, or only
  the selected node for NUMA requests. Percentage `0`/`-1` releases the service.
  Partial release reports the remaining policy; full release reports JSON `null`
  and removes policy metadata. A later allocation is a new service allocation.
- Unavailable/out-of-pool claimed CPUs remain owned and listed, even if eligible
  capacity becomes zero. Replacement that would silently erase such claims is
  rejected. Explicit coordinated release is required; full release also works
  when no CPUs are currently eligible.
- Missing policy metadata in old state means legacy. Malformed metadata is a
  state error, never an implicit downgrade.
- **Downgrading EPA to an old binary is incompatible while protected claims
  exist.** Old binaries may discard policy metadata and reclaim CPUs. Coordinate
  workload stop/release before a revert; a state version field cannot make an
  already shipped old binary enforce protection.

## Persistence, uncertainty, and explicit recovery

Capacity validation, selection, ownership changes, and policy changes run under
one state-file lock. Success is sent only after atomic replacement and durability
confirmation. Other state sections are preserved. Known pre-replacement failures
leave prior claims intact and return an API error.

The existing `state.json.lock` inode also holds an uncertainty latch. Before
replacement its marker and directory entry are synced to storage. The marker is
cleared only after the new state is durable (or after a known pre-replacement
failure is safely unwound). An interrupted commit or post-replacement durability
failure blocks **all StateStore writes**, across instances and daemon restarts.
Reads remain available and show the observed claims. Do not delete/truncate the
lock file, replace it, or restore old state over it to bypass the latch.

Recovery is an explicit operator action, not an automatic API retry:

1. Quiesce clients and stop the daemon (`sudo snap stop epa-orchestrator.daemon`).
2. Fix the storage problem. Retain copies of `state.json`, `state.json.lock`, and
   the daemon journal for evidence. Reconcile the *observed* claims with workloads;
   the failed call may already have replaced the file. Do not assume old bytes.
3. In the installed snap's environment, sync the observed state and directory,
   then clear the latch with the supported `StateStore.recover()` operation:

   ```sh
   sudo snap run --shell epa-orchestrator.daemon
   # In the snap shell (SNAP_DATA and PYTHONPATH must identify this installation):
   python3 -c 'from epa_orchestrator.state_store import StateStore; StateStore().recover()'
   exit
   ```

   Recovery failure must be treated as still blocked. Recovery does not validate
   workload affinity or resolve malformed policy data; repair corruption only
   with coordinated ownership reconciliation. Never erase live claims as repair.
4. Start the daemon (`sudo snap start epa-orchestrator.daemon`), re-list allocations
   and verify policy before allowing clients to allocate. Restart other writer
   processes too: they may retain their own in-process uncertainty latch.

## Disposable installed-snap regression

The following probe is destructive and requires an **empty allocator in a
root-accessible disposable VM**, at least two eligible CPUs, `taskset`, `chattr`,
and a selected NUMA node intersecting that pool. It restarts the snap and briefly
makes the state file immutable. Do not run on a production host.

```sh
sudo python3 tests/scripts/nonpreemptive_probe.py \
  --disposable-vm --source-sha "$(git rev-parse HEAD)" \
  --output-dir /root/nonpreemptive-evidence
```

`--socket`, `--state`, `--snap-name`, and `--numa-node` are configurable. Evidence
includes API transcript, source identifier and installed Python hashes, snap
metadata, kernel setup, protected and final state, affinity observations, and
journal output.

Checks cover count, percentage and NUMA protection before/after restart, legacy NUMA
conflicts without ownership/affinity changes, coordinated release and reuse, a
legacy reclamation control, and immutable-state errors for both new allocation
and replacement of an existing protected claim. Unit tests additionally inject
post-replacement fsync failures, recovery failures, and concurrent DB requests.
The probe discovers the active eligible pool through `list_allocations.cpu_pool`,
so it also works without isolated CPUs after configuring `cpu-pool` and restarting
the daemon.
