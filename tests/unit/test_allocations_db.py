# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Concise unit tests for epa_orchestrator.allocations_db."""

# These tests intentionally contrast opt-in protection with legacy reclamation.
import concurrent.futures
import threading

import pytest

from epa_orchestrator.allocations_db import AllocationsDB
from epa_orchestrator.schemas import PreemptionPolicy
from epa_orchestrator.state_store import StateCorruptionError


class TestAllocationsDB:
    """Unit tests for AllocationsDB class."""

    def test_allocate_and_get_allocation(self, fresh_allocations_db):
        """Test allocation and retrieval of CPU cores."""
        fresh_allocations_db.allocate_cores("snap1", "0-1")
        assert fresh_allocations_db.get_allocation("snap1") == "0-1"
        assert fresh_allocations_db._allocated_cpus == {0, 1}

    def test_remove_allocation(self, fresh_allocations_db):
        """Test removal of a CPU allocation."""
        fresh_allocations_db.allocate_cores("snap1", "0-1")
        assert fresh_allocations_db.remove_allocation("snap1") is True
        assert fresh_allocations_db.get_allocation("snap1") is None
        assert fresh_allocations_db._allocated_cpus == set()

    def test_get_system_stats(self, fresh_allocations_db):
        """Test retrieval of system statistics."""
        fresh_allocations_db.allocate_cores("snap1", "0-1")
        stats = fresh_allocations_db.get_system_stats("0-3")
        assert stats["total_available_cpus"] == 4
        assert stats["total_allocated_cpus"] == 2
        assert stats["remaining_available_cpus"] == 2
        assert stats["total_allocations"] == 1

    def test_stats_with_unavailable_claims(self, fresh_allocations_db):
        """Offline/out-of-pool claims count as allocated but not against free CPUs."""
        fresh_allocations_db.allocate_cores("owner", "0-3")
        assert fresh_allocations_db.get_claimed_cpus() == {0, 1, 2, 3}
        stats = fresh_allocations_db.get_system_stats("3-5")
        assert stats["total_allocated_cpus"] == 4
        assert stats["total_available_cpus"] == 3
        assert stats["remaining_available_cpus"] == 2
        assert fresh_allocations_db.get_system_stats("")["remaining_available_cpus"] == 0

    def test_can_allocate_cpus(self, fresh_allocations_db):
        """Test checking if CPUs can be allocated."""
        assert fresh_allocations_db.can_allocate_cpus(2, "0-3") is True
        fresh_allocations_db.allocate_cores("snap1", "0-3")
        assert fresh_allocations_db.can_allocate_cpus(1, "0-3") is False

    def test_get_available_cpus_for_service_includes_own_allocation(self, fresh_allocations_db):
        """Re-allocation pool must include service's existing cores."""
        isolated = "96-127,224-255,352-383,480-511"  # 128 cores
        fresh_allocations_db.allocate_cores(
            "openstack-hypervisor", "96-127,224-255,352-383,480-495"
        )
        # Old get_available_cpus: excludes own allocation, so only 496-511 (16 CPUs)
        old_available = fresh_allocations_db.get_available_cpus(isolated)
        assert len(old_available) == 16
        # New get_available_cpus_for_service: includes own allocation
        new_available = fresh_allocations_db.get_available_cpus_for_service(
            "openstack-hypervisor", isolated
        )
        assert len(new_available) == 128


PROTECTED = PreemptionPolicy.NON_PREEMPTIVE
LEGACY = PreemptionPolicy.LEGACY


def check_online(cpus):
    """Model a deterministic online set for database policy-only tests."""
    assert cpus <= set(range(8))


@pytest.fixture
def numa_db(fresh_allocations_db, monkeypatch):
    """Provide two deterministic NUMA nodes with singleton SMT topology."""
    monkeypatch.setattr(
        "epa_orchestrator.utils.get_numa_node_cpus",
        lambda: {0: set(range(4)), 1: set(range(4, 8))},
    )
    monkeypatch.setattr(
        "epa_orchestrator.allocations_db.get_thread_siblings_map",
        lambda cpus: {cpu: {cpu} for cpu in cpus},
    )
    return fresh_allocations_db


