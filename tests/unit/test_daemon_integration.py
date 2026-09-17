# SPDX-FileCopyrightText: 2024 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Concise unit tests for daemon integration functionality."""

import json
from unittest.mock import patch

import pytest
from pydantic import parse_obj_as

from epa_orchestrator.allocations_db import allocations_db
from epa_orchestrator.cpu_pinning import calculate_cpu_pinning, get_isolated_cpus
from epa_orchestrator.daemon_handler import (
    handle_allocate_cores,
    handle_allocate_cores_percent,
    handle_allocate_numa_cores,
    handle_daemon_request,
    handle_list_allocations,
)
from epa_orchestrator.hugepages_db import list_allocations_for_node
from epa_orchestrator.schemas import (
    ActionType,
    AllocateCoresPercentRequest,
    AllocateCoresPercentResponse,
    AllocateCoresRequest,
    AllocateCoresResponse,
    AllocateHugepagesResponse,
    AllocateNumaCoresRequest,
    ErrorResponse,
    ListAllocationsRequest,
)
from epa_orchestrator.utils import parse_cpu_ranges


def _list_allocations() -> dict:
    """Helper: call list_allocations and return dict form."""
    resp = handle_list_allocations(
        ListAllocationsRequest(service_name="probe", action=ActionType.LIST_ALLOCATIONS)
    )
    return resp.dict()


def _get_service_entry(service_name: str) -> dict | None:
    """Helper: return allocation entry for a given service if present."""
    data = _list_allocations()
    allocations = data.get("allocations", [])
    return next((e for e in allocations if e.get("service_name") == service_name), None)


