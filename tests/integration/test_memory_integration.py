# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for memory management functionality."""

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import parse_obj_as

import epa_orchestrator.hugepages_db as hugepages_db
from epa_orchestrator.daemon_handler import handle_daemon_request
from epa_orchestrator.schemas import (
    ActionType,
    AllocateHugepagesRequest,
    AllocateHugepagesResponse,
    ErrorResponse,
    GetMemoryInfoRequest,
    MemoryInfoResponse,
)


def _mk_memory_summary(free: int, total: int = 10, size_kb: int = 2048):
    """Return get_memory_summary-shaped dict for node0."""
    return {
        "numa_hugepages": {
            "node0": {
                "capacity": [{"total": total, "free": free, "size": size_kb}],
                "allocations": {},
            }
        }
    }


def _assert_hugepage_capacity(socket_path, node_id, size_kb, expected_total, expected_free):
    """Validate hugepage capacity for a node/size via GET_MEMORY_INFO."""
    mem_request = GetMemoryInfoRequest(service_name="probe", action=ActionType.GET_MEMORY_INFO)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(json.dumps(mem_request.model_dump()).encode())
        response_data = client.recv(4096)
    mem_response = parse_obj_as(MemoryInfoResponse, json.loads(response_data.decode()))
    node_key = f"node{node_id}"
    cap = next(c for c in mem_response.numa_hugepages[node_key].capacity if c.size == size_kb)
    assert cap.total == expected_total
    assert cap.free == expected_free


