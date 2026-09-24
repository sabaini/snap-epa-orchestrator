# SPDX-FileCopyrightText: 2026 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Allocation/accounting regressions with an ordinary configured CPU pool."""

import json
from unittest.mock import patch

import pytest

from epa_orchestrator.allocations_db import AllocationsDB, allocations_db
from epa_orchestrator.cpu_pool import (
    CpuPoolProvider,
    load_startup_pool,
    validate_snap_configuration,
)
from epa_orchestrator.daemon_handler import handle_daemon_request, validate_startup_pool
from epa_orchestrator.utils import parse_cpu_ranges


@pytest.fixture
def pool(mock_cpu_files_empty):
    """Use a subset of a NUMA node, with no isolated CPUs at all."""
    with (
        patch(
            "epa_orchestrator.daemon_handler.get_numa_node_cpus", return_value={0: set(range(8))}
        ),
        patch("epa_orchestrator.utils.get_numa_node_cpus", return_value={0: set(range(8))}),
        patch("epa_orchestrator.cpu_pinning._read_file_strict", return_value="0-7"),
    ):
        yield CpuPoolProvider("2-5")


def request(pool, action="allocate_cores", service="owner", **kwargs):
    """Send an API request with a test provider, requiring no snapd access."""
    payload = {"action": action, "service_name": service, **kwargs}
    return json.loads(handle_daemon_request(json.dumps(payload).encode(), pool))


@pytest.mark.parametrize(
    "action,params,field,count",
    [
        ("allocate_cores", {"num_of_cores": 2}, "allocated_cores", 2),
        ("allocate_cores", {"num_of_cores": 0}, "allocated_cores", 3),
        ("allocate_cores_percent", {"percent": 50}, "allocated_cores", 2),
        ("allocate_numa_cores", {"num_of_cores": 2, "numa_node": 0}, "cores_allocated", 2),
    ],
)
def test_configured_allocation_success(pool, action, params, field, count):
    """All allocation APIs must succeed, never pulling siblings outside the pool."""
    result = request(pool, action, **params)
    assert "error" not in result
    selected = parse_cpu_ranges(result[field])
    assert len(selected) == count
    assert selected <= {2, 3, 4, 5}
    assert result["total_available_cpus"] == 4
    assert result["remaining_available_cpus"] == 4 - count


def test_percentage_capacity_not_free_and_single_snapshot(pool):
    """Percentage uses full eligible capacity and one snapshot, even with other owners."""
    request(pool, service="first", num_of_cores=3)
    before = allocations_db._state_store.read_all()
    with patch.object(pool, "snapshot", wraps=pool.snapshot) as snapshot:
        result = request(pool, "allocate_cores_percent", service="second", percent=50)
        snapshot.assert_called_once()
    assert "Requested: 2, Available: 1" in result["error"]
    assert allocations_db._state_store.read_all() == before


def test_offline_claims_and_restart(pool, mock_cpu_files_empty):
    """Offline ownership survives listing, restart and coming online again."""
    request(pool, num_of_cores=2)
    mock_cpu_files_empty["online"].write_text("0-1,4-7")
    listing = request(pool, "list_allocations")
    assert listing["total_allocated_cpus"] == 2
    assert listing["remaining_available_cpus"] == 2
    assert listing["cpu_pool"] == {
        "source": "configured",
        "configured_cpus": "2-5",
        "eligible_cpus": "4-5",
        "unavailable_allocated_cpus": "2-3",
    }
    assert AllocationsDB().get_allocation("owner") == "2-3"
    assert request(pool, service="other", num_of_cores=1)["allocated_cores"] == "4"
    mock_cpu_files_empty["online"].write_text("0-7")
    assert request(pool, service="third", num_of_cores=1)["allocated_cores"] == "5"
    assert allocations_db.get_allocation("owner") == "2-3"


