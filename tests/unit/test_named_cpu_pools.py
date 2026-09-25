# SPDX-FileCopyrightText: 2026 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Compatibility and ownership boundaries with simultaneous isolated/general pools."""

import json

import pytest

from epa_orchestrator.allocations_db import AllocationsDB, allocations_db
from epa_orchestrator.cpu_pool import (
    CpuPools,
    load_startup_pool,
    validate_snap_configuration,
)
from epa_orchestrator.daemon_handler import handle_daemon_request
from epa_orchestrator.schemas import CpuPoolName
from epa_orchestrator.state_store import StateCorruptionError

GRANTS = [
    ("allocate_cores", {"num_of_cores": 2}, "allocated_cores"),
    ("allocate_cores_percent", {"percent": 50}, "allocated_cores"),
    ("allocate_numa_cores", {"num_of_cores": 2, "numa_node": 0}, "cores_allocated"),
]
RELEASES = [
    ("allocate_cores", {"num_of_cores": -1}),
    ("allocate_cores_percent", {"percent": 0}),
    ("allocate_cores_percent", {"percent": -1}),
    ("allocate_numa_cores", {"num_of_cores": -1, "numa_node": 0}),
]


@pytest.fixture
def pools(mock_cpu_files_empty, monkeypatch):
    """Use two NUMA nodes, each containing CPUs from both disjoint pools."""
    mock_cpu_files_empty["isolated"].write_text("8-11")
    mock_cpu_files_empty["online"].write_text("0-15")
    mock_cpu_files_empty["present"].write_text("0-15")
    topology = {0: {0, 1, 2, 3, 8, 9}, 1: {4, 5, 6, 7, 10, 11}}
    monkeypatch.setattr("epa_orchestrator.utils.get_numa_node_cpus", lambda: topology)
    monkeypatch.setattr("epa_orchestrator.daemon_handler.get_numa_node_cpus", lambda: topology)
    monkeypatch.setattr(
        "epa_orchestrator.allocations_db.get_thread_siblings_map",
        lambda cpus: {cpu: {cpu} for cpu in cpus},
    )
    return CpuPools("2-5")


def api(pools, action="list_allocations", service="owner", **fields):
    """Send wire requests without adding a pool default on the client's behalf."""
    payload = {"version": "1.0", "action": action, "service_name": service, **fields}
    return json.loads(handle_daemon_request(json.dumps(payload).encode(), pools))


@pytest.mark.parametrize("action,fields,result_field", GRANTS)
def test_legacy_requests_and_general_requests_remain_independent(
    pools, action, fields, result_field
):
    """Adding a general owner must not alter old clients' allocations or listing totals."""
    baseline = api(pools)
    assert baseline["pool"] == "isolated"
    assert "cpu-pools" in baseline["supported_cpu_features"]
    general = api(pools, action, service="ceph", pool="general", **fields)
    assert general[result_field] == "2-3"
    assert general["pool"] == "general"
    assert general["preemption_policy"] == "legacy"
    assert api(pools) == baseline
    legacy = api(pools, action, service="openstack-hypervisor", **fields)
    assert legacy[result_field] == "8-9"
    assert legacy["pool"] == "isolated"
    assert type(legacy["total_available_cpus"]) is int
    assert legacy["total_available_cpus"] == 4
    assert legacy["remaining_available_cpus"] == 2
    if action == "allocate_cores":
        assert legacy["shared_cpus"] == "10-11"
        assert type(legacy["cores_allocated"]) is int
    elif action == "allocate_cores_percent":
        assert type(legacy["cores_allocated_count"]) is int
    else:
        assert type(legacy["cores_allocated"]) is str
    for pool, service in (("isolated", "openstack-hypervisor"), ("general", "ceph")):
        listing = api(pools, pool=pool)
        assert listing["total_allocations"] == 1
        assert listing["total_allocated_cpus"] == 2
        assert listing["total_available_cpus"] == 4
        assert listing["remaining_available_cpus"] == 2
        assert listing["allocations"][0]["service_name"] == service
        assert listing["allocations"][0]["pool"] == pool


def test_legacy_nova_default_count_and_dpdk_numa_workflow(pools):
    """Replay Nova's heuristic request and DPDK's NUMA grant/release with no selector."""
    api(pools, "allocate_cores", service="ceph", pool="general", num_of_cores=4)
    nova = api(pools, "allocate_cores", service="openstack-hypervisor", num_of_cores=0)
    assert nova["allocated_cores"] == "8-10"
    assert nova["shared_cpus"] == "11"
    dpdk = api(
        pools, "allocate_numa_cores", service="ovs-dpdk-datapath", num_of_cores=2, numa_node=0
    )
    assert dpdk["cores_allocated"] == "8-9"
    assert allocations_db.get_allocation("openstack-hypervisor") == "10"
    assert (
        api(
            pools, "allocate_numa_cores", service="ovs-dpdk-datapath", num_of_cores=-1, numa_node=0
        )["cores_allocated"]
        == ""
    )
    assert allocations_db.get_allocation("ceph") == "2-5"


