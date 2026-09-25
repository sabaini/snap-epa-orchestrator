# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Daemon handler for EPA Orchestrator."""

import json
import logging
import math
from functools import partial
from typing import Callable, Dict, List, Optional, Tuple, Type, Union, cast

from pydantic import BaseModel, ValidationError

from epa_orchestrator.allocations_db import AllocationsDB, allocations_db
from epa_orchestrator.cpu_pool import CpuPoolProvider, CpuPools, CpuPoolSnapshot
from epa_orchestrator.hugepages_db import (
    list_allocations_for_node,
    remove_allocation_for_key,
    upsert_allocation,
)
from epa_orchestrator.memory_manager import get_memory_summary
from epa_orchestrator.schemas import (
    ActionType,
    AllocateCoresPercentRequest,
    AllocateCoresPercentResponse,
    AllocateCoresRequest,
    AllocateCoresResponse,
    AllocateHugepagesRequest,
    AllocateHugepagesResponse,
    AllocateNumaCoresRequest,
    AllocateNumaCoresResponse,
    CpuPoolInfo,
    CpuPoolName,
    ErrorResponse,
    GetMemoryInfoRequest,
    ListAllocationsRequest,
    ListAllocationsResponse,
    MemoryInfoResponse,
    NodeHugepagesInfo,
)
from epa_orchestrator.state_store import StateCorruptionError
from epa_orchestrator.utils import (
    _count_cpus_in_ranges,
    get_cpus_in_numa_node,
    get_numa_node_cpus,
    parse_cpu_ranges,
    to_ranges,
)


def _get_hugepages_context(
    service_name: str, node_id: int, size_kb: int
) -> Union[Tuple[int, int], ErrorResponse]:
    """Return (free, existing_count) for node/size, or ErrorResponse on failure."""
    summary = get_memory_summary()
    if "error" in summary:
        err = str(summary.get("error", "Unknown error"))
        logging.error(f"Failed to get memory information: {err}")
        return ErrorResponse(error=f"Failed to get memory information: {err}")

    numa_hugepages = cast(Dict[str, Dict[str, object]], summary.get("numa_hugepages", {}))
    node_key = f"node{node_id}"
    node_info = numa_hugepages.get(node_key)
    if not node_info:
        return ErrorResponse(error=f"NUMA node {node_id} not found")

    capacity_list = cast(List[Dict[str, int]], node_info.get("capacity", []))
    size_entry: Optional[Dict[str, int]] = next(
        (e for e in capacity_list if int(e.get("size", -1)) == size_kb),
        None,
    )
    if not size_entry:
        return ErrorResponse(error=f"Hugepage size {size_kb} KB not found on node {node_id}")

    existing_count = 0
    for entry in list_allocations_for_node(node_id):
        if (
            str(entry.get("service_name", "")) == service_name
            and int(entry.get("size_kb", -1)) == size_kb
        ):
            existing_count = int(entry.get("count", 0))
            break

    free = int(size_entry.get("free", 0))
    return free, existing_count


_default_pool_provider: Optional[CpuPools] = None


def get_cpu_pool_provider() -> CpuPools:
    """Lazily construct an isolated-mode provider for source-checkout callers."""
    global _default_pool_provider
    if _default_pool_provider is None:
        _default_pool_provider = CpuPools()
    return _default_pool_provider


def validate_startup_pool(pools: CpuPools) -> None:
    """Log pool/claim conflicts but keep introspection and releases available."""
    for pool in CpuPoolName:
        claimed = allocations_db.get_claimed_cpus(pool)
        if not claimed:
            continue
        try:
            pools.select(pool).validate_claims(claimed)
        except ValueError as exc:
            logging.error("New CPU allocations blocked in pool %s: %s", pool.value, exc)


def handle_allocate_cores(
    request: AllocateCoresRequest,
    pool_provider: Optional[CpuPools] = None,
) -> AllocateCoresResponse:
    """Allocate using one pool snapshot and one locked ownership transaction."""
    provider = (pool_provider or get_cpu_pool_provider()).select(request.pool)
    return allocations_db.transaction(
        lambda db: _allocate_cores(
            request, db, provider, provider.snapshot(allow_unavailable=request.num_of_cores == -1)
        )
    )