@pytest.mark.parametrize(
    "action,params",
    [
        ("allocate_cores", {"num_of_cores": -1}),
        ("allocate_cores_percent", {"percent": 0}),
        ("allocate_cores_percent", {"percent": -1}),
        ("allocate_numa_cores", {"num_of_cores": -1, "numa_node": 0}),
    ],
)
def test_empty_pool_keeps_ledger_and_allows_release(pool, mock_cpu_files_empty, action, params):
    """A zero-capacity pool never hides owners or produces negative free capacity."""
    request(pool, num_of_cores=2)
    mock_cpu_files_empty["online"].write_text("0-1,6-7")
    listing = request(pool, "list_allocations")
    assert listing["total_allocations"] == 1
    assert listing["total_allocated_cpus"] == 2
    assert listing["total_available_cpus"] == listing["remaining_available_cpus"] == 0
    assert "error" not in request(pool, action, **params)
    assert allocations_db.get_allocation("owner") is None


def test_pool_conflict_startup_race_and_recovery(pool, caplog):
    """Claims made after hook validation block grants after restart, not releases."""
    pending = CpuPoolProvider("4-5")
    pending.validate_claims(allocations_db.get_claimed_cpus())
    request(pool, num_of_cores=2)  # active daemon grants CPUs after configure hook
    validate_startup_pool(pending)
    assert "New CPU allocations blocked" in caplog.text
    before = allocations_db._state_store.read_all()
    for action, params in (
        ("allocate_cores", {"num_of_cores": 1}),
        ("allocate_cores_percent", {"percent": 25}),
        ("allocate_numa_cores", {"num_of_cores": 1, "numa_node": 0}),
    ):
        assert "excludes allocated CPUs" in request(pending, action, **params)["error"]
    assert allocations_db._state_store.read_all() == before
    assert request(pending, "list_allocations")["cpu_pool"]["unavailable_allocated_cpus"] == "2-3"
    assert "error" not in request(pending, "allocate_numa_cores", num_of_cores=-1, numa_node=0)
    assert request(pending, num_of_cores=1)["allocated_cores"] == "4"


def test_default_empty_pool_keeps_owners(pool):
    """Reverting to empty isolation keeps claims visible and releasable."""
    request(pool, num_of_cores=1)
    default = CpuPoolProvider()
    assert request(default, "list_allocations")["total_allocations"] == 1
    assert "error" not in request(default, num_of_cores=-1)
    assert request(default, num_of_cores=1)["error"] == "No CPUs available"


def test_missing_numa_topology_full_release(pool):
    """Full-service release is the recovery path if node topology disappears."""
    request(pool, num_of_cores=2)
    with patch(
        "epa_orchestrator.daemon_handler.get_numa_node_cpus",
        side_effect=ValueError("NUMA topology not available"),
    ):
        assert "error" in request(pool, "allocate_numa_cores", num_of_cores=-1, numa_node=0)
        assert "error" not in request(pool, num_of_cores=-1)


@pytest.mark.parametrize(
    "action,params",
    [
        ("allocate_cores", {"num_of_cores": 2}),
        ("allocate_cores_percent", {"percent": 50}),
        ("allocate_numa_cores", {"num_of_cores": 2, "numa_node": 0}),
    ],
)
def test_selected_cpu_offlines_before_grant(pool, mock_cpu_files_empty, action, params):
    """An online race must not mutate either requester or preempted owners."""
    request(pool, num_of_cores=1)
    # NUMA selection can displace this ordinary owner; no mutation is allowed on failure.
    request(pool, service="other", num_of_cores=1)
    before = allocations_db._state_store.read_all()
    original_check = pool.check_online

    def offline_then_check(selected):
        mock_cpu_files_empty["online"].write_text("0-1,6-7")
        original_check(selected)

    with patch.object(pool, "check_online", side_effect=offline_then_check):
        result = request(pool, action, **params)
    assert "retry" in result["error"]
    assert allocations_db._state_store.read_all() == before
    assert allocations_db.get_allocation("owner") == "2"
    assert allocations_db.get_allocation("other") == "3"


