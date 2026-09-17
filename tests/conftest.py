# SPDX-FileCopyrightText: 2024 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest fixtures for EPA Orchestrator tests."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import epa_orchestrator.allocations_db as allocations_db_mod
import epa_orchestrator.cpu_pool as cpu_pool
import epa_orchestrator.daemon_handler as daemon_handler
import epa_orchestrator.hugepages_db as hugepages_db
import epa_orchestrator.state_store as state_store


@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture(scope="session", autouse=True)
def setup_env_and_reload_modules(tmp_path_factory):
    """Set SNAP_DATA to a temp dir and reload modules once per session.

    This ensures persistence writes/read go to an isolated directory while
    keeping imports at the top of the file.
    """
    temp_base = tmp_path_factory.mktemp("epa-state")
    os.environ["SNAP_DATA"] = str(temp_base)
    # Keep imported singleton references and exception identities intact.
    hugepages_db._store = state_store.StateStore()
    allocations_db_mod.allocations_db._state_store = state_store.StateStore()


@pytest.fixture(autouse=True)
def reset_persistent_state():
    """Reset both CPU and hugepages persistent state before and after each test."""
    try:
        hugepages_db.clear_all_allocations()
    except Exception:
        pass
    try:
        allocations_db_mod.allocations_db.clear_all_allocations()
    except Exception:
        pass
    yield
    try:
        hugepages_db.clear_all_allocations()
    except Exception:
        pass
    try:
        allocations_db_mod.allocations_db.clear_all_allocations()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_cpu_provider(tmp_path, monkeypatch):
    """Give legacy isolated-mode tests deterministic present/online topology."""
    present = tmp_path / "present"
    online = tmp_path / "online"
    present.write_text("0-511")
    online.write_text("0-511")
    monkeypatch.setattr(cpu_pool.cpu_pinning, "PRESENT_CPUS_PATH", str(present))
    monkeypatch.setattr(cpu_pool, "ONLINE_CPUS_PATH", str(online))
    monkeypatch.setattr(daemon_handler, "_default_pool_provider", None)


@pytest.fixture
def mock_cpu_files(temp_dir):
    """Create mock CPU system files for testing."""
    isolated_path = temp_dir / "isolated"
    present_path = temp_dir / "present"

    online_path = temp_dir / "online"
    # Create mock CPU files
    isolated_path.write_text("0-3,6-7")
    present_path.write_text("0-7")
    online_path.write_text("0-7")

    with (
        patch("epa_orchestrator.cpu_pinning.ISOLATED_CPUS_PATH", str(isolated_path)),
        patch("epa_orchestrator.cpu_pinning.PRESENT_CPUS_PATH", str(present_path)),
    ):
        with patch("epa_orchestrator.cpu_pool.ONLINE_CPUS_PATH", str(online_path)):
            yield {
                "online": online_path,
                "isolated": isolated_path,
                "present": present_path,
                "isolated_content": "0-3,6-7",
                "present_content": "0-7",
            }


@pytest.fixture
def mock_cpu_files_empty(temp_dir):
    """Create mock CPU system files with empty isolated CPUs."""
    isolated_path = temp_dir / "isolated"
    present_path = temp_dir / "present"

    online_path = temp_dir / "online"
    # Create mock CPU files with empty isolated
    isolated_path.write_text("")
    present_path.write_text("0-7")
    online_path.write_text("0-7")

    with (
        patch("epa_orchestrator.cpu_pinning.ISOLATED_CPUS_PATH", str(isolated_path)),
        patch("epa_orchestrator.cpu_pinning.PRESENT_CPUS_PATH", str(present_path)),
    ):
        with patch("epa_orchestrator.cpu_pool.ONLINE_CPUS_PATH", str(online_path)):
            yield {
                "online": online_path,
                "isolated": isolated_path,
                "present": present_path,
                "isolated_content": "",
                "present_content": "0-7",
            }


@pytest.fixture
def mock_cpu_files_missing(temp_dir):
    """Create mock CPU system files that don't exist."""
    isolated_path = temp_dir / "nonexistent_isolated"
    present_path = temp_dir / "nonexistent_present"

    with (
        patch("epa_orchestrator.cpu_pinning.ISOLATED_CPUS_PATH", str(isolated_path)),
        patch("epa_orchestrator.cpu_pinning.PRESENT_CPUS_PATH", str(present_path)),
    ):
        yield {"isolated": isolated_path, "present": present_path}


@pytest.fixture
def fresh_allocations_db():
    """Create a fresh AllocationsDB instance for testing."""
    return allocations_db_mod.AllocationsDB()


@pytest.fixture
def populated_allocations_db():
    """Create an AllocationsDB instance with some test allocations."""
    db = allocations_db_mod.AllocationsDB()
    db.allocate_cores("test-service-1", "0-1")
    db.allocate_cores("test-service-2", "2,4")
    return db


@pytest.fixture
def snap_env():
    """Mock snap environment variables."""
    return {
        "SNAP": "/snap/epa-orchestrator/1",
        "SNAP_COMMON": "/var/snap/epa-orchestrator/common",
        "SNAP_DATA": "/var/snap/epa-orchestrator/1",
        "SNAP_INSTANCE_NAME": "",
        "SERVICE_NAME": "epa-orchestrator",
        "SNAP_REVISION": "1",
        "SNAP_VERSION": "2025.1",
    }


@pytest.fixture
def mock_socket_path(temp_dir):
    """Create a mock socket path for testing."""
    socket_path = temp_dir / "epa_orchestrator.sock"
    return str(socket_path)


@pytest.fixture
def mock_logging():
    """Mock logging to capture log messages."""
    with patch("epa_orchestrator.cpu_pinning.logging") as mock_log:
        yield mock_log


@pytest.fixture
def sample_cpu_ranges():
    """Sample CPU range strings for testing."""
    return {
        "simple": "0-3",
        "disjoint": "0,2,4,6",
        "mixed": "0-2,4,6-8",
        "single": "5",
        "empty": "",
        "complex": "0-3,5,7-9,12,15-17",
    }


@pytest.fixture
def sample_cpu_lists():
    """Sample CPU number lists for testing."""
    return {
        "consecutive": [0, 1, 2, 3],
        "disjoint": [0, 2, 4, 6],
        "mixed": [0, 1, 2, 4, 6, 7, 8],
        "single": [5],
        "empty": [],
        "complex": [0, 1, 2, 3, 5, 7, 8, 9, 12, 15, 16, 17],
    }