def _allocate_cores(
    request: AllocateCoresRequest,
    db: AllocationsDB,
    provider: CpuPoolProvider,
    pool: CpuPoolSnapshot,
) -> AllocateCoresResponse:
    """Build a response from the candidate which must be committed before success."""
    db.ensure_service_pool(request.service_name, request.pool)
    eligible = to_ranges(sorted(pool.eligible_cpus))

    if request.num_of_cores == -1:
        db.remove_allocation(request.service_name, pool=request.pool)
        stats = db.get_system_stats(eligible)
        remaining_available = db.get_available_cpus(eligible)
        remaining_shared = to_ranges(remaining_available)
        return AllocateCoresResponse(
            service_name=request.service_name,
            pool=request.pool,
            num_of_cores=0,
            cores_allocated=0,
            allocated_cores="",
            shared_cpus=remaining_shared,
            total_available_cpus=stats["total_available_cpus"],
            remaining_available_cpus=stats["remaining_available_cpus"],
        )

    provider.validate_claims(db.get_claimed_cpus(request.pool))
    if not pool.eligible_cpus:
        raise ValueError("No CPUs available")

    num_of_cores = request.num_of_cores or 0
    shared, dedicated = db.allocate_count(
        request.service_name,
        num_of_cores,
        eligible,
        request.preemption_policy,
        check_online=provider.check_online,
        pool=request.pool,
    )

    updated_stats = db.get_system_stats(eligible)
    cores_allocated = _count_cpus_in_ranges(dedicated)

    return AllocateCoresResponse(
        service_name=request.service_name,
        pool=request.pool,
        num_of_cores=num_of_cores,
        cores_allocated=cores_allocated,
        allocated_cores=dedicated,
        preemption_policy=db.get_preemption_policy(request.service_name),
        shared_cpus=shared,
        total_available_cpus=updated_stats["total_available_cpus"],
        remaining_available_cpus=updated_stats["remaining_available_cpus"],
    )


def handle_allocate_cores_percent(
    request: AllocateCoresPercentRequest,
    pool_provider: Optional[CpuPools] = None,
) -> AllocateCoresPercentResponse:
    """Allocate a percentage of the complete eligible pool.

    If percent is -1, deallocate the service's cores.
    If percent is 0, treat as deallocate.
    Otherwise, allocate the percentage of eligible CPUs, not free CPUs.
    The computed core count is ceiling-rounded so small positive percentages
    (e.g. 1% of 8 cores) yield at least 1 core and never fall back to num_of_cores=0.
    """
    provider = (pool_provider or get_cpu_pool_provider()).select(request.pool)
    return allocations_db.transaction(
        lambda db: _allocate_cores_percent(
            request, db, provider, provider.snapshot(allow_unavailable=request.percent in (-1, 0))
        )
    )


def _allocate_cores_percent(
    request: AllocateCoresPercentRequest,
    db: AllocationsDB,
    provider: CpuPoolProvider,
    snapshot: CpuPoolSnapshot,
) -> AllocateCoresPercentResponse:
    """Use the same pool and ledger snapshot for percentage conversion and selection."""
    num_of_cores = (
        -1
        if request.percent in (-1, 0)
        else math.ceil(len(snapshot.eligible_cpus) * request.percent / 100)
    )

    core_req = AllocateCoresRequest(
        service_name=request.service_name,
        pool=request.pool,
        action=ActionType.ALLOCATE_CORES,
        num_of_cores=num_of_cores,
        preemption_policy=request.preemption_policy,
    )
    result = _allocate_cores(core_req, db, provider, snapshot)
    return AllocateCoresPercentResponse(
        version=result.version,
        pool=result.pool,
        service_name=result.service_name,
        cores_allocated_count=result.cores_allocated,
        preemption_policy=result.preemption_policy,
        allocated_cores=result.allocated_cores,
        total_available_cpus=result.total_available_cpus,
        remaining_available_cpus=result.remaining_available_cpus,
    )


def handle_allocate_numa_cores(
    request: AllocateNumaCoresRequest,
    pool_provider: Optional[CpuPools] = None,
) -> AllocateNumaCoresResponse:
    """Handle allocate NUMA cores action.

    Supports exact-count allocation and per-node deallocation with num_of_cores = -1.
    """
    provider = (pool_provider or get_cpu_pool_provider()).select(request.pool)
    return allocations_db.transaction(
        lambda db: _allocate_numa_cores(
            request, db, provider, provider.snapshot(allow_unavailable=request.num_of_cores == -1)
        )
    )