@pytest.mark.parametrize(
    "action,params",
    [
        ("allocate_cores", {"num_of_cores": 2}),
        ("allocate_cores_percent", {"percent": 50}),
        ("allocate_numa_cores", {"num_of_cores": 2, "numa_node": 0}),
    ],
)
def test_final_online_read_failure(pool, mock_cpu_files_empty, action, params):
    """An unreadable online file at the final check leaves all claims unchanged."""
    request(pool, num_of_cores=1)
    before = allocations_db._state_store.read_all()
    original_check = pool.check_online

    def remove_online_then_check(selected):
        mock_cpu_files_empty["online"].unlink()
        original_check(selected)

    with patch.object(pool, "check_online", side_effect=remove_online_then_check):
        result = request(pool, action, **params)
    assert "Failed to read CPU topology" in result["error"]
    assert allocations_db._state_store.read_all() == before


def test_pending_configuration_does_not_change_active_pool(pool):
    """Introspection describes active choice and requests never invoke snapctl."""
    with patch("epa_orchestrator.cpu_pool.subprocess.run", side_effect=AssertionError("snapctl")):
        assert request(pool, "list_allocations")["cpu_pool"]["configured_cpus"] == "2-5"
        assert request(pool, num_of_cores=1)["allocated_cores"] == "2"


@pytest.mark.parametrize(
    "action,params,field",
    [
        ("allocate_cores", {"num_of_cores": 3}, "allocated_cores"),
        ("allocate_cores_percent", {"percent": 100}, "allocated_cores"),
        ("allocate_numa_cores", {"num_of_cores": 3, "numa_node": 0}, "cores_allocated"),
    ],
)
@pytest.mark.parametrize("missing_cpu", [0, 1])
def test_mixed_sibling_read_failure_grants_exact_count(
    mock_cpu_files_empty, tmp_path, monkeypatch, action, params, field, missing_cpu
):
    """Partial topology reads cannot spend the selection budget on duplicate CPU IDs."""
    monkeypatch.setattr(
        "epa_orchestrator.cpu_pinning.THREAD_SIBLINGS_LIST_TEMPLATE", str(tmp_path / "cpu{cpu}")
    )
    for cpu, siblings in {0: "0-1", 1: "0-1", 2: "2"}.items():
        if cpu != missing_cpu:
            (tmp_path / f"cpu{cpu}").write_text(siblings)
    monkeypatch.setattr("epa_orchestrator.utils.get_numa_node_cpus", lambda: {0: {0, 1, 2}})
    monkeypatch.setattr(
        "epa_orchestrator.daemon_handler.get_numa_node_cpus", lambda: {0: {0, 1, 2}}
    )
    provider = CpuPoolProvider("0-2")
    result = request(provider, action, preemption_policy="non-preemptive", **params)
    assert "error" not in result, result
    assert result[field] == "0-2"
    assert result["remaining_available_cpus"] == 0
    assert AllocationsDB().get_allocation("owner") == "0-2"