class TestDaemonIntegration:
    """Unit tests for daemon integration logic."""

    def test_allocate_cores_request(self, fresh_allocations_db, mock_cpu_files):
        """Test allocation of cores via daemon request."""
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-3,6-7"):
            request = AllocateCoresRequest(
                service_name="service1", action=ActionType.ALLOCATE_CORES, num_of_cores=2
            )
            isolated = get_isolated_cpus()
            shared, allocated = calculate_cpu_pinning(isolated, request.num_of_cores)
            fresh_allocations_db.allocate_cores(request.service_name, allocated)
            stats = fresh_allocations_db.get_system_stats(isolated)
            response = AllocateCoresResponse(
                version="1.0",
                service_name="service1",
                num_of_cores=request.num_of_cores,
                cores_allocated=len(parse_cpu_ranges(allocated)),
                allocated_cores=allocated,
                shared_cpus=shared,
                total_available_cpus=stats["total_available_cpus"],
                remaining_available_cpus=stats["remaining_available_cpus"],
            )
            assert response.service_name == "service1"
            assert response.cores_allocated == 2

    def test_error_handling(self):
        """Test error handling in daemon integration."""
        with pytest.raises(Exception):
            AllocateCoresRequest(service_name="service1", action="bad_action", num_of_cores=2)

    def test_allocate_cores_no_isolated_cpus(self):
        """Test error response when no isolated CPUs are configured in daemon handler."""
        with patch(
            "epa_orchestrator.cpu_pinning.get_isolated_cpus",
            return_value="",
        ):
            request = {
                "version": "1.0",
                "service_name": "service1",
                "action": "allocate_cores",
                "num_of_cores": 2,
            }
            response_bytes = handle_daemon_request(bytes(str(request).replace("'", '"'), "utf-8"))
            resp = parse_obj_as(ErrorResponse, json.loads(response_bytes.decode()))
            assert resp.error == "No CPUs available"

    def test_allocate_numa_cores_no_isolated_cpus(self):
        """Test error when no isolated CPUs are available for NUMA allocation."""
        request = {
            "version": "1.0",
            "service_name": "service1",
            "action": "allocate_numa_cores",
            "numa_node": 0,
            "num_of_cores": 2,
        }
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value=""):
            response_bytes = handle_daemon_request(json.dumps(request).encode())
            resp = parse_obj_as(ErrorResponse, json.loads(response_bytes.decode()))
            assert resp.error == "No CPUs available"

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_track_positive(self, mock_summary):
        """Test hugepages recording (>0) with sufficient capacity present."""
        mock_summary.return_value = {
            "numa_hugepages": {
                "node0": {"capacity": [{"total": 10, "free": 10, "size": 2048}], "allocations": {}}
            }
        }
        request = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 3,
            "node_id": 0,
            "size_kb": 2048,
        }
        response_bytes = handle_daemon_request(json.dumps(request).encode())
        resp = parse_obj_as(AllocateHugepagesResponse, json.loads(response_bytes.decode()))
        assert resp.allocation_successful is True
        assert resp.hugepages_requested == 3
        assert resp.node_id == 0
        assert resp.size_kb == 2048

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_deallocate_minus_one(self, mock_summary):
        """-1 should remove record and succeed with message."""
        mock_summary.return_value = {
            "numa_hugepages": {
                "node1": {"capacity": [{"total": 10, "free": 10, "size": 2048}], "allocations": {}}
            }
        }
        # First add a record, then deallocate
        add_req = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 2,
            "node_id": 1,
            "size_kb": 2048,
        }
        _ = handle_daemon_request(json.dumps(add_req).encode())

        del_req = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": -1,
            "node_id": 1,
            "size_kb": 2048,
        }
        response_bytes = handle_daemon_request(json.dumps(del_req).encode())
        resp = parse_obj_as(AllocateHugepagesResponse, json.loads(response_bytes.decode()))
        assert resp.allocation_successful is True
        assert resp.hugepages_requested == -1
        assert resp.node_id == 1
        assert resp.size_kb == 2048
        assert resp.message.startswith("Removed recorded")

    def test_allocate_hugepages_deallocate_minus_one_noop(self):
        """-1 on a missing record returns a specific noop message."""
        del_req = {
            "version": "1.0",
            "service_name": "svc-missing",
            "action": "allocate_hugepages",
            "hugepages_requested": -1,
            "node_id": 1,
            "size_kb": 2048,
        }
        response_bytes = handle_daemon_request(json.dumps(del_req).encode())
        resp = parse_obj_as(AllocateHugepagesResponse, json.loads(response_bytes.decode()))
        assert resp.allocation_successful is True
        assert resp.hugepages_requested == -1
        assert resp.node_id == 1
        assert resp.size_kb == 2048
        assert resp.message.startswith("No existing record")

    def test_allocate_hugepages_zero_invalid(self):
        """0 is invalid and should return an ErrorResponse."""
        bad_req = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 0,
            "node_id": 0,
            "size_kb": 2048,
        }
        response_bytes = handle_daemon_request(json.dumps(bad_req).encode())
        resp = parse_obj_as(ErrorResponse, json.loads(response_bytes.decode()))
        assert "hugepages_requested=0 is invalid" in resp.error

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_hp_capacity_node_missing(self, mock_summary):
        """Error when NUMA node is not found in memory summary."""
        mock_summary.return_value = {"numa_hugepages": {}}
        req = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 2,
            "node_id": 9,
            "size_kb": 2048,
        }
        resp_b = handle_daemon_request(json.dumps(req).encode())
        resp = parse_obj_as(ErrorResponse, json.loads(resp_b.decode()))
        assert resp.error == "NUMA node 9 not found"

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_hp_capacity_size_missing(self, mock_summary):
        """Error when hugepage size is not available on the node."""
        mock_summary.return_value = {
            "numa_hugepages": {
                "node0": {"capacity": [{"total": 10, "free": 10, "size": 4096}], "allocations": {}}
            }
        }
        req = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 2,
            "node_id": 0,
            "size_kb": 2048,
        }
        resp_b = handle_daemon_request(json.dumps(req).encode())
        resp = parse_obj_as(ErrorResponse, json.loads(resp_b.decode()))
        assert resp.error == "Hugepage size 2048 KB not found on node 0"

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_hp_capacity_insufficient_free(self, mock_summary):
        """Error when free hugepages are fewer than requested."""
        mock_summary.return_value = {
            "numa_hugepages": {
                "node0": {"capacity": [{"total": 10, "free": 1, "size": 2048}], "allocations": {}}
            }
        }
        req = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 3,
            "node_id": 0,
            "size_kb": 2048,
        }
        resp_b = handle_daemon_request(json.dumps(req).encode())
        resp = parse_obj_as(ErrorResponse, json.loads(resp_b.decode()))
        assert (
            resp.error
            == "NUMA node 0 size 2048 KB only has 1 free hugepages, requested additional 3"
        )

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_hp_capacity_success(self, mock_summary):
        """Success when sufficient free hugepages are available on node/size."""
        mock_summary.return_value = {
            "numa_hugepages": {
                "node0": {"capacity": [{"total": 10, "free": 5, "size": 2048}], "allocations": {}}
            }
        }
        req1 = {
            "version": "1.0",
            "service_name": "svc",
            "action": "allocate_hugepages",
            "hugepages_requested": 2,
            "node_id": 0,
            "size_kb": 2048,
        }
        _ = handle_daemon_request(json.dumps(req1).encode())
        # Increase mocked free capacity for the second request so it passes capacity checks
        mock_summary.return_value = {
            "numa_hugepages": {
                "node0": {"capacity": [{"total": 10, "free": 10, "size": 2048}], "allocations": {}}
            }
        }
        req2 = dict(req1)
        req2["hugepages_requested"] = 7
        resp_bytes = handle_daemon_request(json.dumps(req2).encode())
        resp2 = parse_obj_as(AllocateHugepagesResponse, json.loads(resp_bytes.decode()))
        assert resp2.allocation_successful is True
        assert resp2.hugepages_requested == 7

        # After replacement, node0 should show only the latest count for svc
        flattened = list_allocations_for_node(0)
        # Expect svc to be present with size 2048 and count 7
        assert any(
            e["service_name"] == "svc" and e["size_kb"] == 2048 and e["count"] == 7
            for e in flattened
        )
        # Ensure no duplicate entry for svc/2048 remains
        counts = [
            e["count"] for e in flattened if e["service_name"] == "svc" and e["size_kb"] == 2048
        ]
        assert counts.count(7) == 1

    def test_allocate_cores_num_of_cores_zero_after_existing_allocation(self):
        """allocate_cores(num_of_cores=0) must use full pool when re-allocating."""
        allocations_db.clear_all_allocations()
        isolated = "96-127,224-255,352-383,480-511"  # 128 cores
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value=isolated):
            # Simulate prior NUMA allocation: service already has 112 cores
            allocations_db.allocate_cores("openstack-hypervisor", "96-127,224-255,352-383,480-495")
            r = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="openstack-hypervisor",
                    action=ActionType.ALLOCATE_CORES,
                    num_of_cores=0,
                )
            )
            assert r.cores_allocated == 112
            assert r.allocated_cores == "96-127,224-255,352-383,480-495"
            assert r.shared_cpus == "496-511"

    def test_allocate_cores_percent_fifty_percent(self):
        """Allocate 50% of isolated cores; 50% of 8 cores = 4 cores."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-7"):
            req = AllocateCoresPercentRequest(
                service_name="service1",
                action=ActionType.ALLOCATE_CORES_PERCENT,
                percent=50,
            )
            r = handle_allocate_cores_percent(req)
            assert r.cores_allocated_count == 4

    def test_allocate_cores_percent_deallocate(self):
        """percent=-1 deallocates the service's cores."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5"):
            r1 = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=50,
                )
            )
            assert r1.cores_allocated_count == 3
            r2 = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=-1,
                )
            )
            assert r2.cores_allocated_count == 0
            assert allocations_db.get_allocation("service1") is None

    def test_allocate_cores_percent_via_daemon_request(self):
        """allocate_cores_percent via handle_daemon_request JSON."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-9"):
            req = {
                "version": "1.0",
                "service_name": "service1",
                "action": "allocate_cores_percent",
                "percent": 30,
            }
            resp_bytes = handle_daemon_request(json.dumps(req).encode())
            resp = parse_obj_as(AllocateCoresPercentResponse, json.loads(resp_bytes.decode()))
            assert resp.service_name == "service1"
            assert resp.cores_allocated_count == 3  # 30% of 10 = 3

    def test_allocate_cores_percent_ceil_rounding(self):
        """Computed core count uses ceil; 7 cores * 25% = 1.75 -> 2. Avoids num_of_cores=0 path."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-6"):
            r = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=25,
                )
            )
            assert r.cores_allocated_count == 2

    def test_allocate_cores_percent_small_percent_yields_one(self):
        """Validate that a small percentage of cores yields at least one core."""
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-7"):
            r = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=1,
                )
            )
            assert r.cores_allocated_count == 1

    def test_allocate_cores_percent_zero_and_one(self):
        """percent=0 treated as 0 cores (deallocate); percent=1 with 100 cores gives 1."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-99"):
            req = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=0,
                )
            )
            assert req.cores_allocated_count == 0
            assert req.allocated_cores == ""
            assert req.total_available_cpus == 100
            assert req.remaining_available_cpus == 100

            # 1% of 100 cores = 1
            req = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=1,
                )
            )
            assert req.cores_allocated_count == 1
            assert req.allocated_cores == "0"
            assert req.total_available_cpus == 100
            assert req.remaining_available_cpus == 99

            # 10% of 100 cores = 10 cores
            req = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=10,
                )
            )
            assert req.cores_allocated_count == 10
            assert req.allocated_cores == "0-9"
            assert req.total_available_cpus == 100
            assert req.remaining_available_cpus == 90

            req = handle_allocate_cores_percent(
                AllocateCoresPercentRequest(
                    service_name="service1",
                    action=ActionType.ALLOCATE_CORES_PERCENT,
                    percent=-1,
                )
            )
            assert req.cores_allocated_count == 0
            assert req.allocated_cores == ""
            assert req.total_available_cpus == 100
            assert req.remaining_available_cpus == 100

    def test_allocate_cores_percent_no_isolated_cpus(self):
        """allocate_cores_percent returns error when no isolated CPUs."""
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value=""):
            req = {
                "version": "1.0",
                "service_name": "service1",
                "action": "allocate_cores_percent",
                "percent": 50,
            }
            resp_bytes = handle_daemon_request(json.dumps(req).encode())
            resp = parse_obj_as(ErrorResponse, json.loads(resp_bytes.decode()))
            assert resp.error == "No CPUs available"

    def test_allocate_cores_valid_override_and_second_service(self):
        """Allocate cores, then override; add second service within remaining."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5"):
            r1 = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc-a-core", action=ActionType.ALLOCATE_CORES, num_of_cores=1
                )
            )
            assert r1.cores_allocated == 1
            a1 = _get_service_entry("svc-a-core")
            assert a1 and int(a1["cores_count"]) == 1 and a1["is_explicit"] is False

            r2 = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc-a-core", action=ActionType.ALLOCATE_CORES, num_of_cores=2
                )
            )
            assert r2.cores_allocated == 2
            a2 = _get_service_entry("svc-a-core")
            assert a2 and int(a2["cores_count"]) == 2

            r3 = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc-b-core", action=ActionType.ALLOCATE_CORES, num_of_cores=1
                )
            )
            assert r3.cores_allocated == 1
            b = _get_service_entry("svc-b-core")
            assert b and int(b["cores_count"]) == 1

    def test_allocate_cores_out_of_bound_and_invalid_param(self):
        """Out-of-bound request errors; numa_node param rejected for allocate_cores."""
        allocations_db.clear_all_allocations()
        with patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5"):
            _ = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc-a-core", action=ActionType.ALLOCATE_CORES, num_of_cores=5
                )
            )
            with pytest.raises(ValueError) as ei:
                _ = handle_allocate_cores(
                    AllocateCoresRequest(
                        service_name="svc-c-core", action=ActionType.ALLOCATE_CORES, num_of_cores=2
                    )
                )
            assert "Insufficient CPUs available" in str(ei.value)

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5")
    @patch(
        "epa_orchestrator.utils.get_numa_node_cpus",
        return_value={0: {0, 1, 2}, 1: {3, 4, 5}},
    )
    def test_allocate_numa_valid_override_and_second_service(self, mock_nodes, mock_iso_cp):
        """Allocate in node, override count; second service consumes same node."""
        allocations_db.clear_all_allocations()
        r1 = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc-a-numa",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=1,
            )
        )
        assert r1.cores_allocated != ""
        a1 = _get_service_entry("svc-a-numa")
        assert a1 and a1["is_explicit"] is True and int(a1["cores_count"]) >= 1

        r2 = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc-a-numa",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=2,
            )
        )
        assert r2.cores_allocated != ""
        a2 = _get_service_entry("svc-a-numa")
        assert a2 and a2["is_explicit"] is True and int(a2["cores_count"]) >= 2

        r3 = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc-b-numa",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=1,
            )
        )
        assert r3.cores_allocated != ""

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5")
    @patch(
        "epa_orchestrator.utils.get_numa_node_cpus",
        return_value={0: {0, 1, 2}, 1: {3, 4, 5}},
    )
    def test_allocate_numa_overask_and_zero_invalid(self, mock_nodes, mock_iso_dh):
        """Over-ask triggers error; num_of_cores=0 invalid for NUMA."""
        allocations_db.clear_all_allocations()
        with pytest.raises(ValueError) as ei:
            _ = handle_allocate_numa_cores(
                AllocateNumaCoresRequest(
                    service_name="svc-c-numa",
                    action=ActionType.ALLOCATE_NUMA_CORES,
                    numa_node=0,
                    num_of_cores=9999,
                )
            )
        assert "only has" in str(ei.value)

        with pytest.raises(ValueError) as ei2:
            _ = handle_allocate_numa_cores(
                AllocateNumaCoresRequest(
                    service_name="svc-x",
                    action=ActionType.ALLOCATE_NUMA_CORES,
                    numa_node=0,
                    num_of_cores=0,
                )
            )
        assert "num_of_cores=0 is invalid" in str(ei2.value)

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5")
    @patch(
        "epa_orchestrator.daemon_handler.get_numa_node_cpus",
        return_value={1: {3, 4, 5}},
    )
    def test_allocate_numa_nonexistent_node(self, mock_nodes, mock_iso_cp):
        """Requesting a NUMA node not present in topology raises error."""
        allocations_db.clear_all_allocations()
        with pytest.raises(ValueError) as ei:
            _ = handle_allocate_numa_cores(
                AllocateNumaCoresRequest(
                    service_name="svc-x",
                    action=ActionType.ALLOCATE_NUMA_CORES,
                    numa_node=0,
                    num_of_cores=1,
                )
            )
        assert "NUMA node 0 does not exist" in str(ei.value)

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5")
    def test_allocate_numa_no_topology_error_response(self, mock_iso_dh):
        """When topology lookup raises, daemon request returns ErrorResponse with message."""
        req = {
            "version": "1.0",
            "action": "allocate_numa_cores",
            "service_name": "svc-x",
            "numa_node": 0,
            "num_of_cores": 1,
        }
        with patch(
            "epa_orchestrator.utils.get_numa_node_cpus",
            side_effect=ValueError("NUMA topology not available"),
        ):
            resp_b = handle_daemon_request(json.dumps(req).encode())
            resp = parse_obj_as(ErrorResponse, json.loads(resp_b.decode()))
            assert resp.error == "NUMA topology not available"

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5")
    @patch(
        "epa_orchestrator.utils.get_numa_node_cpus",
        return_value={0: {0, 1, 2}, 1: {3, 4, 5}},
    )
    def test_allocate_numa_deallocate_path(self, mock_nodes, mock_iso_cp):
        """Deallocate path clears service's allocation in the specified node."""
        allocations_db.clear_all_allocations()
        _ = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc-a-numa",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=2,
            )
        )
        with pytest.raises(ValueError):
            _ = handle_allocate_numa_cores(
                AllocateNumaCoresRequest(
                    service_name="svc-b-numa",
                    action=ActionType.ALLOCATE_NUMA_CORES,
                    numa_node=0,
                    num_of_cores=2,
                )
            )

        assert allocations_db.get_allocation("svc-a-numa") == "0-1"

        r = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc-a-numa",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=-1,
            )
        )
        _ = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc-b-numa",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=2,
            )
        )
        assert r.cores_allocated == ""
        a = _get_service_entry("svc-a-numa")
        assert a is None or int(a.get("cores_count", 0)) == 0
        assert allocations_db.get_allocation("svc-b-numa") == "0-1"
        assert not allocations_db.get_allocation("svc-a-numa")

    @patch("epa_orchestrator.allocations_db.calculate_cpu_pinning", return_value=("", ""))
    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-3")
    def test_allocate_cores_pinning_failure(self, mock_iso_cp, mock_calc):
        """If pinning yields no dedicated CPUs, handler raises ValueError."""
        allocations_db.clear_all_allocations()
        with pytest.raises(ValueError) as ei:
            _ = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc-pin-fail",
                    action=ActionType.ALLOCATE_CORES,
                    num_of_cores=2,
                )
            )
        assert "Failed to allocate 2 cores" in str(ei.value)

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-3")
    def test_allocate_cores_negative_request(self, mock_iso_cp):
        """Non-NUMA deallocation (-1) clears service allocation and frees CPUs for others.

        Flow:
          - Allocate 2 cores to svc-a.
          - svc-b requesting 3 cores should fail (insufficient free).
          - Deallocate svc-a with -1.
          - svc-b requesting 2 cores should now succeed.
        """
        allocations_db.clear_all_allocations()

        # Allocate two cores to service A
        r1 = handle_allocate_cores(
            AllocateCoresRequest(
                service_name="svc-a-core",
                action=ActionType.ALLOCATE_CORES,
                num_of_cores=2,
            )
        )
        assert r1.cores_allocated == 2

        # Service B over-asks given remaining free CPUs -> expect failure
        with pytest.raises(ValueError):
            _ = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc-b-core",
                    action=ActionType.ALLOCATE_CORES,
                    num_of_cores=3,
                )
            )

        # Deallocate service A with -1
        r2 = handle_allocate_cores(
            AllocateCoresRequest(
                service_name="svc-a-core",
                action=ActionType.ALLOCATE_CORES,
                num_of_cores=-1,
            )
        )
        assert r2.cores_allocated == 0
        assert allocations_db.get_allocation("svc-a-core") is None

        # Now service B should succeed within available free CPUs
        r3 = handle_allocate_cores(
            AllocateCoresRequest(
                service_name="svc-b-core",
                action=ActionType.ALLOCATE_CORES,
                num_of_cores=2,
            )
        )
        assert r3.cores_allocated == 2
        assert allocations_db.get_snap_allocation_count("svc-b-core") == 2

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-3")
    def test_core_allocation_exclusive(self, mock_iso_cp):
        """Core allocation must be exclusive across services."""
        allocations_db.clear_all_allocations()

        r1 = handle_allocate_cores(
            AllocateCoresRequest(
                service_name="svc_a", action=ActionType.ALLOCATE_CORES, num_of_cores=1
            )
        )
        r2 = handle_allocate_cores(
            AllocateCoresRequest(
                service_name="svc_b", action=ActionType.ALLOCATE_CORES, num_of_cores=2
            )
        )
        assert r1.cores_allocated == 1
        assert r2.cores_allocated == 2

        # Third request should fail as svc_c overlaps with svc_b
        with pytest.raises(ValueError):
            _ = handle_allocate_cores(
                AllocateCoresRequest(
                    service_name="svc_c",
                    action=ActionType.ALLOCATE_CORES,
                    num_of_cores=2,
                )
            )

        assert allocations_db.get_allocation("svc_a") == "0"
        assert allocations_db.get_allocation("svc_b") == "1-2"
        assert "svc_c" not in allocations_db.get_all_allocations()

        # Fourth request should succeed as svc_c does not overlap with svc_b
        r3 = handle_allocate_cores(
            AllocateCoresRequest(
                service_name="svc_c",
                action=ActionType.ALLOCATE_CORES,
                num_of_cores=1,
            )
        )
        assert r3.cores_allocated == 1
        assert allocations_db.get_allocation("svc_c") == "3"

    @patch("epa_orchestrator.utils.get_numa_node_cpus", return_value={0: {0, 1, 2}, 1: {3, 4, 5}})
    @patch(
        "epa_orchestrator.daemon_handler.get_numa_node_cpus",
        return_value={0: {0, 1, 2}, 1: {3, 4, 5}},
    )
    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-5")
    def test_numa_allocation_exclusive(self, mock_iso_cp, mock_nodes_dh, mock_nodes_utils):
        """Numa allocation must be exclusive across services."""
        allocations_db.clear_all_allocations()

        r1 = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc_a",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=1,
            )
        )

        r2 = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc_b",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=0,
                num_of_cores=2,
            )
        )

        assert r1.cores_allocated == "0"
        assert r2.cores_allocated == "1-2"

        # Third request should fail as svc_c overlaps with svc_b
        with pytest.raises(ValueError):
            _ = handle_allocate_numa_cores(
                AllocateNumaCoresRequest(
                    service_name="svc_c",
                    action=ActionType.ALLOCATE_NUMA_CORES,
                    numa_node=0,
                    num_of_cores=2,
                )
            )

        assert allocations_db.get_allocation("svc_a") == "0"
        assert allocations_db.get_allocation("svc_b") == "1-2"
        assert r1.cores_allocated == "0"
        assert r2.cores_allocated == "1-2"
        assert "svc_c" not in allocations_db.get_all_allocations()

        # Fourth request should succeed as svc_c does not overlap with svc_b
        r3 = handle_allocate_numa_cores(
            AllocateNumaCoresRequest(
                service_name="svc_c",
                action=ActionType.ALLOCATE_NUMA_CORES,
                numa_node=1,
                num_of_cores=2,
            )
        )
        assert r3.cores_allocated == "3-4"
        assert allocations_db.get_allocation("svc_c") == "3-4"

    @patch("epa_orchestrator.daemon_handler.get_memory_summary")
    def test_allocate_hugepages_delta_validation(self, mock_summary):
        """Replacing an existing hugepage request should validate by delta, not sum.

        Flow:
          - First request: set to 7 (free is ample) -> success.
          - Second request: set to 5 with free=0 -> success (delta -2, allowed).
          - Third request: set back to 7 with free=2 -> success (delta +2 == free).
          - Fourth request: set to 11 with free=1 -> error (delta +4 > free).
        """
        # Sequence of capacity snapshots per call
        mock_summary.side_effect = [
            # 1) Free capacity is 10
            {
                "numa_hugepages": {
                    "node0": {
                        "capacity": [{"total": 10, "free": 10, "size": 2048}],
                        "allocations": {},
                    }
                }
            },
            # 2) Free capacity is 0 (still allow reduction)
            {
                "numa_hugepages": {
                    "node0": {
                        "capacity": [{"total": 10, "free": 0, "size": 2048}],
                        "allocations": {},
                    }
                }
            },
            # 3) Free capacity is 2 (increase by +2 should succeed)
            {
                "numa_hugepages": {
                    "node0": {
                        "capacity": [{"total": 10, "free": 2, "size": 2048}],
                        "allocations": {},
                    }
                }
            },
            # 4) Free capacity is 1 (increase by +4 should fail)
            {
                "numa_hugepages": {
                    "node0": {
                        "capacity": [{"total": 10, "free": 1, "size": 2048}],
                        "allocations": {},
                    }
                }
            },
        ]

        # First set request to 7
        req1 = {
            "version": "1.0",
            "service_name": "svc-delta",
            "action": "allocate_hugepages",
            "hugepages_requested": 7,
            "node_id": 0,
            "size_kb": 2048,
        }
        resp1_b = handle_daemon_request(json.dumps(req1).encode())
        resp1 = parse_obj_as(AllocateHugepagesResponse, json.loads(resp1_b.decode()))
        assert resp1.allocation_successful is True
        assert resp1.hugepages_requested == 7

        # Now replace with 5 while free=0 (delta -2): should succeed
        req2 = dict(req1)
        req2["hugepages_requested"] = 5
        resp2_b = handle_daemon_request(json.dumps(req2).encode())
        resp2 = parse_obj_as(AllocateHugepagesResponse, json.loads(resp2_b.decode()))
        assert resp2.allocation_successful is True
        assert resp2.hugepages_requested == 5

        # Increase back to 7 while free=2 (delta +2 == free): should succeed
        req3 = dict(req1)
        req3["hugepages_requested"] = 7
        resp3_b = handle_daemon_request(json.dumps(req3).encode())
        resp3 = parse_obj_as(AllocateHugepagesResponse, json.loads(resp3_b.decode()))
        assert resp3.allocation_successful is True
        assert resp3.hugepages_requested == 7

        # Now over-ask: set to 11 while free=1 (delta +4 > free): should error
        req4 = dict(req1)
        req4["hugepages_requested"] = 11
        resp4_b = handle_daemon_request(json.dumps(req4).encode())
        err4 = parse_obj_as(ErrorResponse, json.loads(resp4_b.decode()))
        assert "requested additional" in err4.error

    @patch("epa_orchestrator.cpu_pinning.get_isolated_cpus", return_value="0-7")
    @patch("epa_orchestrator.utils.get_numa_node_cpus", return_value={0: {0, 1, 2, 3, 4, 5, 6, 7}})
    @patch(
        "epa_orchestrator.daemon_handler.get_numa_node_cpus",
        return_value={0: {0, 1, 2, 3, 4, 5, 6, 7}},
    )
    def test_numa_smt_prefers_pairs_then_singles(
        self, mock_nodes_dh, mock_nodes_utils, mock_iso_cp
    ):
        """NUMA allocator should prefer full sibling pairs, then fill with singles (no blocking).

        Scenario: Node0 has CPUs 0-7 forming pairs (0,1), (2,3), (4,5), (6,7).
        Request 5 CPUs -> expect two full pairs and one single from a third pair.
        """
        allocations_db.clear_all_allocations()

        # Patch thread siblings mapping via cpu_pinning helper
        def fake_read_file(path: str) -> str:
            # Extract cpu number from path like .../cpuN/topology/thread_siblings_list
            try:
                i = path.rfind("/cpu")
                j = path.find("/", i + 4)
                cpu_str = path[i + 4 : j]
                n = int(cpu_str)
                base = n - (n % 2)
                return f"{base}-{base + 1}"
            except Exception:
                return ""

        with patch("epa_orchestrator.cpu_pinning._read_file_strict", side_effect=fake_read_file):
            r = handle_allocate_numa_cores(
                AllocateNumaCoresRequest(
                    service_name="svc-smt",
                    action=ActionType.ALLOCATE_NUMA_CORES,
                    numa_node=0,
                    num_of_cores=5,
                )
            )

        # Validate allocation count
        allocated_set = parse_cpu_ranges(r.cores_allocated)
        assert len(allocated_set) == 5

        # Validate that at least two groups are fully used (pairs), and one single remains
        groups = [(0, 1), (2, 3), (4, 5), (6, 7)]
        counts = []
        for a, b in groups:
            c = (1 if a in allocated_set else 0) + (1 if b in allocated_set else 0)
            counts.append(c)
        assert counts.count(2) >= 2  # two full cores used
        assert counts.count(1) >= 1  # one single used


