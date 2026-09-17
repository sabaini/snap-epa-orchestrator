# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""State transaction, durable commit and uncertainty latch regression tests."""

import os
from pathlib import Path

import pytest

from epa_orchestrator.allocations_db import AllocationsDB
from epa_orchestrator.schemas import PreemptionPolicy
from epa_orchestrator.state_store import (
    StateCorruptionError,
    StateStore,
    StateUncertainError,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolate latch failures from the shared daemon fixtures."""
    monkeypatch.setenv("SNAP_DATA", str(tmp_path))
    return StateStore()


def test_transaction_preserves_other_sections_and_callback_failure(store):
    """Callback sees fresh state; failure publishes no partial mutation."""
    store.update_section("hugepages_db", {"keep": 7})
    store.update_section("allocations_db", {"value": 1})
    assert (
        store.transaction_section(
            "allocations_db", lambda current: ({"value": current["value"] + 1}, "result")
        )
        == "result"
    )
    assert store.read_section("hugepages_db") == {"keep": 7}
    before = store.read_all()

    def fail(current):
        current["value"] = 99
        raise ValueError("validation")

    with pytest.raises(ValueError, match="validation"):
        store.transaction_section("allocations_db", fail)
    assert store.read_all() == before


@pytest.mark.parametrize("failure", ["replace", "temp_fsync"])
def test_failure_before_replace_preserves_claims_and_allows_retry(store, monkeypatch, failure):
    """Known pre-replacement failure preserves old disk bytes and published state."""
    db = AllocationsDB()
    db.allocate_cores("a", "2-3", PreemptionPolicy.NON_PREEMPTIVE)
    before = store.read_all()
    before_memory = db._snapshot()

    def fail(*args):
        raise OSError("storage failed")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace" if failure == "replace" else "fsync", fail)
        with pytest.raises(OSError):
            db.allocate_cores("a", "0-3")
    assert store.read_all() == before
    assert db._snapshot() == before_memory
    db.allocate_cores("a", "0-3")
    assert db.get_allocation("a") == "0-3"
    assert db.get_preemption_policy("a") == PreemptionPolicy.NON_PREEMPTIVE


def test_post_replace_failure_blocks_all_writers_and_recovers(store, monkeypatch):
    """Observed new claims survive uncertain failure; every writer honors the latch."""
    db = AllocationsDB()
    db.allocate_cores("a", "2", PreemptionPolicy.NON_PREEMPTIVE)
    original_sync = StateStore._sync_directory
    calls = 0

    def fail_commit_sync(self):
        nonlocal calls
        calls += 1
        if calls == 2:  # First sync durably establishes the lock-file marker.
            raise OSError("directory fsync failed after replace")
        original_sync(self)

    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "_sync_directory", fail_commit_sync)
        with pytest.raises(StateUncertainError):
            db.allocate_cores("a", "2-3")
    assert db._snapshot()["allocations"] == {"a": "2-3"}
    assert store.read_section("allocations_db")["allocations"] == {"a": "2-3"}
    assert Path(store._lock_path).read_bytes()
    # Simulate a fresh process: disk marker alone must block all write entry points.
    StateStore._uncertain_paths.discard(store._lock_path)
    restarted = StateStore()
    for write in [
        lambda: restarted.write_all({}),
        lambda: restarted.update_section("hugepages_db", {}),
        lambda: restarted.transaction_section("allocations_db", lambda data: ({}, None)),
        lambda: AllocationsDB().remove_allocation("a"),
    ]:
        with pytest.raises(StateUncertainError):
            write()
    restarted.recover()
    assert Path(store._lock_path).read_bytes() == b""
    restarted.update_section("hugepages_db", {"preserved": True})
    assert AllocationsDB().get_allocation("a") == "2-3"
    AllocationsDB().allocate_count(
        "b", 2, "0-3", PreemptionPolicy.NON_PREEMPTIVE, check_online=lambda cpus: None
    )
    assert AllocationsDB().get_allocation("b") == "0-1"


def test_marker_is_durable_before_replace(store, monkeypatch):
    """Replacement is forbidden if establishing the crash latch cannot be confirmed."""
    store.write_all({"old": True})
    before = store.read_all()
    real_sync = StateStore._sync_directory
    replaced = []

    def fail_sync(self):
        raise OSError("cannot persist marker")

    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "_sync_directory", fail_sync)
        patch.setattr(os, "replace", lambda *args: replaced.append(args))
        with pytest.raises(OSError):
            store.write_all({"new": True})
    assert not replaced
    assert store.read_all() == before
    with pytest.raises(StateUncertainError):
        StateStore().write_all({})
    assert real_sync is StateStore._sync_directory
    store.recover()


def test_marker_cleanup_failure_stays_blocked(store, monkeypatch):
    """Cleanup errors do not allow writes after a known pre-replacement failure."""
    store.write_all({"old": True})
    original = StateStore._set_uncertain_unlocked

    def fail_cleanup(self, uncertain):
        if not uncertain:
            raise OSError("latch cleanup failed")
        original(self, uncertain)

    def fail_replace(*args):
        raise OSError("replace failed")

    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "_set_uncertain_unlocked", fail_cleanup)
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError):
            store.write_all({"new": True})
    with pytest.raises(StateUncertainError):
        StateStore().write_all({})
    assert store.read_all()["old"] is True
    store.recover()


def test_recovery_failure_keeps_latch(store, monkeypatch):
    """Only successful durable recovery clears a crash marker."""
    store.write_all({"claims": True})
    Path(store._lock_path).write_bytes(b"uncertain\n")

    def fail(*args):
        raise OSError("storage still unhealthy")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(OSError):
            store.recover()
    with pytest.raises(StateUncertainError):
        StateStore().write_all({})
    store.recover()
    store.update_section("new", {})


@pytest.mark.parametrize("state", ["[]", '{"allocations_db": []}', "not json"])
def test_invalid_state_fails_closed(store, state):
    """Malformed roots and sections must not appear to be empty allocation pools."""
    Path(store._file_path).write_text(state)
    with pytest.raises(StateCorruptionError):
        store.read_section("allocations_db")
    with pytest.raises(StateCorruptionError):
        store.transaction_section("allocations_db", lambda data: ({}, None))
