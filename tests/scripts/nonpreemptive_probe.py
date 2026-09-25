#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Destructive installed-snap regression probe; run only in an empty disposable VM."""

import argparse
import datetime
import errno
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path

POLICY = "non-preemptive"
OWNERS = ("nonpreemptive-probe-a", "nonpreemptive-probe-b")


def cpu_set(value):
    """Expand Linux CPU ranges without requiring installed EPA Python modules."""
    result = set()
    for part in value.strip().split(","):
        if part:
            ends = [int(item) for item in part.split("-")]
            result.update(range(ends[0], ends[-1] + 1))
    return result


def utc_now():
    """Return an evidence timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Probe:
    """Collect socket transcripts, process affinity and installed-snap evidence."""

    def __init__(self, args):
        """Initialize a probe without modifying the host."""
        self.args = args
        self.pool = getattr(args, "pool", "isolated")
        self.workers = []
        self.report = {"status": "RUNNING", "started_at": utc_now(), "events": [], "checks": []}

    def command(self, argv, check=True):
        """Run an operator command and record its result."""
        result = subprocess.run(argv, text=True, capture_output=True, timeout=90)
        self.report["events"].append(
            {
                "time": utc_now(),
                "command": argv,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if check:
            result.check_returncode()
        return result.stdout

    def request(self, action, owner=OWNERS[0], **fields):
        """Send one JSON request, recording even daemon error responses."""
        payload = {
            "version": "1.0",
            "action": action,
            "service_name": owner,
            "pool": self.pool,
            **fields,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(self.args.socket))
            connection.sendall(json.dumps(payload).encode())
            chunks = []
            while chunk := connection.recv(65536):
                chunks.append(chunk)
        response = json.loads(b"".join(chunks))
        self.report["events"].append({"time": utc_now(), "request": payload, "response": response})
        if "error" not in response:
            assert response.get("pool") == self.pool, response
        return response

    def allocations(self):
        """Observe ownership only after verifying capability support."""
        result = self.request("list_allocations")
        assert "error" not in result, result
        assert "non-preemptive-allocations" in result.get("supported_cpu_features", []), result
        assert "cpu-pools" in result.get("supported_cpu_features", []), result
        assert result.get("pool") == self.pool, result
        return {entry["service_name"]: entry for entry in result["allocations"]}

    def wait_for_allocations(self, timeout=15):
        """Bound startup/restart socket readiness waits without masking protocol failures."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.allocations()
            except (ConnectionError, FileNotFoundError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def release(self, owner):
        """Release only after the workload has been stopped."""
        result = self.request("allocate_cores", owner, num_of_cores=-1)
        assert "error" not in result and result.get("preemption_policy", "missing") is None, result

    def pinned_worker(self, cpus):
        """Start a sleeping real process and wait until taskset has applied affinity."""
        worker = subprocess.Popen(
            ["taskset", "-c", ",".join(map(str, sorted(cpus))), "sleep", "600"]
        )
        self.workers.append(worker)
        for _ in range(100):
            assert worker.poll() is None, "worker exited unexpectedly"
            if set(os.sched_getaffinity(worker.pid)) == cpus:
                return worker
            time.sleep(0.01)
        raise AssertionError("affinity not established")

    def stop_workers(self):
        """Stop every workload before any claim is released."""
        for worker in self.workers:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)
        self.workers.clear()

    def conflict(self, before, worker, pool):
        """A legacy NUMA request must fail with ownership and affinity unchanged."""
        result = self.request(
            "allocate_numa_cores", OWNERS[1], numa_node=self.args.numa_node, num_of_cores=1
        )
        assert "error" in result, result
        assert self.allocations() == before
        affinity = set(os.sched_getaffinity(worker.pid))
        assert affinity == pool
        self.report["events"].append({"pid": worker.pid, "affinity": sorted(affinity)})

    def protected_round(self, action, fields, pool):
        """Test protection, restart, unchanged affinity, and reuse after coordinated release."""
        result = self.request(action, preemption_policy=POLICY, **fields)
        assert "error" not in result and result.get("preemption_policy") == POLICY, result
        field = "cores_allocated" if action == "allocate_numa_cores" else "allocated_cores"
        assert cpu_set(result[field]) == pool
        before = self.allocations()
        worker = self.pinned_worker(pool)
        self.conflict(before, worker, pool)
        self.command(["snap", "restart", self.args.snap_name])
        assert self.wait_for_allocations() == before
        self.conflict(before, worker, pool)
        self.report.setdefault("protected_states", []).append(
            json.loads(self.args.state.read_text())
        )
        self.stop_workers()
        self.release(OWNERS[0])
        result = self.request(
            "allocate_numa_cores", OWNERS[1], numa_node=self.args.numa_node, num_of_cores=1
        )
        assert "error" not in result, result
        self.release(OWNERS[1])
        self.report["checks"].append(f"{action}: protection, restart, affinity, release and reuse")

    def legacy_control(self, pool):
        """Show the compatibility boundary: legacy ordinary claims remain reclaimable."""
        result = self.request("allocate_cores", num_of_cores=len(pool), preemption_policy="legacy")
        assert "error" not in result and result["preemption_policy"] == "legacy", result
        result = self.request(
            "allocate_numa_cores", OWNERS[1], numa_node=self.args.numa_node, num_of_cores=1
        )
        assert "error" not in result, result
        stolen = cpu_set(result["cores_allocated"])
        after = self.allocations()
        assert cpu_set(after[OWNERS[0]]["allocated_cores"]) == pool - stolen
        for owner in OWNERS:
            self.release(owner)
        self.report["checks"].append("legacy NUMA reclamation control")

    def immutable_failure(self):
        """A failed replacement must return an error and retain the existing protected claim."""
        result = self.request("allocate_cores", num_of_cores=1, preemption_policy=POLICY)
        assert "error" not in result and result["preemption_policy"] == POLICY, result
        before = self.allocations()
        before_bytes = self.args.state.read_bytes()
        self.command(["chattr", "+i", str(self.args.state)])
        try:
            for owner, count in ((OWNERS[0], 2), (OWNERS[1], 1)):
                result = self.request(
                    "allocate_cores", owner, num_of_cores=count, preemption_policy=POLICY
                )
                error = result.get("error", "")
                assert f"[Errno {errno.EPERM}]" in error, result
                assert any(
                    str(path) in error for path in (self.args.state, self.args.state.resolve())
                ), result
                assert self.args.state.read_bytes() == before_bytes
                assert self.allocations() == before
            self.report["immutable_state"] = json.loads(before_bytes)
        finally:
            self.command(["chattr", "-i", str(self.args.state)])
        self.release(OWNERS[0])
        self.report["checks"].append(
            "immutable state rejects new allocation and protected replacement"
        )

    def collect_metadata(self):
        """Record source identification, installed snap metadata and kernel setup."""
        self.report["candidate_source_sha"] = self.args.source_sha
        self.report["kernel_command_line"] = Path("/proc/cmdline").read_text().strip()
        self.command(["snap", "list", self.args.snap_name])
        self.command(["snap", "info", self.args.snap_name], check=False)
        snap_root = Path("/snap") / self.args.snap_name / "current"
        self.report["installed_source_sha256"] = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in snap_root.glob("lib/python*/site-packages/epa_orchestrator/*.py")
        }

    def run(self):
        """Run all checks, always retaining evidence and cleaning only test-owned claims."""
        safe_to_clean = False
        try:
            self.collect_metadata()
            assert (
                not self.wait_for_allocations()
            ), "requires an empty allocator in a disposable VM"
            safe_to_clean = True
            pool_info = self.request("list_allocations")["cpu_pool"]
            pool = cpu_set(pool_info["eligible_cpus"])
            node = cpu_set(
                Path(f"/sys/devices/system/node/node{self.args.numa_node}/cpulist").read_text()
            )
            assert len(pool) >= 2 and pool & node, (pool, node)
            self.report["cpu_pool"] = pool_info
            self.report["isolated_cpus"] = sorted(
                cpu_set(Path("/sys/devices/system/cpu/isolated").read_text())
            )
            self.protected_round("allocate_cores", {"num_of_cores": len(pool)}, pool)
            self.protected_round("allocate_cores_percent", {"percent": 100}, pool)
            self.protected_round(
                "allocate_numa_cores",
                {"num_of_cores": len(pool & node), "numa_node": self.args.numa_node},
                pool & node,
            )
            self.legacy_control(pool)
            self.immutable_failure()
            self.report["status"] = "PASS"
        except BaseException as error:
            self.report.update(status="FAIL", error=repr(error))
            raise
        finally:
            self.cleanup(safe_to_clean)
        assert self.report["status"] == "PASS", self.report

    def cleanup(self, safe_to_clean):
        """Retain failure evidence and stop workloads before release, including on exceptions."""
        self.stop_workers()
        if safe_to_clean:
            for owner in OWNERS:
                try:
                    self.release(owner)
                except Exception as error:
                    self.report.setdefault("cleanup_errors", []).append(repr(error))
        try:
            self.report["final_state"] = json.loads(self.args.state.read_text())
            self.command(
                [
                    "journalctl",
                    "--no-pager",
                    "-u",
                    f"snap.{self.args.snap_name}.daemon.service",
                    "--since",
                    self.report["started_at"],
                ],
                check=False,
            )
        except Exception as error:
            self.report["evidence_error"] = repr(error)
        if self.report.get("cleanup_errors"):
            self.report["status"] = "FAIL"
        self.report["finished_at"] = utc_now()
        self.args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.args.output_dir / "result.json").write_text(json.dumps(self.report, indent=2) + "\n")
        print(json.dumps({"status": self.report["status"], "checks": self.report["checks"]}))


def main():
    """Parse explicit destructive-test consent and configurable evidence locations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-vm", action="store_true", required=True)
    parser.add_argument(
        "--socket", type=Path, default=Path("/var/snap/epa-orchestrator/current/data/epa.sock")
    )
    parser.add_argument(
        "--state", type=Path, default=Path("/var/snap/epa-orchestrator/current/data/state.json")
    )
    parser.add_argument("--snap-name", default="epa-orchestrator")
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--pool", choices=("isolated", "general"), default="isolated")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("requires root in a disposable VM (restart and immutable-state tests)")
    Probe(args).run()


if __name__ == "__main__":
    main()