@pytest.fixture
def policy_api(monkeypatch):
    """Provide a deterministic pool for daemon protocol regressions."""
    monkeypatch.setattr("epa_orchestrator.cpu_pinning.get_isolated_cpus", lambda: "0-7")

    def topology():
        return {0: set(range(4)), 1: set(range(4, 8))}

    monkeypatch.setattr("epa_orchestrator.utils.get_numa_node_cpus", topology)
    monkeypatch.setattr("epa_orchestrator.daemon_handler.get_numa_node_cpus", topology)

    def request(action, service="a", **fields):
        return json.loads(
            handle_daemon_request(
                json.dumps(
                    {"version": "1.0", "action": action, "service_name": service, **fields}
                ).encode()
            )
        )

    return request


@pytest.mark.parametrize(
    "action,fields",
    [
        ("allocate_cores", {"num_of_cores": 8}),
        ("allocate_cores_percent", {"percent": 100}),
        ("allocate_numa_cores", {"numa_node": 0, "num_of_cores": 4}),
    ],
)
def test_policy_protocol_protection_and_confirmation(policy_api, action, fields):
    """All CPU APIs confirm protection and prevent a legacy NUMA requester stealing it."""
    assert "non-preemptive-allocations" in policy_api("list_allocations")["supported_cpu_features"]
    result = policy_api(action, preemption_policy="non-preemptive", **fields)
    assert "error" not in result
    assert result["preemption_policy"] == "non-preemptive"
    before = policy_api("list_allocations")
    conflict = policy_api("allocate_numa_cores", "b", numa_node=0, num_of_cores=4)
    assert "error" in conflict
    assert policy_api("list_allocations") == before
    assert before["allocations"][0]["preemption_policy"] == "non-preemptive"
    assert policy_api("allocate_cores", num_of_cores=-1)["preemption_policy"] is None
    assert "error" not in policy_api("allocate_numa_cores", "b", numa_node=0, num_of_cores=4)


