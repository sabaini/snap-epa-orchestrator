# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Tracking of hugepage allocation requests per service."""

import logging
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from epa_orchestrator.schemas import (
    HugepageAllocationEntry,
    NodeHugepageAllocation,
    ServiceHugepageAllocations,
)
from epa_orchestrator.state_store import StateStore

# Structure: service_name -> list of {"node_id": int, "size_kb": int, "count": int}
HugepageAllocations = Dict[str, List[Dict[str, int]]]
T = TypeVar("T")
_allocations: HugepageAllocations = {}
_store: StateStore = StateStore()


def _restore_allocations(data: Dict[str, Any]) -> HugepageAllocations:
    """Decode the existing state format, retaining its legacy entry validation."""
    restored: HugepageAllocations = {}
    raw = data.get("allocations")
    if isinstance(raw, dict):
        for svc, entries in raw.items():
            if not isinstance(entries, list):
                continue
            valid_entries: List[Dict[str, int]] = []
            for entry in entries:
                try:
                    obj = HugepageAllocationEntry(**entry)
                    valid_entries.append(
                        {"node_id": obj.node_id, "size_kb": obj.size_kb, "count": obj.count}
                    )
                except Exception:
                    continue
            if valid_entries:
                restored[str(svc)] = valid_entries
    return restored


def _load_from_store() -> None:
    restored = _restore_allocations(_store.read_section("hugepages_db"))
    _allocations.clear()
    _allocations.update(restored)


def _transaction(operation: Callable[[HugepageAllocations], T]) -> T:
    """Mutate fresh private state and publish only after a durable commit."""
    candidate: HugepageAllocations = {}

    def update(data: Dict[str, Any]) -> tuple[Dict[str, Any], T]:
        candidate.update(_restore_allocations(data))
        result = operation(candidate)
        data["allocations"] = candidate
        return data, result

    try:
        result = _store.transaction_section("hugepages_db", update)
    except Exception:
        # Replacement may have happened even if its durability check failed.
        _load_from_store()
        raise
    _allocations.clear()
    _allocations.update(candidate)
    return result


# Load persisted state at import time
_load_from_store()


def upsert_allocation(service_name: str, node_id: int, size_kb: int, count: int) -> None:
    """Replace the record for service+node+size in one durable transaction."""
    entry = HugepageAllocationEntry(node_id=node_id, size_kb=size_kb, count=count)

    def update(candidate: HugepageAllocations) -> None:
        retained = [
            e
            for e in candidate.get(service_name, [])
            if not (e["node_id"] == entry.node_id and e["size_kb"] == entry.size_kb)
        ]
        candidate[service_name] = retained + [
            {"node_id": entry.node_id, "size_kb": entry.size_kb, "count": entry.count}
        ]

    _transaction(update)
    logging.info(
        f"Set hugepage allocation for {service_name} node {node_id} size {size_kb}KB -> {count}"
    )


def list_allocations() -> Dict[str, List[Dict[str, int]]]:
    """Return all hugepage allocation records by service."""
    _load_from_store()
    result: Dict[str, List[Dict[str, int]]] = {}
    for service, entries in _allocations.items():
        validated = ServiceHugepageAllocations(
            service_name=service,
            allocations=[HugepageAllocationEntry(**e) for e in entries],
        )
        result[service] = [
            {"node_id": e.node_id, "size_kb": e.size_kb, "count": e.count}
            for e in validated.allocations
        ]
    return result


def list_allocations_for_node(node_id: int) -> List[Dict[str, Union[str, int]]]:
    """Return flattened list of allocations for a specific node."""
    _load_from_store()
    results: List[Dict[str, Union[str, int]]] = []
    for service, entries in _allocations.items():
        for entry in entries:
            if entry.get("node_id") == node_id:
                validated = NodeHugepageAllocation(
                    service_name=service,
                    size_kb=int(entry.get("size_kb", 0)),
                    count=int(entry.get("count", 0)),
                )
                results.append(
                    {
                        "service_name": validated.service_name,
                        "size_kb": validated.size_kb,
                        "count": validated.count,
                    }
                )
    return results


def get_allocation(service_name: str) -> Optional[List[Dict[str, int]]]:
    """Get the allocated hugepages for a specific service."""
    _load_from_store()
    return _allocations.get(service_name)


def clear_all_allocations() -> None:
    """Clear all allocations durably."""
    _transaction(lambda candidate: candidate.clear())
    logging.info("Cleared all hugepage allocations")


def remove_allocation_for_key(service_name: str, node_id: int, size_kb: int) -> bool:
    """Remove a service+node+size record, returning whether it existed."""

    def remove(candidate: HugepageAllocations) -> bool:
        entries = candidate.get(service_name, [])
        retained = [
            e for e in entries if not (e["node_id"] == node_id and e["size_kb"] == size_kb)
        ]
        if len(retained) == len(entries):
            return False
        if retained:
            candidate[service_name] = retained
        else:
            del candidate[service_name]
        return True

    removed = _transaction(remove)
    if removed:
        logging.info(
            f"Removed hugepage allocation records for {service_name} node {node_id} size {size_kb}KB"
        )
    return removed
