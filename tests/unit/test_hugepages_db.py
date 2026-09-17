# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for epa_orchestrator.hugepages_db."""

import concurrent.futures
import threading

import pytest
from pydantic import ValidationError

from epa_orchestrator import hugepages_db
from epa_orchestrator.state_store import StateStore, StateUncertainError


@pytest.fixture(autouse=True)
def reset_db():
    """Reset the hugepages database before and after each test (persistent-safe)."""
    hugepages_db.clear_all_allocations()
    yield
    hugepages_db.clear_all_allocations()


def test_record_and_list_allocations():
    """Test recording and listing hugepage allocations."""
    hugepages_db.upsert_allocation("svc-a", 0, 2048, 10)
    hugepages_db.upsert_allocation("svc-a", 0, 1048576, 2)
    hugepages_db.upsert_allocation("svc-b", 1, 2048, 5)

    data = hugepages_db.list_allocations()
    assert set(data.keys()) == {"svc-a", "svc-b"}
    assert {tuple(sorted((d["node_id"], d["size_kb"], d["count"]) for d in data["svc-a"]))} == {
        tuple(sorted(((0, 2048, 10), (0, 1048576, 2))))
    }
    assert len(data["svc-b"]) == 1
    assert data["svc-b"][0]["node_id"] == 1
    assert data["svc-b"][0]["size_kb"] == 2048
    assert data["svc-b"][0]["count"] == 5


def test_list_allocations_for_node_filters():
    """Test filtering hugepage allocations by node."""
    hugepages_db.upsert_allocation("svc-a", 0, 2048, 10)
    hugepages_db.upsert_allocation("svc-a", 1, 2048, 1)
    hugepages_db.upsert_allocation("svc-b", 0, 1048576, 3)

    node0 = hugepages_db.list_allocations_for_node(0)
    assert {e["service_name"] for e in node0} == {"svc-a", "svc-b"}
    assert any(e["size_kb"] == 2048 and e["count"] == 10 for e in node0)
    assert any(e["size_kb"] == 1048576 and e["count"] == 3 for e in node0)

    node1 = hugepages_db.list_allocations_for_node(1)
    assert node1 == [{"service_name": "svc-a", "size_kb": 2048, "count": 1}]


def test_upsert_replaces_for_same_key():
    """Upsert should replace prior entry for same service/node/size."""
    hugepages_db.upsert_allocation("svc-x", 0, 2048, 2)
    hugepages_db.upsert_allocation("svc-x", 0, 2048, 7)

    data = hugepages_db.list_allocations()
    assert set(data.keys()) == {"svc-x"}
    assert data["svc-x"] == [{"node_id": 0, "size_kb": 2048, "count": 7}]


def test_remove_allocation_for_key_removes_matching_entries():
    """Remove only the records matching service+node+size, keep others."""
    hugepages_db.upsert_allocation("svc-a", 0, 2048, 10)
    hugepages_db.upsert_allocation("svc-a", 0, 1048576, 2)
    hugepages_db.upsert_allocation("svc-a", 1, 2048, 1)
    hugepages_db.upsert_allocation("svc-b", 0, 2048, 5)

    removed = hugepages_db.remove_allocation_for_key("svc-a", 0, 2048)
    assert removed is True
    data = hugepages_db.list_allocations()
    # svc-a should still have the other two entries
    assert {tuple(sorted((d["node_id"], d["size_kb"], d["count"]) for d in data["svc-a"]))} == {
        tuple(sorted(((0, 1048576, 2), (1, 2048, 1))))
    }
    # svc-b unchanged
    assert data["svc-b"] == [{"node_id": 0, "size_kb": 2048, "count": 5}]


def test_remove_allocation_for_key_noop_when_missing():
    """Removing a non-existent key returns False and leaves state unchanged."""
    hugepages_db.upsert_allocation("svc-a", 0, 2048, 10)
    data_before = hugepages_db.list_allocations()
    removed = hugepages_db.remove_allocation_for_key("svc-a", 1, 2048)
    assert removed is False
    assert data_before == hugepages_db.list_allocations()


def test_remove_allocation_service_cleanup_when_empty():
    """Service entry is removed when all its records are deleted."""
    hugepages_db.upsert_allocation("svc-a", 0, 2048, 10)
    removed = hugepages_db.remove_allocation_for_key("svc-a", 0, 2048)
    assert removed is True
    assert hugepages_db.get_allocation("svc-a") is None


@pytest.mark.parametrize("service", ["svc", "new"])
def test_invalid_upsert_does_not_mutate_memory_or_disk(service):
    """Validate a replacement before removing any prior record or adding an empty owner."""
    hugepages_db.upsert_allocation("svc", 0, 2048, 10)
    before = hugepages_db.list_allocations()
    disk = hugepages_db._store.read_all()
    with pytest.raises(ValidationError):
        hugepages_db.upsert_allocation(service, 0, 2048, "invalid")
    assert hugepages_db._allocations == before
    assert hugepages_db._store.read_all() == disk


