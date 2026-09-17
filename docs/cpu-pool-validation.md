# Configurable CPU pool validation

Historical standalone validation on 2026-09-16 in worktree `epa-nonisol-alloc`.
The integration worktree also includes non-preemptive ownership and its persistence
fix. See [combined validation](allocation-integration-validation.md) for current
integration results; the standalone dependencies below describe the original snapshot.

## Source and artifact

- Baseline SHA: `bb2790d6877abed41e417be9cc05d4b7d07323ce` plus the uncommitted
  configurable-pool changes. No non-preemptive policy or persistence fix included.
- Real managed build: `snapcraft -v pack --use-lxd` (Snapcraft 9.0.1), from a normal
  Git clone with the worktree changes copied in. This avoids a linked-worktree
  `.git` pointer referring to a host-only path in the builder.
- Artifact: `~/epa-pool-evidence/candidate.snap`.
- Snap metadata: `2026.1-bb2790d6+dirty`, amd64, core26, strict; guest revision `x1`.
- SHA-256: `4a7c45abf41b57302980e13d04dea42524998f89e83ace44479defc9e3d63c38`.
- Every packaged Python module, `bin/daemon`, and executable `meta/hooks/configure`
  was compared byte-for-byte with the final worktree. Digests are in
  `production-source.sha256`. The snapshot used for building is in `build-source/`.

Evidence paths below are relative to `~/epa-pool-evidence/` on Seadra.

## Repository checks

`tox -e fmt,lint,mypy,unit,integration` passed:

- lint and strict mypy: pass;
- unit: **134 passed**;
- integration: **12 passed**, including actual Unix-socket requests with a
  configured pool and an empty isolated list.

New tests cover parser bounds/holes, strict read failures, frozen configuration,
percentage denominator/snapshot reuse, offline ownership, empty capacity,
configure/startup conflicts, sibling exclusion, and grant-time offlining/read
failure without changing the requester's or another owner's saved claims.

An independent read-only correctness review found no issues. Its two suggested
failure-path tests were added and passed. See `review.md` and `tox-final.log`.

## Installed snap

Used a new disposable LXD VM, `epa-pool-test`: Ubuntu 26.04, 8 logical CPUs,
4 GiB memory, kernel `7.0.0-30-generic`. No host CPU settings were changed.
Installed the actual artifact with `snap install --dangerous` (not devmode), and
connected the existing `sys-devices-system-node` interface.

With an empty isolated list and `cpu-pool=2-5`:

- count, percentage and NUMA requests succeeded and returned `2-3`;
- saved claims and active configuration survived daemon restarts;
- pending settings did not affect the running daemon;
- percentages used complete eligible capacity despite other owners;
- empty/malformed/oversized/nonexistent CPU input was rejected, leaving the
  previous snap option and ledger unchanged;
- shrinking/unsetting a pool with owners failed; supersets succeeded;
- offline CPUs lost eligibility without losing ownership, including across restart;
- an empty eligible pool still listed owners and allowed full release;
- bringing CPUs online again retained prior ownership;
- a real hook/startup race (configure, allocate on old pool, restart) kept the socket
  available, rejected all three new allocation APIs, and recovered after release;
- single numeric IDs and normalized whitespace/duplicates were accepted.

See `installed-check.py`, `installed-transcript.jsonl`, `state-configured.json`,
`daemon-configured.journal`, and `functional-configured.log` (**5 passed**).
The first rapid-restart test run hit systemd's start-rate limiter near the end;
the harness now resets that limiter before intentional restarts. The complete
rerun passed; the initial transcript is retained, not hidden.

Both exact hook and daemon AppArmor profiles permitted reads of `present`,
`online`, `isolated`, and `node0/cpulist` (`hook-sysfs.txt`, `daemon-sysfs.txt`).
Actual hook execution and confined daemon allocations also passed. No extra CPU
read plug or CPU-control write permission was required; no EPA confinement denials
were observed in the kernel journals.

For a second guest configuration, only this VM was rebooted with `isolcpus=2-5`.
With `cpu-pool` unset, all three APIs again succeeded inside the isolated pool;
explicit `cpu-pool=isolated` was also tested. See `default-isolated-check.py`,
`default-isolated-transcript.jsonl`, and `daemon-isolated.journal`. Existing
functional tests passed (**2 passed**, 3 configured-only tests intentionally skipped).

The guest was deleted after journals, state, metadata and transcripts were collected.
See `instances-after-cleanup.csv` and `guest-config.yaml`.

## Remaining dependencies

The pre-existing swallowed state-write failure is not repaired here and remains
an explicit release prerequisite for dependable ownership accounting. NUMA's
existing preemption policy is unchanged. Combining with the non-preemptive plan
requires deliberate integration of its transaction/policy changes and this
provider's eligible snapshot plus pre-mutation online check, followed by combined
tests. Neither Ceph performance nor IRQ/affinity tuning was evaluated.
