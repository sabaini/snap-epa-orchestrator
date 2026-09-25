# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Database for tracking snap CPU allocations."""

import logging
from functools import wraps
from typing import (
    AbstractSet,
    Any,
    Callable,
    Concatenate,
    Dict,
    Optional,
    ParamSpec,
    Set,
    Tuple,
    TypeVar,
)

from .cpu_pinning import (
    calculate_cpu_pinning,
    get_thread_siblings_map,
)
from .schemas import CpuPoolName, PreemptionPolicy, SnapAllocation
from .state_store import StateCorruptionError, StateStore
from .utils import (
    get_cpus_in_numa_node,
    parse_cpu_ranges,
    to_ranges,
)

P = ParamSpec("P")
T = TypeVar("T")


def _mutating(
    method: Callable[Concatenate["AllocationsDB", P], T],
) -> Callable[Concatenate["AllocationsDB", P], T]:
    """Keep public and internal mutation entry points on the transaction path."""

    @wraps(method)
    def wrapped(self: "AllocationsDB", /, *args: P.args, **kwargs: P.kwargs) -> T:
        return self.transaction(lambda candidate: method(candidate, *args, **kwargs))

    return wrapped


class AllocationsDB:
    """In-memory database for tracking snap CPU allocations."""

    def __init__(self) -> None:
        """Initialize the allocations database."""
        self._in_transaction = False
        self._preemption_policies: Dict[str, PreemptionPolicy] = {}
        self._allocation_pools: Dict[str, CpuPoolName] = {}
        self._allocations: Dict[str, str] = {}
        self._allocated_cpus: Set[int] = set()
        self._explicit_allocations: Dict[str, str] = {}
        self._explicitly_allocated_cpus: Set[int] = set()
        logging.info("Allocations database initialized")
        self._state_store: StateStore = StateStore()
        self._load_from_store()

    def transaction(self, operation: Callable[["AllocationsDB"], T]) -> T:
        """Apply an operation to a private fresh snapshot, publishing only after commit."""
        if self._in_transaction:
            return operation(self)
        candidate = object.__new__(AllocationsDB)
        candidate._in_transaction = True
        candidate._state_store = self._state_store

        def update(data: Dict[str, Any]) -> tuple[Dict[str, Any], T]:
            candidate._load_snapshot(data)
            result = operation(candidate)
            return candidate._snapshot(), result

        try:
            result = self._state_store.transaction_section("allocations_db", update)
        except Exception:
            # A failed rename durability check may already have installed new claims.
            self._load_from_store()
            raise
        self._load_snapshot(candidate._snapshot())
        return result

    def _effective_policy(
        self, service_name: str, requested: Optional[PreemptionPolicy] = None
    ) -> PreemptionPolicy:
        stored = self._preemption_policies.get(service_name, PreemptionPolicy.LEGACY)
        policy = PreemptionPolicy(requested) if requested is not None else stored
        if stored == PreemptionPolicy.NON_PREEMPTIVE and policy != stored:
            raise ValueError("Fully release protected claims before selecting legacy policy")
        return policy

    def get_preemption_policy(self, service_name: str) -> Optional[PreemptionPolicy]:
        """Return the policy of current claims, or None after full release."""
        self._load_from_store()
        if service_name not in self._allocations:
            return None
        return self._preemption_policies.get(service_name, PreemptionPolicy.LEGACY)

    def ensure_service_pool(self, service_name: str, pool: CpuPoolName) -> None:
        """Reject cross-pool changes, including releases, while the service owns CPUs."""
        self._load_from_store()
        if service_name in self._allocations and self._service_pool(service_name) != pool:
            raise ValueError(
                f"Service {service_name} owns CPUs in pool {self._service_pool(service_name).value}; "
                "release that allocation in its pool before switching pools"
            )

    def _service_pool(self, service_name: str) -> CpuPoolName:
        return self._allocation_pools.get(service_name, CpuPoolName.ISOLATED)

    def _parse_cpu_ranges(self, cpu_ranges: str) -> set[int]:
        """Parse CPU range string into a set of CPU numbers."""
        return set(parse_cpu_ranges(cpu_ranges))

    def _remove_service_allocation(self, service_name: str) -> set[int]:
        """Remove any allocation for a service and return removed CPU set."""
        removed: set[int] = set()
        if service_name in self._allocations:
            old_cores = self._allocations.pop(service_name)
            removed = self._parse_cpu_ranges(old_cores)
            self._allocated_cpus -= removed
        if service_name in self._explicit_allocations:
            old_explicit = self._parse_cpu_ranges(self._explicit_allocations.pop(service_name))
            self._explicitly_allocated_cpus -= old_explicit
        self._preemption_policies.pop(service_name, None)
        self._allocation_pools.pop(service_name, None)
        return removed

    def _snapshot(self) -> Dict[str, object]:
        """Return a JSON-serializable snapshot of current state."""
        return {
            "allocations": dict(self._allocations),
            "explicit_allocations": dict(self._explicit_allocations),
            "preemption_policies": dict(self._preemption_policies),
            "allocation_pools": dict(self._allocation_pools),
        }

    def _load_from_store(self) -> None:
        if not self._in_transaction:
            self._load_snapshot(self._state_store.read_section("allocations_db"))

    def _load_snapshot(self, data: Dict[str, Any]) -> None:
        policies = data.get("preemption_policies", {})
        if not isinstance(policies, dict):
            raise StateCorruptionError("Invalid preemption_policies mapping")
        try:
            self._preemption_policies = {
                name: PreemptionPolicy(value) for name, value in policies.items()
            }
        except (ValueError, TypeError) as error:
            raise StateCorruptionError("Invalid stored preemption policy") from error

        allocations = data.get("allocations")
        explicit_allocations = data.get("explicit_allocations")

        if isinstance(allocations, dict):
            self._allocations = {str(k): str(v) for k, v in allocations.items()}
        else:
            self._allocations = {}
        if isinstance(explicit_allocations, dict):
            self._explicit_allocations = {str(k): str(v) for k, v in explicit_allocations.items()}
        else:
            self._explicit_allocations = {}

        if any(name not in self._allocations for name in self._preemption_policies):
            raise StateCorruptionError("Preemption policy without owned CPUs")

        self._load_pool_metadata(data)

        self._allocated_cpus = set()
        self._explicitly_allocated_cpus = set()
        for cores_str in self._allocations.values():
            self._allocated_cpus.update(self._parse_cpu_ranges(cores_str))
        for cores_str in self._explicit_allocations.values():
            self._explicitly_allocated_cpus.update(self._parse_cpu_ranges(cores_str))

    def _load_pool_metadata(self, data: Dict[str, Any]) -> None:
        """Decode pool ownership without inferring it from mutable host topology."""
        pools = data.get("allocation_pools", {})
        if not isinstance(pools, dict):
            raise StateCorruptionError("Invalid allocation_pools mapping")
        try:
            self._allocation_pools = {name: CpuPoolName(value) for name, value in pools.items()}
        except (ValueError, TypeError) as error:
            raise StateCorruptionError("Invalid stored CPU pool") from error

        if any(name not in self._allocations for name in self._allocation_pools):
            raise StateCorruptionError("CPU pool without owned CPUs")

    def _apply_allocation(self, service_name: str, cpu_set: set[int], explicit: bool) -> None:
        """Apply an allocation to a service, updating all tracking structures."""
        if not cpu_set:
            return
        cores_str = to_ranges(sorted(cpu_set))
        self._allocations[service_name] = cores_str
        self._allocated_cpus.update(cpu_set)
        if explicit:
            self._explicit_allocations[service_name] = cores_str
            self._explicitly_allocated_cpus.update(cpu_set)
        elif service_name in self._explicit_allocations:
            del self._explicit_allocations[service_name]

    @_mutating
    def _subtract_cpus_from_service(
        self,
        service_name: str,
        cpus_to_remove: set[int],
        *,
        requester: Optional[str] = None,
        requester_policy: Optional[PreemptionPolicy] = None,
        requester_pool: CpuPoolName = CpuPoolName.ISOLATED,
    ) -> None:
        """Subtract given CPUs from a service allocation, remove entry if empty."""
        if service_name not in self._allocations:
            return
        current_set = self._parse_cpu_ranges(self._allocations[service_name])
        if not (current_set & cpus_to_remove):
            return
        if requester is not None and requester != service_name:
            self.ensure_service_pool(requester, requester_pool)
            policy = self._effective_policy(requester, requester_policy)
            self._validate_reclamation(service_name, cpus_to_remove, policy, requester_pool)
        remaining = current_set - cpus_to_remove
        # Update global allocated CPUs
        self._allocated_cpus -= current_set & cpus_to_remove
        # Handle explicit tracking if needed
        if service_name in self._explicit_allocations:
            explicit_set = self._parse_cpu_ranges(self._explicit_allocations[service_name])
            explicit_remaining = explicit_set - cpus_to_remove
            self._explicitly_allocated_cpus -= explicit_set & cpus_to_remove
            if explicit_remaining:
                self._explicit_allocations[service_name] = to_ranges(sorted(explicit_remaining))
            else:
                del self._explicit_allocations[service_name]
        if remaining:
            self._allocations[service_name] = to_ranges(sorted(remaining))
        else:
            del self._allocations[service_name]
            self._preemption_policies.pop(service_name, None)
            self._allocation_pools.pop(service_name, None)

    def get_available_cpus(self, total_cpus: str) -> list[int]:
        """Get list of available CPUs that haven't been allocated.

        Args:
            total_cpus: Comma-separated list of all available CPU ranges

        Returns:
            List of available CPU numbers
        """
        self._load_from_store()
        all_cpus = self._parse_cpu_ranges(total_cpus)
        available_cpus = sorted(list(all_cpus - self._allocated_cpus))
        return available_cpus

    def get_available_cpus_for_service(self, service_name: str, total_cpus: str) -> list[int]:
        """Get CPUs available for (re-)allocation to a specific service.

        When a service requests allocation, it may already hold cores. Those cores
        should be in the pool since they will be freed and re-assigned. Excludes
        only allocations of *other* services.

        Args:
            service_name: Service requesting allocation
            total_cpus: Comma-separated list of all available CPU ranges (e.g. isolated)

        Returns:
            List of CPU numbers the service may be allocated from
        """
        self._load_from_store()
        all_cpus = self._parse_cpu_ranges(total_cpus)
        this_service_cpus = self._parse_cpu_ranges(
            self._allocations.get(service_name, "")
        ) | self._parse_cpu_ranges(self._explicit_allocations.get(service_name, ""))
        other_allocated = self._allocated_cpus - this_service_cpus
        return sorted(list(all_cpus - other_allocated))

    def can_allocate_cpus(self, requested_count: int, total_cpus: str) -> bool:
        """Check if requested number of CPUs can be allocated.

        Args:
            requested_count: Number of CPUs requested
            total_cpus: Comma-separated list of all available CPU ranges

        Returns:
            True if allocation is possible, False otherwise
        """
        self._load_from_store()
        available_cpus = self.get_available_cpus(total_cpus)
        return len(available_cpus) >= requested_count

    @_mutating
    def allocate_cores(
        self,
        service_name: str,
        allocated_cores: str,
        preemption_policy: Optional[PreemptionPolicy] = None,
        *,
        pool: CpuPoolName = CpuPoolName.ISOLATED,
    ) -> None:
        """Replace an owner's allocation, rejecting overlap before changing any state."""
        self.ensure_service_pool(service_name, pool)
        if not allocated_cores:
            return
        policy = self._effective_policy(service_name, preemption_policy)
        new_cpu_set = self._parse_cpu_ranges(allocated_cores)
        if not new_cpu_set:
            # Never record policy metadata without claims: that state cannot be loaded.
            return
        others = self._allocated_cpus - self._get_service_allocation_set(service_name)
        if new_cpu_set & others:
            raise ValueError("Requested CPUs are already allocated to other services")
        self._remove_service_allocation(service_name)
        self._apply_allocation(service_name, new_cpu_set, explicit=False)
        self._preemption_policies[service_name] = policy
        self._allocation_pools[service_name] = pool

    @_mutating
    def allocate_count(
        self,
        service_name: str,
        count: int,
        eligible_cpus: str,
        preemption_policy: Optional[PreemptionPolicy] = None,
        *,
        pool: CpuPoolName = CpuPoolName.ISOLATED,
        check_online: Callable[[AbstractSet[int]], None],
    ) -> tuple[str, str]:
        """Select and commit ordinary CPUs with capacity validation under the state lock."""
        self.ensure_service_pool(service_name, pool)
        policy = self._effective_policy(service_name, preemption_policy)
        candidates = set(self.get_available_cpus_for_service(service_name, eligible_cpus))
        self._check_offline_claims(service_name, self._parse_cpu_ranges(eligible_cpus))
        if count > len(candidates):
            raise ValueError(
                f"Insufficient CPUs available. Requested: {count}, Available: {len(candidates)}"
            )
        shared, dedicated = calculate_cpu_pinning(to_ranges(sorted(candidates)), count)
        if not dedicated:
            raise ValueError(f"Failed to allocate {count} cores")
        selected = self._parse_cpu_ranges(dedicated)
        target_count = count if count > 0 else len(selected)
        if policy == PreemptionPolicy.NON_PREEMPTIVE:
            selected = self._select_stable(service_name, candidates, target_count)
            dedicated = to_ranges(sorted(selected))
            shared = to_ranges(sorted(candidates - selected))
        self._validate_selection(selected, candidates, target_count)
        check_online(selected)
        self.allocate_cores(service_name, dedicated, policy, pool=pool)
        return shared, dedicated

    def _check_offline_claims(self, service_name: str, eligible: AbstractSet[int]) -> None:
        if self._get_service_allocation_set(service_name) - eligible:
            raise ValueError(
                "Existing claims are outside the eligible CPU pool; explicitly release before replacing"
            )

    def _select_stable(self, service_name: str, candidates: set[int], count: int) -> set[int]:
        own = self._get_service_allocation_set(service_name) & candidates
        retained = set(sorted(own)[:count])
        if len(retained) == count:
            return retained
        return retained | self._select_numa_cpus_smt_aware(candidates - own, count - len(retained))

    def _validate_selection(self, selected: set[int], candidates: set[int], count: int) -> None:
        """Reject a short or invalid selector result before changing ownership."""
        if len(selected) != count or not selected <= candidates:
            raise ValueError(f"Failed to select exactly {count} eligible CPUs")

    def _validate_reclamation(
        self,
        owner: str,
        cpus: set[int],
        requester_policy: PreemptionPolicy,
        requester_pool: CpuPoolName = CpuPoolName.ISOLATED,
    ) -> None:
        if (
            self._service_pool(owner) != requester_pool
            or requester_policy == PreemptionPolicy.NON_PREEMPTIVE
            or self._effective_policy(owner) == PreemptionPolicy.NON_PREEMPTIVE
            or cpus & self._get_service_explicit_set(owner)
        ):
            raise ValueError(f"Cannot reclaim CPUs owned by {owner}")

    def _get_allocatable_numa_cpus(
        self,
        service_name: str,
        numa_node: int,
        eligible_cpus: AbstractSet[int],
        preemption_policy: Optional[PreemptionPolicy] = None,
        pool: CpuPoolName = CpuPoolName.ISOLATED,
    ) -> Tuple[Set[int], Set[int]]:
        """Exclude foreign claims according to both requester and owner policy."""
        policy = self._effective_policy(service_name, preemption_policy)
        numa_cpus = get_cpus_in_numa_node(numa_node, to_ranges(sorted(eligible_cpus)))
        unavailable: set[int] = set()
        for owner in self._allocations:
            if owner == service_name:
                continue
            if (
                self._service_pool(owner) != pool
                or policy == PreemptionPolicy.NON_PREEMPTIVE
                or self._effective_policy(owner) == PreemptionPolicy.NON_PREEMPTIVE
            ):
                unavailable.update(self._get_service_allocation_set(owner))
            else:
                unavailable.update(self._get_service_explicit_set(owner))
        return numa_cpus - unavailable, numa_cpus & unavailable

    def _get_service_allocation_set(self, service_name: str) -> set[int]:
        """Return current allocation set for a service (empty if none)."""
        return self._parse_cpu_ranges(self._allocations.get(service_name, ""))

    def _get_service_explicit_set(self, service_name: str) -> set[int]:
        """Return current explicit allocation set for a service (empty if none)."""
        return self._parse_cpu_ranges(self._explicit_allocations.get(service_name, ""))

    @_mutating
    def _apply_numa_explicit_allocation(
        self,
        service_name: str,
        numa_node: int,
        new_cores_in_node: Set[int],
        preemption_policy: Optional[PreemptionPolicy] = None,
        *,
        pool: CpuPoolName = CpuPoolName.ISOLATED,
        eligible_cpus: AbstractSet[int],
        check_online: Callable[[AbstractSet[int]], None],
    ) -> None:
        """Validate pool, ownership and online status before changing any candidate claim."""
        self.ensure_service_pool(service_name, pool)
        policy = self._effective_policy(service_name, preemption_policy)
        eligible = get_cpus_in_numa_node(numa_node, to_ranges(sorted(eligible_cpus)))
        if not new_cores_in_node <= eligible:
            raise ValueError("Selected CPUs are outside the eligible NUMA node")
        self._check_offline_claims(service_name, eligible_cpus)
        overlaps = {
            owner: self._get_service_allocation_set(owner) & new_cores_in_node
            for owner in self._allocations
            if owner != service_name
        }
        for owner, overlap in overlaps.items():
            if overlap:
                self._validate_reclamation(owner, overlap, policy, pool)
        current = self._get_service_allocation_set(service_name)
        in_node = get_cpus_in_numa_node(numa_node, to_ranges(sorted(current)))
        remaining_explicit = self._get_service_explicit_set(service_name) - in_node
        # This guard also covers direct calls to the internal application path.
        check_online(new_cores_in_node)
        self._subtract_cpus_from_service(service_name, in_node)
        for owner, overlap in overlaps.items():
            self._subtract_cpus_from_service(
                owner,
                overlap,
                requester=service_name,
                requester_policy=policy,
                requester_pool=pool,
            )
        updated = (current - in_node) | new_cores_in_node
        if updated:
            self._allocations[service_name] = to_ranges(sorted(updated))
            self._allocated_cpus.update(updated)
            explicit = remaining_explicit | new_cores_in_node
            if explicit:
                self._explicit_allocations[service_name] = to_ranges(sorted(explicit))
                self._explicitly_allocated_cpus.update(explicit)
            self._preemption_policies[service_name] = policy
            self._allocation_pools[service_name] = pool

    @_mutating
    def allocate_numa_cores(
        self,
        service_name: str,
        numa_node: int,
        num_of_cores: int,
        preemption_policy: Optional[PreemptionPolicy] = None,
        *,
        pool: CpuPoolName = CpuPoolName.ISOLATED,
        eligible_cpus: AbstractSet[int],
        check_online: Callable[[AbstractSet[int]], None],
    ) -> Tuple[str, str]:
        """Select exactly the requested node capacity, or release that owner's node claims."""
        self.ensure_service_pool(service_name, pool)
        if num_of_cores == 0:
            return "", ""
        if num_of_cores == -1:
            in_node = get_cpus_in_numa_node(numa_node, self._allocations.get(service_name, ""))
            self._subtract_cpus_from_service(service_name, in_node)
            return "", ""
        if num_of_cores < -1:
            raise ValueError("Invalid NUMA core count")
        policy = self._effective_policy(service_name, preemption_policy)
        self._check_offline_claims(service_name, eligible_cpus)
        candidates, rejected = self._get_allocatable_numa_cpus(
            service_name, numa_node, eligible_cpus, policy, pool
        )
        if len(candidates) < num_of_cores:
            return "", to_ranges(sorted(rejected))
        selected = (
            self._select_stable(service_name, candidates, num_of_cores)
            if policy == PreemptionPolicy.NON_PREEMPTIVE
            else self._select_numa_cpus_smt_aware(candidates, num_of_cores)
        )
        self._validate_selection(selected, candidates, num_of_cores)
        self._apply_numa_explicit_allocation(
            service_name,
            numa_node,
            selected,
            policy,
            eligible_cpus=eligible_cpus,
            check_online=check_online,
            pool=pool,
        )
        return to_ranges(sorted(selected)), ""

    def _select_numa_cpus_smt_aware(self, candidate_cpus: Set[int], num_of_cores: int) -> set[int]:
        """Select exactly num_of_cores from candidates, preferring pairs then singles."""
        groups = self._group_candidates_by_siblings(candidate_cpus)
        selected_list = self._select_from_groups_pairs_then_singles(groups, num_of_cores)
        return set(sorted(selected_list))

    def _group_candidates_by_siblings(self, candidate_cpus: Set[int]) -> list[list[int]]:
        """Group candidate CPUs by their thread sibling sets.

        Returns a list of groups, each a sorted list of CPUs, ordered by the group's lowest CPU id.
        Only CPUs present in candidate_cpus are included in groups.
        """
        if not candidate_cpus:
            return []
        mapping = get_thread_siblings_map(set(candidate_cpus))
        ungrouped = set(candidate_cpus)
        groups: list[list[int]] = []
        for cpu in sorted(candidate_cpus):
            if cpu not in ungrouped:
                continue
            # A partial sysfs read can disagree with a sibling's singleton fallback.
            # Assign each candidate exactly once, regardless of that disagreement.
            members = (mapping.get(cpu, {cpu}) | {cpu}) & ungrouped
            groups.append(sorted(members))
            ungrouped -= members
        return groups

    def _select_from_groups_pairs_then_singles(
        self, groups: list[list[int]], count: int
    ) -> list[int]:
        """Pick CPUs by taking pairs from groups first, then singles.

        Mutates the input groups (consumes members) to keep the logic simple.
        """
        selected: list[int] = []
        remaining = count

        pair_taken = self._take_pairs_from_groups(groups, remaining)
        selected.extend(pair_taken)
        remaining -= len(pair_taken)

        if remaining > 0:
            single_taken = self._take_singles_from_groups(groups, remaining)
            selected.extend(single_taken)
            remaining -= len(single_taken)

        if remaining > 0:
            leftovers = self._collect_leftovers(groups)
            selected.extend(leftovers[:remaining])

        return selected[:count]

    def _take_pairs_from_groups(self, groups: list[list[int]], budget: int) -> list[int]:
        """Consume up to budget CPUs in pairs (2 per group) from groups."""
        taken: list[int] = []
        if budget < 2:
            return taken
        remaining = budget
        for members in groups:
            if remaining < 2:
                break
            if len(members) >= 2:
                taken.extend(members[:2])
                del members[:2]
                remaining -= 2
        return taken

    def _take_singles_from_groups(self, groups: list[list[int]], budget: int) -> list[int]:
        """Consume up to budget CPUs one-by-one across groups."""
        taken: list[int] = []
        if budget <= 0:
            return taken
        remaining = budget
        for members in groups:
            if remaining == 0:
                break
            if members:
                taken.append(members.pop(0))
                remaining -= 1
        return taken

    def _collect_leftovers(self, groups: list[list[int]]) -> list[int]:
        """Flatten remaining members across all groups in order."""
        leftovers: list[int] = []
        for members in groups:
            leftovers.extend(members)
        return leftovers

    def get_allocation(self, service_name: str) -> Optional[str]:
        """Get the allocated cores for a specific service.

        Args:
            service_name: Name of the service

        Returns:
            Comma-separated list of CPU ranges allocated to the service, or None if not found
        """
        self._load_from_store()
        return self._allocations.get(service_name)

    def get_all_allocations(self, pool: Optional[CpuPoolName] = None) -> list[SnapAllocation]:
        """Get all service allocations.

        Returns:
            List of SnapAllocation objects
        """
        self._load_from_store()
        return [
            SnapAllocation(
                service_name=service_name,
                pool=self._service_pool(service_name),
                allocated_cores=cores,
                cores_count=len(self._parse_cpu_ranges(cores)),
                is_explicit=service_name in self._explicit_allocations,
                preemption_policy=self._effective_policy(service_name),
            )
            for service_name, cores in self._allocations.items()
            if pool is None or self._service_pool(service_name) == pool
        ]

    @_mutating
    def remove_allocation(
        self, service_name: str, *, pool: CpuPoolName = CpuPoolName.ISOLATED
    ) -> bool:
        """Remove allocation for a specific service.

        Args:
            service_name: Name of the service

        Returns:
            True if allocation was removed, False if not found
        """
        self._load_from_store()
        self.ensure_service_pool(service_name, pool)
        if service_name in self._allocations or service_name in self._explicit_allocations:
            self._remove_service_allocation(service_name)
            logging.info(f"Removed allocation for service {service_name}")
            return True
        return False

    @_mutating
    def clear_all_allocations(self) -> None:
        """Clear all allocations."""
        self._load_from_store()
        self._allocations.clear()
        self._allocated_cpus.clear()
        self._explicit_allocations.clear()
        self._explicitly_allocated_cpus.clear()
        logging.info("Cleared all allocations")
        self._preemption_policies.clear()
        self._allocation_pools.clear()

    def get_claimed_cpus(self, pool: Optional[CpuPoolName] = None) -> set[int]:
        """Return all recorded claims, without reloading inside a transaction."""
        self._load_from_store()
        return set().union(
            *(
                self._parse_cpu_ranges(cores)
                for name, cores in self._allocations.items()
                if pool is None or self._service_pool(name) == pool
            )
        )

    def get_snap_allocation_count(self, service_name: str) -> int:
        """Get the number of CPUs allocated to a specific service.

        Args:
            service_name: Name of the service

        Returns:
            Number of CPUs allocated to the service, or 0 if not found
        """
        self._load_from_store()
        allocation = self._allocations.get(service_name)
        if allocation:
            return len(self._parse_cpu_ranges(allocation))
        return 0

    def get_system_stats(self, total_cpus: str) -> dict[str, int]:
        """Get system statistics for CPU allocation.

        Args:
            total_cpus: Comma-separated list of all available CPU ranges

        Returns:
            Dictionary with system statistics
        """
        self._load_from_store()
        total_available = len(self._parse_cpu_ranges(total_cpus))
        total_allocated = len(self._allocated_cpus)
        remaining_available = len(self._parse_cpu_ranges(total_cpus) - self._allocated_cpus)

        return {
            "total_available_cpus": total_available,
            "total_allocated_cpus": total_allocated,
            "remaining_available_cpus": remaining_available,
            "total_allocations": len(self._allocations),
        }


allocations_db: AllocationsDB = AllocationsDB()