@pytest.mark.parametrize("operation", ["upsert", "release", "clear"])
@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_hugepage_pre_replace_failure_preserves_state(monkeypatch, operation, failure):
    """Every writer propagates a known storage failure without publishing the candidate."""
    hugepages_db.upsert_allocation("svc", 0, 2048, 10)
    before = hugepages_db.list_allocations()
    disk = hugepages_db._store.read_all()

    def fail(*args):
        raise OSError("write failed")

    with monkeypatch.context() as patcher:
        patcher.setattr(f"epa_orchestrator.state_store.os.{failure}", fail)
        with pytest.raises(OSError, match="write failed"):
            _mutate_hugepages(operation)
    assert hugepages_db._allocations == before
    assert hugepages_db._store.read_all() == disk
    _mutate_hugepages(operation)
    assert hugepages_db._allocations != before


def _mutate_hugepages(operation):
    if operation == "upsert":
        hugepages_db.upsert_allocation("svc", 0, 2048, 20)
    elif operation == "release":
        hugepages_db.remove_allocation_for_key("svc", 0, 2048)
    else:
        hugepages_db.clear_all_allocations()


@pytest.mark.parametrize("operation", ["upsert", "release", "clear"])
def test_hugepage_uncertain_commit_reconciles_and_blocks(monkeypatch, operation):
    """A failed post-replace sync exposes observed claims, blocks writers and recovers."""
    hugepages_db.upsert_allocation("svc", 0, 2048, 10)
    sync = StateStore._sync_directory
    calls = 0

    def fail(self):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("uncertain commit")
        sync(self)

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(StateStore, "_sync_directory", fail)
            with pytest.raises(StateUncertainError):
                _mutate_hugepages(operation)
        observed = hugepages_db._store.read_section("hugepages_db")["allocations"]
        assert hugepages_db._allocations == observed
        assert observed == (
            {"svc": [{"node_id": 0, "size_kb": 2048, "count": 20}]}
            if operation == "upsert"
            else {}
        )
        for mutation in ("upsert", "release", "clear"):
            with pytest.raises(StateUncertainError):
                _mutate_hugepages(mutation)
            assert hugepages_db._allocations == observed
        hugepages_db._load_from_store()
        assert hugepages_db.list_allocations() == observed
    finally:
        hugepages_db._store.recover()
    hugepages_db.upsert_allocation("after-recovery", 0, 2048, 1)
    assert hugepages_db.get_allocation("after-recovery")[0]["count"] == 1


def test_hugepage_transactions_use_fresh_state_and_preserve_other_sections():
    """A stale process cache must not overwrite other owners or unrelated state."""
    store = StateStore()
    hugepages_db.upsert_allocation("stale", 0, 2048, 10)
    store.update_section("unrelated", {"keep": True})
    store.update_section(
        "hugepages_db",
        {
            "allocations": {"other": [{"node_id": 1, "size_kb": 2048, "count": 3}]},
            "metadata": {"keep": True},
        },
    )
    hugepages_db.upsert_allocation("new", 0, 2048, 1)
    assert set(hugepages_db.list_allocations()) == {"other", "new"}
    assert store.read_section("unrelated") == {"keep": True}
    assert store.read_section("hugepages_db")["metadata"] == {"keep": True}
    store.update_section("hugepages_db", {})
    hugepages_db._load_from_store()
    assert hugepages_db.list_allocations() == {}


def test_hugepage_concurrent_writers_preserve_both_owners():
    """Competing mutations must read and replace state under the same lock."""
    barrier = threading.Barrier(2)

    def allocate(index):
        barrier.wait(timeout=5)
        hugepages_db.upsert_allocation(f"owner-{index}", 0, 2048, index + 1)

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        list(executor.map(allocate, range(2)))
    expected = {
        f"owner-{index}": [{"node_id": 0, "size_kb": 2048, "count": index + 1}]
        for index in range(2)
    }
    assert StateStore().read_section("hugepages_db")["allocations"] == expected
    assert hugepages_db.list_allocations() == expected


def test_legacy_hugepage_state_remains_readable():
    """Legacy coercion/filtering stays compatible when a new owner is committed."""
    StateStore().update_section(
        "hugepages_db",
        {
            "allocations": {
                "old-owner": [
                    {"node_id": "0", "size_kb": "2048", "count": "2"},
                    {"node_id": 1},
                ],
                "invalid": None,
            },
        },
    )
    hugepages_db.upsert_allocation("new-owner", 1, 2048, 1)
    assert hugepages_db.list_allocations() == {
        "old-owner": [{"node_id": 0, "size_kb": 2048, "count": 2}],
        "new-owner": [{"node_id": 1, "size_kb": 2048, "count": 1}],
    }