@pytest.mark.parametrize("action,fields", RELEASES + [(a, f) for a, f, _ in GRANTS])
@pytest.mark.parametrize("owned_pool", ["general", "isolated"])
def test_wrong_pool_cannot_replace_or_release_claims(pools, action, fields, owned_pool):
    """Wrong-pool requests preserve persisted claims, including after a daemon restart."""
    api(pools, "allocate_cores", pool=owned_pool, num_of_cores=2)
    before = allocations_db._state_store.read_all()
    restarted = CpuPools("2-5")
    # Omission is deliberately exercised when the owner belongs to general.
    selector = {} if owned_pool == "general" else {"pool": "general"}
    result = api(restarted, action, **fields, **selector)
    assert "release that allocation in its pool" in result["error"]
    assert allocations_db._state_store.read_all() == before
    assert AllocationsDB().get_all_allocations()[0].pool == owned_pool


def test_pool_switch_requires_full_release_not_partial_numa_release(pools):
    """A partial NUMA release retains the pool until the last claim is released."""
    for node in (0, 1):
        assert "error" not in api(
            pools, "allocate_numa_cores", pool="general", num_of_cores=2, numa_node=node
        )
    assert (
        api(pools, "allocate_numa_cores", pool="general", num_of_cores=-1, numa_node=0)["pool"]
        == "general"
    )
    assert AllocationsDB().get_allocation("owner") == "4-5"
    assert "error" in api(pools, "allocate_cores", num_of_cores=1)
    assert "error" not in api(pools, "allocate_cores", pool="general", num_of_cores=-1)
    assert api(pools, "allocate_cores", num_of_cores=1)["allocated_cores"] == "8"
    assert AllocationsDB().get_all_allocations()[0].pool == "isolated"


@pytest.mark.parametrize(
    "action,fields", [("list_allocations", {})] + RELEASES + [(a, f) for a, f, _ in GRANTS]
)
@pytest.mark.parametrize("selector", ["unknown", "general", None])
def test_invalid_or_unconfigured_pool_never_falls_back(pools, action, fields, selector):
    """A typo, null selector or missing general configuration cannot target isolated CPUs."""
    api(pools, "allocate_cores", num_of_cores=2)
    before = allocations_db._state_store.read_all()
    result = api(CpuPools(), action, pool=selector, **fields)
    assert "error" in result
    assert allocations_db._state_store.read_all() == before


@pytest.mark.parametrize("pool", ["isolated", "general"])
@pytest.mark.parametrize("action,fields,result_field", GRANTS)
def test_exhausted_pool_never_borrows_from_other_pool(pools, pool, action, fields, result_field):
    """All allocation actions fail when protected claims exhaust only the selected pool."""
    api(pools, "allocate_cores", pool=pool, num_of_cores=4, preemption_policy="non-preemptive")
    before = allocations_db._state_store.read_all()
    assert "error" in api(pools, action, service="other", pool=pool, **fields)
    assert allocations_db._state_store.read_all() == before
    other_pool = "general" if pool == "isolated" else "isolated"
    assert api(pools, pool=other_pool)["remaining_available_cpus"] == 4


@pytest.mark.parametrize("protected", [False, True])
def test_general_pool_preserves_legacy_preemption_unless_opted_in(pools, protected):
    """The same general NUMA request reclaims legacy claims but rejects protected claims."""
    policy = {"preemption_policy": "non-preemptive"} if protected else {}
    api(pools, "allocate_cores", pool="general", num_of_cores=4, **policy)
    result = api(
        pools, "allocate_numa_cores", service="other", pool="general", num_of_cores=2, numa_node=0
    )
    if protected:
        assert "error" in result
        assert allocations_db.get_allocation("owner") == "2-5"
    else:
        assert result["cores_allocated"] == "2-3"
        assert result["preemption_policy"] == "legacy"
        assert allocations_db.get_allocation("owner") == "4-5"


def test_metadata_free_legacy_state_defaults_to_isolated(pools):
    """Old claims remain visible in the default view and cannot be released via general."""
    allocations_db._state_store.update_section("allocations_db", {"allocations": {"owner": "8-9"}})
    assert api(pools)["allocations"][0]["pool"] == "isolated"
    assert api(pools, pool="general")["allocations"] == []
    assert "error" in api(pools, "allocate_cores", pool="general", num_of_cores=-1)
    assert "error" not in api(pools, "allocate_cores", num_of_cores=-1)


@pytest.mark.parametrize(
    "metadata", [None, [], {"owner": "unknown"}, {"owner": None}, {"ghost": "general"}]
)
def test_invalid_saved_pool_metadata_is_not_treated_as_isolated(pools, metadata):
    """Corrupt ownership metadata must fail closed instead of silently changing pools."""
    store = allocations_db._state_store
    store.update_section(
        "allocations_db", {"allocations": {"owner": "2"}, "allocation_pools": metadata}
    )
    before = store.read_all()
    try:
        with pytest.raises(StateCorruptionError):
            AllocationsDB()
        assert store.read_all() == before
    finally:
        store.update_section("allocations_db", {})


