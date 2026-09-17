# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Concise unit tests for epa_orchestrator.schemas."""

import pytest
from pydantic import ValidationError

from epa_orchestrator.schemas import (
    ActionType,
    AllocateCoresPercentRequest,
    AllocateCoresRequest,
    AllocateCoresResponse,
    AllocateNumaCoresRequest,
    CpuPoolInfo,
    ListAllocationsRequest,
    ListAllocationsResponse,
    SnapAllocation,
)


class TestSchemas:
    """Unit tests for schema validation and serialization."""

    def test_allocate_cores_request_valid(self):
        """Test valid AllocateCoresRequest creation."""
        req = AllocateCoresRequest(
            service_name="service1", action=ActionType.ALLOCATE_CORES, num_of_cores=2
        )
        assert req.service_name == "service1"
        assert req.action == ActionType.ALLOCATE_CORES
        assert req.num_of_cores == 2

    def test_list_allocations_request_valid(self):
        """Test valid ListAllocationsRequest creation."""
        req = ListAllocationsRequest(service_name="service1", action=ActionType.LIST_ALLOCATIONS)
        assert req.service_name == "service1"
        assert req.action == ActionType.LIST_ALLOCATIONS

    def test_allocate_cores_request_invalid(self):
        """Test invalid AllocateCoresRequest creation."""
        with pytest.raises(Exception):
            AllocateCoresRequest(service_name="service1", action="invalid_action", num_of_cores=2)

    def test_allocate_cores_percent_request_valid(self):
        """Test valid AllocateCoresPercentRequest creation."""
        req = AllocateCoresPercentRequest(
            service_name="svc1", action=ActionType.ALLOCATE_CORES_PERCENT, percent=50
        )
        assert req.service_name == "svc1"
        assert req.action == ActionType.ALLOCATE_CORES_PERCENT
        assert req.percent == 50

    def test_allocate_cores_percent_request_deallocate(self):
        """Test AllocateCoresPercentRequest with percent=-1 for deallocate."""
        req = AllocateCoresPercentRequest(
            service_name="svc1", action=ActionType.ALLOCATE_CORES_PERCENT, percent=-1
        )
        assert req.percent == -1

    def test_allocate_cores_percent_request_invalid_percent_over_100(self):
        """Percent > 100 is rejected; API allows 0-100 or -1."""
        with pytest.raises(ValidationError):
            AllocateCoresPercentRequest(
                service_name="svc1",
                action=ActionType.ALLOCATE_CORES_PERCENT,
                percent=101,
            )

    def test_list_allocations_request_invalid(self):
        """Test invalid ListAllocationsRequest creation."""
        with pytest.raises(Exception):
            ListAllocationsRequest(service_name="service1", action="invalid_action")

    def test_allocate_cores_response(self):
        """Test AllocateCoresResponse serialization."""
        resp = AllocateCoresResponse(
            service_name="service1",
            num_of_cores=2,
            cores_allocated=2,
            allocated_cores="0-1",
            shared_cpus="2-3",
            total_available_cpus=4,
            remaining_available_cpus=2,
        )
        assert resp.service_name == "service1"
        assert resp.cores_allocated == 2

    def test_pool_introspection_serialization(self):
        """The additive pool object distinguishes configured, eligible and owned IDs."""
        response = ListAllocationsResponse(
            total_allocations=1,
            total_allocated_cpus=1,
            total_available_cpus=0,
            remaining_available_cpus=0,
            allocations=[SnapAllocation(service_name="owner", allocated_cores="2", cores_count=1)],
            cpu_pool=CpuPoolInfo(
                source="configured",
                configured_cpus="2",
                eligible_cpus="",
                unavailable_allocated_cpus="2",
            ),
        )
        assert response.model_dump()["cpu_pool"] == {
            "source": "configured",
            "configured_cpus": "2",
            "eligible_cpus": "",
            "unavailable_allocated_cpus": "2",
        }
        assert response.total_allocated_cpus > response.total_available_cpus

    def test_snap_allocation(self):
        """Test SnapAllocation model serialization."""
        alloc = SnapAllocation(service_name="service1", allocated_cores="0-1", cores_count=2)
        assert alloc.service_name == "service1"
        assert alloc.allocated_cores == "0-1"
        assert alloc.cores_count == 2


@pytest.mark.parametrize(
    "model,fields",
    [
        (AllocateCoresRequest, {"action": "allocate_cores", "num_of_cores": 1}),
        (AllocateCoresPercentRequest, {"action": "allocate_cores_percent", "percent": 50}),
        (
            AllocateNumaCoresRequest,
            {"action": "allocate_numa_cores", "numa_node": 0, "num_of_cores": 1},
        ),
    ],
)
def test_preemption_policy_schema(model, fields):
    """Preserve omission for inheritance and reject unknown protection values."""
    assert model(service_name="a", **fields).preemption_policy is None
    for policy in ("legacy", "non-preemptive"):
        assert (
            model(service_name="a", preemption_policy=policy, **fields).preemption_policy == policy
        )
    for invalid in ("nonpreemptive", "", 1, []):
        with pytest.raises(ValidationError):
            model(service_name="a", preemption_policy=invalid, **fields)