@pytest.mark.parametrize(
    "action,params",
    [
        ("allocate_cores", {"num_of_cores": 3}),
        ("allocate_cores", {"num_of_cores": 0}),
        ("allocate_cores_percent", {"percent": 75}),
        ("allocate_numa_cores", {"num_of_cores": 3, "numa_node": 0}),
    ],
)
@pytest.mark.parametrize("selected", [{2, 3}, {2, 3, 7}, {2, 3, 4, 5}])
@pytest.mark.parametrize("original_policy", ["legacy", "non-preemptive"])
def test_invalid_selection_preserves_claims_and_policy(
    pool, monkeypatch, action, params, selected, original_policy
):
    """Short or out-of-pool selection must not commit a replacement or policy upgrade."""
    request(pool, num_of_cores=1, preemption_policy=original_policy)
    before = allocations_db._state_store.read_all()
    monkeypatch.setattr(AllocationsDB, "_select_stable", lambda *args: selected)
    result = request(pool, action, preemption_policy="non-preemptive", **params)
    assert "error" in result, result
    assert allocations_db._state_store.read_all() == before
    assert AllocationsDB().get_preemption_policy("owner") == original_policy


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("numa", [False, True])
def test_retained_protected_cpus_skip_topology_but_validate_and_commit(
    pool, monkeypatch, count, numa
):
    """Retries and shrinks need no free-CPU topology but still check online and persist."""
    request(pool, num_of_cores=2, preemption_policy="non-preemptive")
    action = "allocate_numa_cores" if numa else "allocate_cores"
    params = {"num_of_cores": count, **({"numa_node": 0} if numa else {})}
    with (
        patch("epa_orchestrator.allocations_db.get_thread_siblings_map") as topology,
        patch.object(pool, "check_online", wraps=pool.check_online) as online,
    ):
        result = request(pool, action, **params)
        assert "error" not in result, result
        topology.assert_not_called()
        online.assert_called_once_with(set(range(2, 2 + count)))
    assert AllocationsDB().get_allocation("owner") == ("2" if count == 1 else "2-3")
    before = allocations_db._state_store.read_all()
    with patch.object(pool, "check_online", side_effect=ValueError("offline; retry")):
        assert "offline" in request(pool, action, **params)["error"]
    with patch("epa_orchestrator.state_store.os.replace", side_effect=OSError("write failed")):
        assert "write failed" in request(pool, action, **params)["error"]
    assert allocations_db._state_store.read_all() == before


def test_short_legacy_numa_selection_preserves_all_owners(pool, monkeypatch):
    """The shared NUMA boundary rejects a short result before legacy reclamation."""
    request(pool, num_of_cores=1)
    request(pool, service="other", num_of_cores=1)
    before = allocations_db._state_store.read_all()
    monkeypatch.setattr(AllocationsDB, "_select_numa_cpus_smt_aware", lambda *args: {2, 3})
    result = request(pool, "allocate_numa_cores", num_of_cores=3, numa_node=0)
    assert "exactly 3" in result["error"]
    assert allocations_db._state_store.read_all() == before


def test_short_ordinary_selection_preserves_claims(pool, monkeypatch):
    """Validate even the initial ordinary selector against an explicit requested count."""
    request(pool, num_of_cores=1)
    before = allocations_db._state_store.read_all()
    monkeypatch.setattr(
        "epa_orchestrator.allocations_db.calculate_cpu_pinning", lambda *args: ("4-5", "2-3")
    )
    result = request(pool, num_of_cores=3)
    assert "exactly 3" in result["error"]
    assert allocations_db._state_store.read_all() == before


def test_missing_present_cpus_keep_daemon_serving_and_recoverable(
    pool, mock_cpu_files_empty, snap_pool_options
):
    """A configured CPU leaving the machine must not cost listing, release or reconfiguration."""
    assert request(pool, num_of_cores=2)["allocated_cores"] == "2-3"
    # vCPU removal: the claimed CPUs are no longer present, so they are no longer online.
    mock_cpu_files_empty["present"].write_text("0-1,6-7")
    mock_cpu_files_empty["online"].write_text("0-1,6-7")
    restarted = load_startup_pool_with("2-5")
    listing = request(restarted, "list_allocations")
    assert listing["cpu_pool"]["unavailable_allocated_cpus"] == "2-3"
    assert listing["allocations"][0]["allocated_cores"] == "2-3"
    assert "error" in request(restarted, num_of_cores=1)
    assert "error" not in request(restarted, num_of_cores=-1)
    # With the claims released, the operator can configure a pool of surviving CPUs.
    with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value="0-1"):
        validate_snap_configuration()
    assert request(load_startup_pool_with("0-1"), num_of_cores=1)["allocated_cores"] == "0"


def load_startup_pool_with(configuration):
    """Build the provider exactly as the daemon does at startup."""
    with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value=configuration):
        return load_startup_pool()


