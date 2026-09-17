# CPU pool and ownership-policy integration

Validated on 2026-09-16 in `/home/ubuntu/src/epa-alloc-integration`, branch
`epa-alloc-integration`. The changes are uncommitted.

## Inputs and merge decisions

Both source worktrees were based on
`bb2790d6877abed41e417be9cc05d4b7d07323ce` with uncommitted changes:

- `epa-nonisol-alloc`: configurable pools, online eligibility, configure hook,
  unavailable-claim introspection and pool regression tests;
- `epa-nonpreempt-alloc`: ownership policy, stable protected selection, capability
  advertisement, transactional state updates, persistence failure handling,
  uncertainty recovery and its tests/probe.

Each source was captured before merging. SHA-256 verification after integration
confirmed that neither original worktree was modified. Snapshots and the source
manifest are in `~/epa-alloc-integration-evidence/`.

The integrated paths deliberately combine semantics rather than choosing one
branch's allocation implementation:

- One eligible-pool snapshot is taken inside each allocation's state transaction.
  Percentage conversion and count selection reuse it.
- Pool/claim conflicts are checked against the transaction's fresh candidate
  ledger, not a separate singleton read which could deadlock or miss another writer.
- Count and NUMA selection preserve policy inheritance and protected own-ID stability.
- NUMA selection **and its private application helper** receive the same eligible
  set. Neither independently discovers isolated CPUs.
- Final online validation is a required callback at the count and guarded NUMA
  application boundaries, before ownership mutation. Failed checks cannot install
  claims or policy upgrades.
- Offline claims remain owned. Replacement cannot silently remove them; coordinated
  release is required. Releases remain independent of empty/conflicting pools, but
  uncertain storage still blocks mutations until explicit recovery.
- Listing uses one ownership snapshot for totals, policies, unavailable IDs,
  capability advertisement and active pool information.
- The ownership branch's persistence fix is included: successful replies require
  durable commit. The pool-only validation document is historical, not a statement
  that the persistence defect is still present in this integration.

## Repository validation

`tox -e fmt,lint,mypy,unit,integration` passed:

| Check | Result |
| --- | --- |
| Lint and formatting | Pass |
| Strict mypy | Pass |
| Unit tests | 182 passed |
| Integration tests | 28 passed |
| Independent read-only correctness review | No issues found |

Combined regressions exercise ordinary configured pools through all three APIs,
protection against conflicting legacy NUMA requests, fresh-ledger pool validation,
restart/reload, retained IDs after pool growth, offline ownership, failed online
checks, write failures and actual socket round trips in both pool modes.
Existing concurrency, uncertainty-latch, recovery and backward-compatibility tests
from the policy branch remain included.

See `tox-final.log` and `review.md` in the evidence directory. An initial run found
one test call still missing the newly required online-check argument; that test
adapter was corrected before the complete passing run.

## Real strict-snap validation

A real managed Snapcraft build was installed in a new disposable local LXD VM:

- VM: `epa-alloc-integration-test`, Ubuntu 26.04, 8 logical CPUs, 4 GiB RAM.
- Artifact: `~/epa-alloc-integration-evidence/candidate.snap`.
- Snap: `2026.1-bb2790d6+dirty`, amd64, core26, **strict**, guest revision `x1`.
- SHA-256: `7a7899f229a5e42acb3d6d0665f338b6d3f2824172c1531b2ab957cc4c2696a9`.
- Build: `snapcraft -v pack --use-lxd --output candidate.snap`, from a normal Git
  clone containing the merged working-tree snapshot; no manual repack.
- The packaged Python modules, daemon and configure hook matched the final source
  byte-for-byte. Installed-module hashes recorded by both guest probe runs also
  matched. See `production-source.sha256` and the probe JSON files.

### Ordinary pool, no isolated CPUs

With `/sys/devices/system/cpu/isolated` empty and `cpu-pool=2-5`:

- Count, percentage **and NUMA** non-preemptive allocations passed the installed
  probe: conflicting legacy NUMA requests failed; saved IDs, policy and the pinned
  sleeping process's affinity were unchanged before and after daemon restart.
- Workloads were stopped before release; released CPUs were then allocatable.
- The legacy reclamation control passed, preserving the intended compatibility
  boundary rather than silently changing all clients to non-preemptive behavior.
- Making the state file immutable caused errors for both new allocations and
  replacement of an existing protected allocation. Prior state remained unchanged.
- Full pool lifecycle tests passed: absent-default no-fallback, invalid configuration
  rollback, pending versus active settings, supersets/shrinks, restart persistence,
  CPU offlining/onlining, zero-capacity release, and the actual configure/startup race.
- A separate protected-hotplug run verified unavailable protected claims across
  restart, rejection of all replacement APIs without claim/policy loss, protection
  on CPU return, and full release while all configured CPUs were offline.
- Functional suite: **5 passed**.

Evidence: `combined-probe-result.json`, `pool-lifecycle-transcript.jsonl`,
`protected-hotplug-transcript.jsonl`, `functional-configured.log`, and
`configured-daemon.journal`.

### Default isolated mode

Only the disposable guest was rebooted with `isolcpus=2-5`, with `cpu-pool` unset.
The same installed protection/affinity/restart/release/legacy/persistence probe
passed all checks against the isolated pool. Functional suite: **2 passed**, with
3 configured-only tests intentionally skipped.

Evidence: `default-probe-result.json`, `default-environment.txt`,
`functional-isolated.log`, and `default-daemon.journal`.

The existing NUMA read interface was connected; no new CPU permissions were needed.
No EPA AppArmor denials appeared in either kernel journal. The VM was deleted after
collecting evidence, and the dedicated managed builder was cleaned. Host isolation
and other running guests were not modified.

## Scope limits

No external Ceph charm/client code was changed, and no Ceph performance or IRQ
placement evaluation was performed. Clients must still check capability and the
returned policy before applying affinity, coordinate release with workloads, and
follow the documented recovery procedure after uncertain commits. Kernel hotplug
can still occur after the final online check and successful reply.
