# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Combined configured-pool, protected-ownership and transaction regressions."""

import json

import pytest

from epa_orchestrator.allocations_db import AllocationsDB, allocations_db
from epa_orchestrator.cpu_pool import CpuPools
from epa_orchestrator.daemon_handler import handle_daemon_request
from epa_orchestrator.schemas import CpuPoolName, PreemptionPolicy

POLICY = "non-preemptive"
REQUESTS = [
    ("allocate_cores", {"num_of_cores": 4}, "allocated_cores"),
    ("allocate_cores_percent", {"percent": 100}, "allocated_cores"),
    ("allocate_numa_cores", {"num_of_cores": 4, "numa_node": 0}, "cores_allocated"),
]


@pytest.fixture
def configured(mock_cpu_files_empty, monkeypatch):
    """Use an ordinary pool narrower than node topology, without isolated discovery."""

    def topology():
        return {0: set(range(8))}

    monkeypatch.setattr("epa_orchestrator.utils.get_numa_node_cpus", topology)
    monkeypatch.setattr("epa_orchestrator.daemon_handler.get_numa_node_cpus", topology)
    monkeypatch.setattr(
        "epa_orchestrator.allocations_db.get_thread_siblings_map",
        lambda cpus: {cpu: {cpu} for cpu in cpus},
    )
    provider = CpuPools("2-5")

    return provider


def api(provider, action="list_allocations", service="owner", **params):
    """Exercise the production dispatcher with one injected active pool."""
    request = {"action": action, "service_name": service, "pool": "general", **params}
    return json.loads(handle_daemon_request(json.dumps(request).encode(), provider))


@pytest.mark.parametrize("action,params,field", REQUESTS)
def test_protected_ordinary_pool_conflict_restart_and_release(configured, action, params, field):
    """Every API protects ordinary pool claims across a new DB/provider and legacy conflict."""
    listing = api(configured)
    assert listing["cpu_pool"]["source"] == "configured"
    assert listing["supported_cpu_features"] == ["non-preemptive-allocations", "cpu-pools"]
    granted = api(configured, action, preemption_policy=POLICY, **params)
    assert granted[field] == "2-5"
    assert granted["preemption_policy"] == POLICY
    reloaded = AllocationsDB()
    assert reloaded.get_allocation("owner") == "2-5"
    assert reloaded.get_preemption_policy("owner") == POLICY
    restarted = CpuPools("2-5")
    before = allocations_db._state_store.read_all()
    rejected = api(restarted, "allocate_numa_cores", "other", numa_node=0, num_of_cores=1)
    assert "error" in rejected
    assert allocations_db._state_store.read_all() == before
    assert api(restarted, "allocate_cores", num_of_cores=-1)["preemption_policy"] is None
    assert (
        api(restarted, "allocate_numa_cores", "other", numa_node=0, num_of_cores=4)[
            "cores_allocated"
        ]
        == "2-5"
    )


def test_pool_growth_keeps_protected_ids_and_policy(configured):
    """Restarting with a larger pool must not replace retained IDs with lower free IDs."""
    small = CpuPools("4-5")
    assert (
        api(small, "allocate_cores", num_of_cores=1, preemption_policy=POLICY)["allocated_cores"]
        == "4"
    )
    assert api(configured, "allocate_cores_percent", percent=25)["allocated_cores"] == "4"
    grown = api(configured, "allocate_numa_cores", numa_node=0, num_of_cores=3)
    assert grown["cores_allocated"] == "2-4"
    assert grown["preemption_policy"] == POLICY


@pytest.mark.parametrize("action,params,field", REQUESTS)
def test_offline_protected_claim_cannot_be_replaced(
    configured, mock_cpu_files_empty, action, params, field
):
    """Offline ownership survives requests, zero capacity, online return and full release."""
    assert api(configured, action, preemption_policy=POLICY, **params)[field] == "2-5"
    mock_cpu_files_empty["online"].write_text("0-2,6-7")
    before = allocations_db._state_store.read_all()
    for action, params in (
        ("allocate_cores", {"num_of_cores": 1}),
        ("allocate_cores_percent", {"percent": 1}),
        ("allocate_numa_cores", {"numa_node": 0, "num_of_cores": 1}),
    ):
        assert "error" in api(configured, action, **params)
    assert allocations_db._state_store.read_all() == before
    mock_cpu_files_empty["online"].write_text("0-1,6-7")
    listing = api(configured)
    assert listing["remaining_available_cpus"] == listing["total_available_cpus"] == 0
    assert listing["cpu_pool"]["unavailable_allocated_cpus"] == "2-5"
    assert listing["allocations"][0]["preemption_policy"] == POLICY
    mock_cpu_files_empty["online"].write_text("0-7")
    assert "error" in api(configured, "allocate_numa_cores", "other", numa_node=0, num_of_cores=1)
    mock_cpu_files_empty["online"].write_text("0-1,6-7")
    assert api(configured, "allocate_cores_percent", percent=0)["preemption_policy"] is None


@pytest.mark.parametrize("action,params,field", REQUESTS)
def test_failed_online_check_does_not_upgrade_or_reclaim(
    configured, monkeypatch, action, params, field
):
    """Selection and final online validation run before committing either claims or policy."""
    api(configured, "allocate_cores", num_of_cores=1)
    before = allocations_db._state_store.read_all()

    def unavailable(selected):
        assert selected <= {2, 3, 4, 5}
        raise ValueError("CPU offline; retry")

    monkeypatch.setattr(configured.general, "check_online", unavailable)
    assert "retry" in api(configured, action, preemption_policy=POLICY, **params)["error"]
    assert allocations_db._state_store.read_all() == before
    assert AllocationsDB().get_preemption_policy("owner") == PreemptionPolicy.LEGACY


def test_pool_conflict_validation_uses_locked_fresh_claims(configured, monkeypatch):
    """A claim from another instance between requests cannot escape pool validation."""
    original = allocations_db.transaction

    def claim_before_lock(operation):
        AllocationsDB().allocate_cores(
            "other", "7", PreemptionPolicy.NON_PREEMPTIVE, pool=CpuPoolName.GENERAL
        )
        return original(operation)

    monkeypatch.setattr(allocations_db, "transaction", claim_before_lock)
    result = api(configured, "allocate_cores", num_of_cores=1, preemption_policy=POLICY)
    assert "excludes allocated CPUs: 7" in result["error"]
    assert AllocationsDB().get_allocation("owner") is None
    assert AllocationsDB().get_allocation("other") == "7"


@pytest.mark.parametrize("action,params,field", REQUESTS)
def test_configured_policy_write_failure_preserves_state(
    configured, monkeypatch, action, params, field
):
    """No API may acknowledge protected grants when configured-pool state cannot persist."""
    api(configured, "allocate_cores", num_of_cores=1, preemption_policy=POLICY)
    before = allocations_db._state_store.read_all()

    def fail(*args):
        raise OSError("state write rejected")

    monkeypatch.setattr("epa_orchestrator.state_store.os.replace", fail)
    assert "state write rejected" in api(configured, action, **params)["error"]
    assert allocations_db._state_store.read_all() == before