def test_hook_validates_claims_per_pool_and_rejects_overlap(pools, snap_pool_options):
    """General reconfiguration neither excludes isolated owners nor permits overlapping IDs."""
    api(pools, "allocate_cores", num_of_cores=1)
    api(pools, "allocate_cores", service="ceph", pool="general", num_of_cores=2)
    snap_pool_options["cpu-pool"] = "2-5"
    validate_snap_configuration()
    for value in ("3-5", "2-9", None):
        snap_pool_options["cpu-pool"] = value
        with pytest.raises(ValueError):
            validate_snap_configuration()
    assert allocations_db.get_allocation("owner") == "8"
    assert allocations_db.get_allocation("ceph") == "2-3"


def test_changed_isolation_never_reassigns_foreign_pool_claims(
    pools, mock_cpu_files_empty, monkeypatch
):
    """Across reboot, a CPU changing isolation status still belongs to its recorded owner."""
    api(pools, "allocate_cores", pool="general", num_of_cores=2)
    mock_cpu_files_empty["isolated"].write_text("2-3")
    monkeypatch.setattr("epa_orchestrator.cpu_pool.read_snap_cpu_pool", lambda: "2-5")
    restarted = load_startup_pool()
    # Overlap disables general grants but leaves its claims inspectable and releasable.
    assert api(restarted, pool="general")["allocations"][0]["allocated_cores"] == "2-3"
    assert api(restarted)["remaining_available_cpus"] == 0
    result = api(restarted, "allocate_numa_cores", service="dpdk", numa_node=0, num_of_cores=2)
    assert "error" in result
    assert allocations_db.get_allocation("owner") == "2-3"
    assert "error" not in api(restarted, "allocate_cores", pool="general", num_of_cores=-1)
    assert (
        api(restarted, "allocate_cores", service="dpdk", num_of_cores=2)["allocated_cores"]
        == "2-3"
    )


def test_failed_general_commit_cannot_publish_pool_membership(pools, monkeypatch):
    """Ownership and pool metadata commit together, before the API reports success."""
    before = allocations_db._state_store.read_all()

    def fail(*args):
        raise OSError("write failed")

    with monkeypatch.context() as patcher:
        patcher.setattr("epa_orchestrator.state_store.os.replace", fail)
        assert (
            "write failed" in api(pools, "allocate_cores", pool="general", num_of_cores=2)["error"]
        )
    assert allocations_db._state_store.read_all() == before
    assert api(pools, "allocate_cores", num_of_cores=2)["pool"] == "isolated"


def test_internal_reclamation_cannot_misrepresent_requesters_pool(pools):
    """Internal reclamation must respect the requester's already persisted pool."""
    allocations_db.allocate_cores("general-owner", "2", pool=CpuPoolName.GENERAL)
    allocations_db.allocate_cores("isolated-owner", "8")
    before = allocations_db._state_store.read_all()
    with pytest.raises(ValueError, match="switching pools"):
        allocations_db._subtract_cpus_from_service(
            "isolated-owner", {8}, requester="general-owner"
        )
    assert allocations_db._state_store.read_all() == before


@pytest.mark.parametrize("failure", ["configuration", "isolation"])
def test_startup_discovery_failure_never_redirects_pool_requests(
    pools, mock_cpu_files_empty, monkeypatch, failure
):
    """General discovery failure leaves isolated usable; unknown isolation blocks both."""
    monkeypatch.setattr(
        "epa_orchestrator.cpu_pool.read_snap_cpu_pool",
        lambda: "invalid" if failure == "configuration" else "2-5",
    )
    if failure == "isolation":
        mock_cpu_files_empty["isolated"].unlink()
    restarted = load_startup_pool()
    assert "error" in api(restarted, "allocate_cores", pool="general", num_of_cores=1)
    assert api(restarted, pool="general")["total_available_cpus"] == 0
    isolated = api(restarted, "allocate_cores", num_of_cores=1)
    if failure == "configuration":
        assert isolated["allocated_cores"] == "8"
    else:
        assert "error" in isolated
        assert api(restarted)["total_available_cpus"] == 0


def test_concurrent_pool_requests_cannot_split_one_service(pools):
    """Pool binding and CPU ownership are decided in the same state transaction."""
    from concurrent.futures import ThreadPoolExecutor

    def allocate(pool):
        return api(pools, "allocate_cores", pool=pool, num_of_cores=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(allocate, ("general", "isolated")))
    granted = [result for result in results if "error" not in result]
    assert len(granted) == 1
    stored = AllocationsDB().get_all_allocations()
    assert len(stored) == 1
    assert stored[0].pool == granted[0]["pool"]
    assert stored[0].allocated_cores == granted[0]["allocated_cores"]