@pytest.mark.parametrize(
    "owner_policy,requester_policy", [(PROTECTED, LEGACY), (LEGACY, PROTECTED)]
)
def test_numa_cannot_steal(numa_db, owner_policy, requester_policy):
    """Protection prevents both being reclaimed and reclaiming foreign ordinary claims."""
    numa_db.allocate_cores("a", "0-3", owner_policy)
    before = numa_db._snapshot()
    assert (
        numa_db.allocate_numa_cores(
            "b", 0, 4, requester_policy, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == ""
    )
    assert numa_db._snapshot() == before
    with pytest.raises(ValueError, match="Cannot reclaim"):
        numa_db._apply_numa_explicit_allocation(
            "b",
            0,
            {0, 1},
            requester_policy,
            check_online=check_online,
            eligible_cpus=set(range(8)),
        )
    assert numa_db._snapshot() == before
    with pytest.raises(ValueError, match="Cannot reclaim"):
        numa_db._subtract_cpus_from_service(
            "a", {0}, requester="b", requester_policy=requester_policy
        )
    assert numa_db._snapshot() == before


def test_legacy_reclamation_and_explicit_protection(numa_db):
    """Legacy NUMA still reclaims ordinary claims but cannot reclaim explicit claims."""
    numa_db.allocate_cores("a", "0-3")
    assert (
        numa_db.allocate_numa_cores(
            "b", 0, 2, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == "0-1"
    )
    assert numa_db.get_allocation("a") == "2-3"
    with pytest.raises(ValueError, match="Cannot reclaim"):
        numa_db._apply_numa_explicit_allocation(
            "c", 0, {0}, check_online=check_online, eligible_cpus=set(range(8))
        )
    assert (
        numa_db.allocate_numa_cores(
            "c", 0, 2, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == "2-3"
    )


def test_protected_numa_uses_only_free_cpus(numa_db):
    """A protected request may use free CPUs alongside legacy owned CPUs."""
    numa_db.allocate_cores("a", "0-1")
    assert (
        numa_db.allocate_numa_cores(
            "b", 0, 2, PROTECTED, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == "2-3"
    )
    assert numa_db.get_allocation("a") == "0-1"


@pytest.mark.parametrize("numa", [False, True])
def test_stable_retry_growth_shrink_and_failed_growth(numa_db, numa):
    """Retain own IDs ahead of lower free IDs for both ordinary and NUMA calls."""
    numa_db.allocate_cores("a", "2", PROTECTED)

    def allocate(count):
        if numa:
            result = numa_db.allocate_numa_cores(
                "a", 0, count, check_online=check_online, eligible_cpus=set(range(8))
            )[0]
            if not result:
                raise ValueError("Insufficient")
            return result
        return numa_db.allocate_count("a", count, "0-3", check_online=check_online)[1]

    assert allocate(1) == "2"
    assert allocate(2) == "0,2"
    assert allocate(1) == "0"
    before = numa_db._snapshot()
    with pytest.raises(ValueError):
        allocate(5)
    assert numa_db._snapshot() == before
    assert numa_db.get_preemption_policy("a") == PROTECTED


def test_policy_inheritance_release_restart_and_downgrade(numa_db):
    """Policy spans API types and nodes, survives restart and ends only on full release."""
    numa_db.allocate_cores("a", "0", PROTECTED)
    assert (
        numa_db.allocate_numa_cores(
            "a", 1, 2, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == "4-5"
    )
    reloaded = AllocationsDB()
    assert reloaded.get_allocation("a") == "0,4-5"
    assert reloaded.get_preemption_policy("a") == PROTECTED
    with pytest.raises(ValueError, match="Fully release"):
        reloaded.allocate_count("a", 3, "0-7", LEGACY, check_online=check_online)
    reloaded.allocate_numa_cores(
        "a", 0, -1, LEGACY, check_online=check_online, eligible_cpus=set(range(8))
    )
    assert reloaded.get_allocation("a") == "4-5"
    assert reloaded.get_preemption_policy("a") == PROTECTED
    reloaded.allocate_numa_cores(
        "a", 1, -1, check_online=check_online, eligible_cpus=set(range(8))
    )
    assert reloaded.get_preemption_policy("a") is None
    assert reloaded._snapshot()["preemption_policies"] == {}
    reloaded.allocate_count("a", 1, "0-7", check_online=check_online)
    assert reloaded.get_preemption_policy("a") == LEGACY


def test_upgrade_only_with_success(numa_db):
    """Failed upgrade preserves policy and claims; success protects every service claim."""
    numa_db.allocate_cores("a", "0")
    before = numa_db._snapshot()
    assert (
        numa_db.allocate_numa_cores(
            "a", 1, 5, PROTECTED, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == ""
    )
    assert numa_db._snapshot() == before
    numa_db.allocate_numa_cores(
        "a", 1, 1, PROTECTED, check_online=check_online, eligible_cpus=set(range(8))
    )
    assert numa_db.get_allocation("a") == "0,4"
    assert (
        numa_db.allocate_numa_cores(
            "b", 0, 4, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == ""
    )


def test_old_state_explicit_claims_stay_protected(numa_db):
    """Absent policy metadata migrates to legacy without weakening explicit ownership."""
    numa_db._state_store.update_section(
        "allocations_db", {"allocations": {"a": "0-3"}, "explicit_allocations": {"a": "0-1"}}
    )
    assert numa_db.get_preemption_policy("a") == LEGACY
    assert (
        numa_db.allocate_numa_cores(
            "b", 0, 2, check_online=check_online, eligible_cpus=set(range(8))
        )[0]
        == "2-3"
    )
    assert numa_db.get_allocation("a") == "0-1"


@pytest.mark.parametrize(
    "policies", [None, [], "legacy", {"a": "invalid"}, {"a": None}, {"a": []}, {"ghost": "legacy"}]
)
def test_malformed_policy_fails_closed(numa_db, policies):
    """Never convert malformed policy state to reclaimable legacy ownership."""
    numa_db._state_store.update_section(
        "allocations_db", {"allocations": {"a": "0-3"}, "preemption_policies": policies}
    )
    before = numa_db._state_store.read_all()
    try:
        with pytest.raises(StateCorruptionError):
            numa_db.allocate_numa_cores(
                "b", 0, 4, check_online=check_online, eligible_cpus=set(range(8))
            )
        with pytest.raises(StateCorruptionError):
            AllocationsDB()
        assert numa_db._state_store.read_all() == before
    finally:
        numa_db._state_store.update_section("allocations_db", {})


def test_overlap_failure_preserves_prior_allocation(numa_db):
    """Direct replacement cannot discard the old claim before detecting overlap."""
    numa_db.allocate_cores("a", "0", PROTECTED)
    numa_db.allocate_cores("b", "1")
    before = numa_db._snapshot()
    with pytest.raises(ValueError):
        numa_db.allocate_cores("a", "0-1")
    assert numa_db._snapshot() == before


@pytest.mark.parametrize("numa", [False, True])
def test_offline_claims_require_explicit_release(numa_db, monkeypatch, numa):
    """Unavailable own CPUs remain owned until an explicit release, not implicit resize."""
    numa_db.allocate_cores("a", "2-3", PROTECTED)
    before = numa_db._snapshot()
    with pytest.raises(ValueError, match="outside the eligible"):
        if numa:
            numa_db.allocate_numa_cores(
                "a", 0, 1, check_online=check_online, eligible_cpus=set(range(3))
            )
        else:
            numa_db.allocate_count("a", 1, "0-2", check_online=check_online)
    assert numa_db._snapshot() == before
    numa_db.remove_allocation("a")
    assert numa_db.get_allocation("a") is None


def test_separate_instances_validate_under_lock(numa_db):
    """Simultaneous full-pool claims have exactly one winner, including after reload."""
    databases = [AllocationsDB(), AllocationsDB()]
    barrier = threading.Barrier(2)

    def allocate(index):
        barrier.wait(timeout=5)
        try:
            databases[index].allocate_count(
                str(index), 4, "0-3", PROTECTED, check_online=check_online
            )
            return True
        except ValueError:
            return False

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        results = list(executor.map(allocate, range(2)))
    assert sorted(results) == [False, True]
    entries = AllocationsDB().get_all_allocations()
    assert len(entries) == 1
    assert entries[0].allocated_cores == "0-3"
    assert entries[0].preemption_policy == PROTECTED


def test_internal_numa_application_validates_placement(numa_db):
    """The guarded internal entry point cannot install IDs from another node."""
    numa_db.allocate_cores("a", "0", PROTECTED)
    before = numa_db._snapshot()
    with pytest.raises(ValueError, match="outside the eligible NUMA"):
        numa_db._apply_numa_explicit_allocation(
            "a", 0, {4}, check_online=check_online, eligible_cpus=set(range(8))
        )
    assert numa_db._snapshot() == before


def test_internal_subtraction_inherits_requester_policy(numa_db):
    """A helper cannot downgrade a protected requester by omitting its policy argument."""
    numa_db.allocate_cores("a", "0")
    numa_db.allocate_cores("b", "1", PROTECTED)
    before = numa_db._snapshot()
    with pytest.raises(ValueError, match="Cannot reclaim"):
        numa_db._subtract_cpus_from_service("a", {0}, requester="b")
    with pytest.raises(ValueError, match="Fully release"):
        numa_db._subtract_cpus_from_service("a", {0}, requester="b", requester_policy=LEGACY)
    assert numa_db._snapshot() == before


@pytest.mark.parametrize("cores", [" ", ",", " , "])
def test_cpu_less_allocation_never_persists_policy(numa_db, cores):
    """A range string without CPUs must not write policy metadata that fails to load."""
    before = numa_db._state_store.read_section("allocations_db")
    numa_db.allocate_cores("ghost", cores, PROTECTED)
    assert numa_db.get_allocation("ghost") is None
    assert numa_db.get_preemption_policy("ghost") is None
    assert numa_db._state_store.read_section("allocations_db") == before
    assert "ghost" not in numa_db._snapshot()["preemption_policies"]
    # The store stays loadable, so the daemon and configure hook still start.
    assert AllocationsDB().get_all_allocations() == []