def test_zero_capacity_startup_still_lists_and_releases(pool, caplog):
    """Failed pool discovery blocks grants only, never introspection or release."""
    assert request(pool, num_of_cores=2)["allocated_cores"] == "2-3"
    with patch(
        "epa_orchestrator.cpu_pool.read_snap_cpu_pool", side_effect=ValueError("snapctl failed")
    ):
        degraded = load_startup_pool()
    validate_startup_pool(degraded)
    assert "CPU pool discovery failed" in caplog.text
    listing = request(degraded, "list_allocations")
    assert listing["total_available_cpus"] == 0
    assert listing["cpu_pool"] == {
        "source": "isolated",
        "configured_cpus": "",
        "eligible_cpus": "",
        "unavailable_allocated_cpus": "2-3",
    }
    assert "excludes allocated CPUs: 2-3" in request(degraded, num_of_cores=1)["error"]
    assert "error" not in request(degraded, num_of_cores=-1)
    assert allocations_db.get_allocation("owner") is None
    assert request(degraded, num_of_cores=1)["error"] == "No CPUs available"


def test_numa_ownership_error_names_the_blocking_claims(pool):
    """The NUMA failure message must describe protected as well as explicit ownership."""
    request(pool, service="protected", num_of_cores=2, preemption_policy="non-preemptive")
    result = request(pool, "allocate_numa_cores", num_of_cores=4, numa_node=0)
    assert "CPUs 2-3 are held by other services" in result["error"]
    assert "non-preemptive" in result["error"]
    assert allocations_db.get_allocation("owner") is None


@pytest.mark.parametrize("failure", ["missing", "malformed"])
@pytest.mark.parametrize(
    "action,params",
    [
        ("allocate_cores", {"num_of_cores": -1}),
        ("allocate_cores_percent", {"percent": 0}),
        ("allocate_cores_percent", {"percent": -1}),
        ("allocate_numa_cores", {"num_of_cores": -1, "numa_node": 0}),
    ],
)
def test_unreadable_online_keeps_listing_and_release_available(
    pool, mock_cpu_files_empty, caplog, failure, action, params
):
    """Topology failures block grants but never hide or prevent releasing saved claims."""
    assert (
        request(pool, num_of_cores=2, preemption_policy="non-preemptive")["allocated_cores"]
        == "2-3"
    )
    before = allocations_db._state_store.read_all()
    online = mock_cpu_files_empty["online"]
    if failure == "missing":
        online.unlink()
    else:
        online.write_text("not a CPU list")
    for grant, fields in (
        ("allocate_cores", {"num_of_cores": 1}),
        ("allocate_cores_percent", {"percent": 25}),
        ("allocate_numa_cores", {"num_of_cores": 1, "numa_node": 0}),
    ):
        assert "error" in request(pool, grant, service="other", **fields)
    listing = request(pool, "list_allocations")
    assert listing["allocations"][0]["allocated_cores"] == "2-3"
    assert listing["allocations"][0]["preemption_policy"] == "non-preemptive"
    assert listing["total_allocated_cpus"] == 2
    assert listing["total_available_cpus"] == listing["remaining_available_cpus"] == 0
    assert listing["cpu_pool"] == {
        "source": "configured",
        "configured_cpus": "2-5",
        "eligible_cpus": "",
        "unavailable_allocated_cpus": "2-3",
    }
    assert "CPU capacity unavailable" in caplog.text
    assert allocations_db._state_store.read_all() == before
    result = request(pool, action, **params)
    assert "error" not in result, result
    assert result["total_available_cpus"] == result["remaining_available_cpus"] == 0
    assert AllocationsDB().get_allocation("owner") is None
    assert request(pool, "list_allocations")["allocations"] == []
    online.write_text("0-7")
    assert request(pool, "list_allocations")["cpu_pool"]["eligible_cpus"] == "2-5"
    assert request(pool, num_of_cores=1)["allocated_cores"] == "2"