def test_omitted_policy_across_apis_and_release(policy_api):
    """Wrappers retain inherited policy and node releases report remaining policy accurately."""
    policy_api("allocate_cores", num_of_cores=2, preemption_policy="non-preemptive")
    assert (
        policy_api("allocate_cores_percent", percent=25)["preemption_policy"] == "non-preemptive"
    )
    assert (
        policy_api("allocate_numa_cores", numa_node=1, num_of_cores=2)["preemption_policy"]
        == "non-preemptive"
    )
    assert (
        policy_api("allocate_numa_cores", numa_node=0, num_of_cores=-1)["preemption_policy"]
        == "non-preemptive"
    )
    assert "error" in policy_api("allocate_cores", num_of_cores=2, preemption_policy="legacy")
    assert policy_api("allocate_cores_percent", percent=0)["preemption_policy"] is None
    assert policy_api("allocate_cores", num_of_cores=2)["preemption_policy"] == "legacy"


def test_zero_capacity_lists_claims_and_support(policy_api, monkeypatch):
    """Offline CPUs do not hide ownership, policy, or capability advertisement."""
    policy_api("allocate_cores", num_of_cores=2, preemption_policy="non-preemptive")
    # Online eligibility refreshes even though the configured choice is frozen.
    monkeypatch.setattr("epa_orchestrator.cpu_pool.read_cpu_list", lambda path: frozenset())
    result = policy_api("list_allocations")
    assert result["supported_cpu_features"] == ["non-preemptive-allocations"]
    assert result["total_available_cpus"] == 0
    assert result["remaining_available_cpus"] == 0
    assert result["allocations"][0]["allocated_cores"] == "0-1"
    assert policy_api("allocate_cores", num_of_cores=-1)["preemption_policy"] is None
    assert policy_api("list_allocations")["allocations"] == []


def test_api_pre_replace_failure_returns_error(policy_api, monkeypatch):
    """A storage failure cannot send a successful replacement or forget existing policy."""
    policy_api("allocate_cores", num_of_cores=2, preemption_policy="non-preemptive")
    before = policy_api("list_allocations")

    def fail(*args):
        raise OSError("simulated read-only state")

    with monkeypatch.context() as patcher:
        patcher.setattr("epa_orchestrator.state_store.os.replace", fail)
        result = policy_api("allocate_cores", num_of_cores=4)
    assert "error" in result
    assert policy_api("list_allocations") == before


def test_api_uncertain_commit_returns_error_and_blocks(policy_api, monkeypatch):
    """No success reply is possible after replacement without directory durability."""
    from epa_orchestrator.state_store import StateStore

    policy_api("allocate_cores", num_of_cores=2, preemption_policy="non-preemptive")
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
            result = policy_api("allocate_cores", num_of_cores=4)
        assert "error" in result
        assert "error" in policy_api("allocate_cores", "b", num_of_cores=1)
        claims = policy_api("list_allocations")["allocations"]
        assert claims[0]["allocated_cores"] == "0-3"
        assert claims[0]["preemption_policy"] == "non-preemptive"
    finally:
        allocations_db._state_store.recover()