def _allocate_numa_cores(
    request: AllocateNumaCoresRequest,
    db: AllocationsDB,
    provider: CpuPoolProvider,
    pool: CpuPoolSnapshot,
) -> AllocateNumaCoresResponse:
    """Validate pool/ownership and construct the committed NUMA response under lock."""
    if request.num_of_cores == 0:
        raise ValueError("num_of_cores=0 is invalid for allocate_numa_cores")

    db.ensure_service_pool(request.service_name, request.pool)
    eligible = to_ranges(sorted(pool.eligible_cpus))
    stats = db.get_system_stats(eligible)
    numa_cpus = get_numa_node_cpus()

    if request.numa_node not in numa_cpus:
        raise ValueError(f"NUMA node {request.numa_node} does not exist")

    if request.num_of_cores == -1:
        # Deallocate any existing cores for this service in the specified node
        allocated_cores, _ = db.allocate_numa_cores(
            request.service_name,
            request.numa_node,
            request.num_of_cores,
            request.preemption_policy,
            eligible_cpus=pool.eligible_cpus,
            check_online=provider.check_online,
            pool=request.pool,
        )
        updated_stats = db.get_system_stats(eligible)

        return AllocateNumaCoresResponse(
            service_name=request.service_name,
            pool=request.pool,
            numa_node=request.numa_node,
            num_of_cores=request.num_of_cores,
            cores_allocated=allocated_cores,
            preemption_policy=db.get_preemption_policy(request.service_name),
            total_available_cpus=stats["total_available_cpus"],
            remaining_available_cpus=updated_stats["remaining_available_cpus"],
        )

    provider.validate_claims(db.get_claimed_cpus(request.pool))
    if not pool.eligible_cpus:
        raise ValueError("No CPUs available")

    # Allocation path (num_of_cores > 0)
    available_numa_cpus = get_cpus_in_numa_node(request.numa_node, eligible)
    if not available_numa_cpus:
        raise ValueError(f"No eligible CPUs available in NUMA node {request.numa_node}")

    if len(available_numa_cpus) < request.num_of_cores:
        raise ValueError(
            f"NUMA node {request.numa_node} only has {len(available_numa_cpus)} eligible CPUs, "
            f"but {request.num_of_cores} were requested"
        )

    allocated_cores, unavailable = db.allocate_numa_cores(
        request.service_name,
        request.numa_node,
        request.num_of_cores,
        request.preemption_policy,
        eligible_cpus=pool.eligible_cpus,
        check_online=provider.check_online,
        pool=request.pool,
    )

    if not allocated_cores:
        raise ValueError(
            f"Failed to allocate {request.num_of_cores} cores from NUMA node "
            f"{request.numa_node}. CPUs {unavailable} are held by other services through "
            f"an explicit NUMA or non-preemptive claim."
        )

    updated_stats = db.get_system_stats(eligible)

    return AllocateNumaCoresResponse(
        service_name=request.service_name,
        pool=request.pool,
        numa_node=request.numa_node,
        num_of_cores=request.num_of_cores,
        cores_allocated=allocated_cores,
        preemption_policy=db.get_preemption_policy(request.service_name),
        total_available_cpus=stats["total_available_cpus"],
        remaining_available_cpus=updated_stats["remaining_available_cpus"],
    )


# Hugepages/memory handlers restored from main


def handle_get_memory_info(
    request: GetMemoryInfoRequest,
) -> Union[MemoryInfoResponse, ErrorResponse]:
    """Handle get memory info action."""
    try:
        memory_summary = get_memory_summary()
        if "error" in memory_summary:
            err = str(memory_summary.get("error", "Unknown error"))
            logging.error(f"Failed to get memory information: {err}")
            return ErrorResponse(error=f"Failed to get memory information: {err}")
        numa_map = cast(Dict[str, NodeHugepagesInfo], memory_summary.get("numa_hugepages", {}))
        return MemoryInfoResponse(
            service_name=request.service_name or "",
            numa_hugepages=numa_map,
        )
    except Exception as e:
        logging.error(f"Failed to get memory information: {e}")
        return ErrorResponse(error=f"Failed to get memory information: {e}")


def handle_allocate_hugepages(
    request: AllocateHugepagesRequest,
) -> Union[AllocateHugepagesResponse, ErrorResponse]:
    """Handle allocate hugepages action (tracking only)."""
    try:
        # Validation for 0 is handled by schema; treat -1 as deallocation
        if request.hugepages_requested == -1:
            removed = remove_allocation_for_key(
                request.service_name, request.node_id, request.size_kb
            )
            message = (
                "Removed recorded hugepage allocation"
                if removed
                else "No existing record to remove"
            )
            return AllocateHugepagesResponse(
                service_name=request.service_name,
                hugepages_requested=request.hugepages_requested,
                allocation_successful=True,
                message=message,
                node_id=request.node_id,
                size_kb=request.size_kb,
            )

        # Capacity validation for positive requests
        if request.hugepages_requested > 0:
            ctx = _get_hugepages_context(request.service_name, request.node_id, request.size_kb)
            if isinstance(ctx, ErrorResponse):
                return ctx
            free, existing_count = ctx

            # Only additional increase beyond existing requires free capacity
            delta = int(request.hugepages_requested) - int(existing_count)
            if delta > 0 and free < delta:
                return ErrorResponse(
                    error=(
                        f"NUMA node {request.node_id} size {request.size_kb} KB only has {free} "
                        f"free hugepages, requested additional {delta}"
                    )
                )

        # Record (replace) the allocation request for this key
        upsert_allocation(
            request.service_name, request.node_id, request.size_kb, request.hugepages_requested
        )

        message = f"Successfully set allocation request to {request.hugepages_requested} hugepages"

        return AllocateHugepagesResponse(
            service_name=request.service_name,
            hugepages_requested=request.hugepages_requested,
            allocation_successful=True,
            message=message,
            node_id=request.node_id,
            size_kb=request.size_kb,
        )
    except Exception as e:
        logging.error(f"Failed to record hugepage allocation: {e}")
        return ErrorResponse(error=f"Failed to record hugepage allocation: {e}")