class TestMemoryIntegration:
    """Integration tests for memory management functionality."""

    @pytest.fixture(autouse=True)
    def clear_hugepages_db(self):
        """Clear hugepages DB before each test."""
        hugepages_db.clear_all_allocations()
        yield

    @pytest.fixture
    def socket_path(self, tmp_path):
        """Create a temporary socket path."""
        socket_dir = tmp_path / "data"
        socket_dir.mkdir()
        return str(socket_dir / "epa.sock")

    @pytest.fixture
    def memory_socket_daemon(self, socket_path, monkeypatch):
        """Start a socket-based daemon server with memory functionality."""
        monkeypatch.setenv("SNAP_DATA", str(Path(socket_path).parent.parent))

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(socket_path)
        server_sock.listen(5)

        def server_handler():
            """Handle daemon requests."""
            for _ in range(10):
                try:
                    conn, _ = server_sock.accept()
                except OSError:
                    break
                with conn:
                    data = conn.recv(1024)
                    if data:
                        response_bytes = handle_daemon_request(data)
                        conn.sendall(response_bytes)

        server_thread = threading.Thread(target=server_handler, daemon=True)
        server_thread.start()

        time.sleep(0.1)

        yield server_sock

        server_sock.close()
        if Path(socket_path).exists():
            Path(socket_path).unlink()

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_get_memory_info_via_socket(self, mock_summary, memory_socket_daemon, socket_path):
        """Test getting memory information through socket communication."""
        mock_summary.return_value = _mk_memory_summary(10)

        request = GetMemoryInfoRequest(
            service_name="test-service", action=ActionType.GET_MEMORY_INFO
        )

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.dict()).encode())
            response_data = client.recv(4096)

        response = parse_obj_as(MemoryInfoResponse, json.loads(response_data.decode()))
        assert response.service_name == "test-service"
        assert isinstance(response.numa_hugepages, dict)

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_via_socket(self, mock_summary, memory_socket_daemon, socket_path):
        """Test hugepage allocation through socket communication."""
        mock_summary.return_value = _mk_memory_summary(10)

        request = AllocateHugepagesRequest(
            service_name="test-service",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=2,
            node_id=0,
            size_kb=2048,
        )

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.dict()).encode())
            response_data = client.recv(4096)

        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.service_name == "test-service"
        assert response.allocation_successful is True
        assert response.hugepages_requested == 2
        assert response.node_id == 0
        assert response.size_kb == 2048

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_deallocate_via_socket(
        self, mock_summary, memory_socket_daemon, socket_path
    ):
        """Test hugepage deallocation (-1) through socket communication."""
        mock_summary.return_value = _mk_memory_summary(10)
        # Allocate first
        request = AllocateHugepagesRequest(
            service_name="service1",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=2,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.allocation_successful is True
        assert response.hugepages_requested == 2

        # Service1 allocates 4
        request = AllocateHugepagesRequest(
            service_name="service1",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=4,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.allocation_successful is True
        assert response.hugepages_requested == 4

        # Deallocate with -1
        request = AllocateHugepagesRequest(
            service_name="service1",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=-1,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.allocation_successful is True
        assert response.hugepages_requested == -1
        assert "Removed recorded" in response.message

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_multiple_services_via_socket(
        self, mock_summary, memory_socket_daemon, socket_path
    ):
        """Test multiple services allocating hugepages; third service gets insufficient error."""
        mock_summary.side_effect = [
            _mk_memory_summary(10),
            _mk_memory_summary(7),
            _mk_memory_summary(7),
            _mk_memory_summary(3),
            _mk_memory_summary(3),
        ]
        # Service1 allocates 3
        request = AllocateHugepagesRequest(
            service_name="service1",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=3,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.allocation_successful is True
        assert response.hugepages_requested == 3
        _assert_hugepage_capacity(
            socket_path, node_id=0, size_kb=2048, expected_total=10, expected_free=7
        )

        # Service2 allocates 4
        request = AllocateHugepagesRequest(
            service_name="service2",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=4,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.allocation_successful is True
        assert response.hugepages_requested == 4
        _assert_hugepage_capacity(
            socket_path, node_id=0, size_kb=2048, expected_total=10, expected_free=3
        )

        # Service3 requests 5 when only 3 free -> insufficient
        request = AllocateHugepagesRequest(
            service_name="service3",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=5,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(ErrorResponse, json.loads(response_data.decode()))
        assert "only has 3 free hugepages" in response.error
        assert "requested additional 5" in response.error

        # Deallocate service1
        request = AllocateHugepagesRequest(
            service_name="service1",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=-1,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(AllocateHugepagesResponse, json.loads(response_data.decode()))
        assert response.allocation_successful is True
        assert response.hugepages_requested == -1

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_insufficient_capacity_via_socket(
        self, mock_summary, memory_socket_daemon, socket_path
    ):
        """Test error when requesting more hugepages than available free."""
        mock_summary.return_value = _mk_memory_summary(2)

        request = AllocateHugepagesRequest(
            service_name="service1",
            action=ActionType.ALLOCATE_HUGEPAGES,
            hugepages_requested=5,
            node_id=0,
            size_kb=2048,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.model_dump()).encode())
            response_data = client.recv(4096)
        response = parse_obj_as(ErrorResponse, json.loads(response_data.decode()))
        assert "only has 2 free hugepages" in response.error
        assert "requested additional 5" in response.error

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_memory_info_response_structure(self, mock_summary, memory_socket_daemon, socket_path):
        """Test that memory info response has correct structure."""
        mock_summary.return_value = _mk_memory_summary(10)

        request = GetMemoryInfoRequest(
            service_name="test-service", action=ActionType.GET_MEMORY_INFO
        )

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps(request.dict()).encode())
            response_data = client.recv(4096)

        response = parse_obj_as(MemoryInfoResponse, json.loads(response_data.decode()))

        assert hasattr(response, "version")
        assert hasattr(response, "service_name")
        assert hasattr(response, "numa_hugepages")
        assert response.version == "1.0"
        assert response.service_name == "test-service"
        assert isinstance(response.numa_hugepages, dict)


@pytest.mark.parametrize(
    "service,requested",
    [("new-owner", 2), ("memory-owner", 2), ("memory-owner", -1), ("missing-owner", -1)],
)
def test_hugepage_api_honors_cpu_uncertainty_latch(monkeypatch, service, requested):
    """CPU commit uncertainty rejects hugepage requests through reload and explicit recovery."""
    from epa_orchestrator.allocations_db import allocations_db
    from epa_orchestrator.state_store import StateStore, StateUncertainError

    hugepages_db.upsert_allocation("memory-owner", 0, 2048, 10)
    before = hugepages_db.list_allocations()
    allocations_db.allocate_cores("cpu-owner", "0", "non-preemptive")
    sync = StateStore._sync_directory
    calls = 0

    def fail(self):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("uncertain CPU commit")
        sync(self)

    payload = {
        "action": "allocate_hugepages",
        "service_name": service,
        "node_id": 0,
        "size_kb": 2048,
        "hugepages_requested": requested,
    }
    monkeypatch.setattr(
        "epa_orchestrator.daemon_handler.get_memory_summary", lambda: _mk_memory_summary(100)
    )
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(StateStore, "_sync_directory", fail)
            with pytest.raises(StateUncertainError):
                allocations_db.allocate_cores("cpu-owner", "0-1")
        for _ in range(2):
            result = json.loads(handle_daemon_request(json.dumps(payload).encode()))
            assert "uncertain" in result["error"]
            assert "allocation_successful" not in result
            assert hugepages_db._allocations == before
            assert StateStore().read_section("hugepages_db")["allocations"] == before
            # A fresh store and the on-disk marker alone must still reject mutations.
            StateStore._uncertain_paths.discard(hugepages_db._store._lock_path)
            monkeypatch.setattr(hugepages_db, "_store", StateStore())
            hugepages_db._load_from_store()
    finally:
        allocations_db._state_store.recover()
    result = json.loads(handle_daemon_request(json.dumps(payload).encode()))
    assert result["allocation_successful"] is True
    hugepages_db._load_from_store()
    assert hugepages_db.get_allocation(service) == (
        None if requested == -1 else [{"node_id": 0, "size_kb": 2048, "count": 2}]
    )
    if service != "memory-owner":
        assert hugepages_db.get_allocation("memory-owner") == before["memory-owner"]
    assert allocations_db.get_allocation("cpu-owner") == "0-1"