def handle_list_allocations(
    request: ListAllocationsRequest,
    pool_provider: Optional[CpuPools] = None,
) -> ListAllocationsResponse:
    """List the selected pool's saved owners, including currently unavailable claims."""
    provider = (pool_provider or get_cpu_pool_provider()).select(request.pool)
    pool = provider.snapshot(allow_unavailable=True)
    # Read ownership once so totals, policies and unavailable IDs remain coherent.
    all_entries = allocations_db.get_all_allocations()
    entries = [entry for entry in all_entries if entry.pool == request.pool]
    all_owned = set().union(*(parse_cpu_ranges(entry.allocated_cores) for entry in all_entries))
    owned = set().union(*(parse_cpu_ranges(entry.allocated_cores) for entry in entries))
    unavailable = owned - pool.eligible_cpus
    return ListAllocationsResponse(
        pool=request.pool,
        total_allocations=len(entries),
        total_allocated_cpus=len(owned),
        total_available_cpus=len(pool.eligible_cpus),
        remaining_available_cpus=len(pool.eligible_cpus - all_owned),
        allocations=entries,
        cpu_pool=CpuPoolInfo(
            source=pool.source,
            configured_cpus=to_ranges(sorted(pool.configured_cpus)),
            eligible_cpus=to_ranges(sorted(pool.eligible_cpus)),
            unavailable_allocated_cpus=to_ranges(sorted(unavailable)),
        ),
    )


def handle_daemon_request(data: bytes, pool_provider: Optional[CpuPools] = None) -> bytes:
    """Handle daemon request."""
    response_bytes: bytes = b""
    try:
        request_data = json.loads(data.decode())
        logging.info("EPA request: action=%s payload=%s", request_data.get("action"), request_data)
        action_value = request_data.get("action")

        dispatcher: Dict[str, Tuple[Type[BaseModel], Callable[..., BaseModel]]] = {
            ActionType.ALLOCATE_CORES.value: (
                AllocateCoresRequest,
                partial(handle_allocate_cores, pool_provider=pool_provider),
            ),
            ActionType.ALLOCATE_CORES_PERCENT.value: (
                AllocateCoresPercentRequest,
                partial(handle_allocate_cores_percent, pool_provider=pool_provider),
            ),
            ActionType.ALLOCATE_NUMA_CORES.value: (
                AllocateNumaCoresRequest,
                partial(handle_allocate_numa_cores, pool_provider=pool_provider),
            ),
            ActionType.LIST_ALLOCATIONS.value: (
                ListAllocationsRequest,
                partial(handle_list_allocations, pool_provider=pool_provider),
            ),
            ActionType.GET_MEMORY_INFO.value: (GetMemoryInfoRequest, handle_get_memory_info),
            ActionType.ALLOCATE_HUGEPAGES.value: (
                AllocateHugepagesRequest,
                handle_allocate_hugepages,
            ),
        }

        # Normalize action to string value
        key = action_value.value if isinstance(action_value, ActionType) else str(action_value)
        entry = dispatcher.get(key)
        if not entry:
            response = ErrorResponse(error=f"Unknown action: {action_value}", version="1.0")
            response_bytes = response.json().encode()
        else:
            schema_cls, handler_fn = entry
            req_obj: BaseModel = schema_cls.parse_obj(request_data)
            resp_obj: BaseModel = handler_fn(req_obj)
            response_bytes = resp_obj.json().encode()
    except StateCorruptionError:
        # Let corruption crash the daemon so the charm can block/alert
        raise
    except (ValidationError, json.JSONDecodeError) as e:
        error_response = ErrorResponse(error=str(e), version="1.0")
        response_bytes = error_response.json().encode()
    except ValueError as e:
        error_response = ErrorResponse(error=str(e), version="1.0")
        response_bytes = error_response.json().encode()
    except Exception as e:
        error_response = ErrorResponse(error=str(e), version="1.0")
        response_bytes = error_response.json().encode()

    logging.info("EPA response: %s", response_bytes.decode())
    try:
        # Do not take a second pool snapshot just for diagnostic logging.
        logging.info("EPA allocations: %s", allocations_db.get_all_allocations())
    except Exception as e:
        # Best-effort allocations logging; must not interfere with responding
        logging.warning("Failed to list EPA allocations for logging: %s", e)
    return response_bytes
