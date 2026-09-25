# SPDX-FileCopyrightText: 2026 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""CPU pool configuration, discovery and online eligibility.

A provider freezes the configured IDs for its lifetime. Only online status is
refreshed for each request; no configuration or read failure widens the pool.
Present-topology membership is enforced on operator input by the configure hook,
not at daemon startup, so a CPU that disappears behaves like an offline CPU
instead of preventing the daemon from serving introspection and releases.
"""

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Literal, Optional

from . import cpu_pinning
from .schemas import CpuPoolName
from .utils import to_ranges

ONLINE_CPUS_PATH = "/sys/devices/system/cpu/online"
VALIDATED_POOL_OPTION = "internal.validated-cpu-pool"
# Bound range expansion when membership in the present topology is not enforced.
MAX_CPU_ID = 65535


def parse_cpu_list(
    value: str, present: Optional[AbstractSet[int]] = None, *, allow_empty: bool = False
) -> frozenset[int]:
    """Parse IDs/ranges, checking bounds before expanding user input."""
    if allow_empty and not value.strip():
        return frozenset()
    cpus: set[int] = set()
    for component in value.split(","):
        match = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", component.strip())
        if not match:
            raise ValueError(f"Invalid cpu-pool CPU list component: {component!r}")
        start = int(match[1])
        end = int(match[2]) if match[2] is not None else start
        if start > end:
            raise ValueError(f"Invalid cpu-pool descending range: {component!r}")
        if present is None:
            if end > MAX_CPU_ID:
                raise ValueError(f"cpu-pool CPU ID exceeds {MAX_CPU_ID}: {component!r}")
        elif start not in present or end not in present or end - start + 1 > len(present):
            raise ValueError(f"cpu-pool range is outside present CPU topology: {component!r}")
        ids = set(range(start, end + 1))
        if present is not None and not ids <= present:
            raise ValueError(f"cpu-pool includes CPUs not present: {component!r}")
        cpus.update(ids)
    return frozenset(cpus)


def read_cpu_list(path: str) -> frozenset[int]:
    """Read required sysfs data without conflating an I/O error with an empty list."""
    try:
        value = Path(path).read_text()
    except OSError as exc:
        raise ValueError(f"Failed to read CPU topology {path}: {exc}") from exc
    return parse_cpu_list(value, allow_empty=True)


def read_snap_cpu_pool() -> Optional[str]:
    """Read the committed (or hook transaction's proposed) snap option once.

    Document output distinguishes an unset/null key from an explicit empty string.
    snap set can encode a single CPU ID as a JSON integer as well as a string.
    """
    return _read_snap_pool_option("cpu-pool")


def _read_snap_pool_option(key: str) -> Optional[str]:
    """Read a pool option, including the hook's transactionally saved validation record."""
    try:
        result = subprocess.run(
            ["snapctl", "get", "-d", key],
            check=True,
            capture_output=True,
            text=True,
        )
        document = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to read snap cpu-pool configuration: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("Invalid snap cpu-pool configuration document")
    value = document.get(key)
    if type(value) is int:
        return str(value)
    if value is not None and not isinstance(value, str):
        raise ValueError("cpu-pool must be 'isolated' or a CPU list")
    return value


@dataclass(frozen=True)
class CpuPoolSnapshot:
    """One request's configured pool and online eligible subset."""

    source: Literal["isolated", "configured"]
    configured_cpus: frozenset[int]
    eligible_cpus: frozenset[int]


class CpuPoolProvider:
    """Freeze a pool choice, using only sysfs (not snapd) by default."""

    def __init__(self, configuration: Optional[str] = None) -> None:
        """Freeze the configured IDs, retaining offline and absent CPUs."""
        self.source: Literal["isolated", "configured"]
        if configuration is None or configuration.strip() == "isolated":
            self.source = "isolated"
            self.configured_cpus = parse_cpu_list(
                cpu_pinning.get_isolated_cpus(), allow_empty=True
            )
        else:
            self.source = "configured"
            self.configured_cpus = parse_cpu_list(configuration)

    @classmethod
    def without_capacity(
        cls, source: Literal["isolated", "configured"] = "isolated"
    ) -> "CpuPoolProvider":
        """Build a zero-capacity pool so listing and releases survive failed discovery."""
        provider = cls.__new__(cls)
        provider.source = source
        provider.configured_cpus = frozenset()
        return provider

    def snapshot(self, *, allow_unavailable: bool = False) -> CpuPoolSnapshot:
        """Refresh capacity; inspection and release may conservatively report none."""
        try:
            online = read_cpu_list(ONLINE_CPUS_PATH)
        except ValueError as exc:
            if not allow_unavailable:
                raise
            logging.warning("CPU capacity unavailable; reporting zero eligible CPUs: %s", exc)
            online = frozenset()
        return CpuPoolSnapshot(
            self.source,
            self.configured_cpus,
            self.configured_cpus & online,
        )

    def validate_claims(self, claimed: AbstractSet[int]) -> None:
        """Reject a configured pool which excludes owners, even if they are offline."""
        excluded = claimed - self.configured_cpus
        if excluded:
            raise ValueError(
                f"cpu-pool excludes allocated CPUs: {to_ranges(sorted(excluded))}; "
                "release or migrate these allocations before changing the pool"
            )

    def check_online(self, selected: AbstractSet[int]) -> None:
        """Check the chosen CPUs immediately before mutation; failures are retryable."""
        unavailable = selected - read_cpu_list(ONLINE_CPUS_PATH)
        if unavailable:
            raise ValueError(
                f"Selected CPUs became unavailable: {to_ranges(sorted(unavailable))}; "
                "retry the allocation request"
            )


class CpuPools:
    """Keep the legacy isolated pool separate from an opt-in general CPU pool."""

    def __init__(self, configuration: Optional[str] = None) -> None:
        """Freeze both pools; explicit configuration only supplies general CPUs."""
        self.isolated = CpuPoolProvider()
        self.general: Optional[CpuPoolProvider] = None
        if configuration is not None and configuration.strip() != "isolated":
            self.general = CpuPoolProvider(configuration)
            self.validate_disjoint()

    def select(self, pool: CpuPoolName) -> CpuPoolProvider:
        """Select exactly the requested pool, never substituting another one."""
        pool = CpuPoolName(pool)
        if pool == CpuPoolName.ISOLATED:
            return self.isolated
        if self.general is None:
            raise ValueError("CPU pool general is not configured")
        return self.general

    def validate_disjoint(self) -> None:
        """Reject general CPU IDs which the kernel reserves for the isolated pool."""
        if self.general is not None:
            overlap = self.isolated.configured_cpus & self.general.configured_cpus
            if overlap:
                raise ValueError(
                    f"General cpu-pool overlaps isolated CPUs: {to_ranges(sorted(overlap))}"
                )


def load_startup_pool() -> CpuPools:
    """Load pools independently, retaining recovery access after discovery failures."""
    pools = CpuPools.__new__(CpuPools)
    isolation_known = True
    try:
        pools.isolated = CpuPoolProvider()
    except Exception as exc:
        logging.error("CPU pool discovery failed for isolated: %s", exc)
        pools.isolated = CpuPoolProvider.without_capacity()
        isolation_known = False
    pools.general = None
    try:
        configuration = read_snap_cpu_pool()
        if configuration is not None and configuration.strip() != "isolated":
            pools.general = CpuPoolProvider(configuration)
            if not isolation_known:
                raise ValueError("Cannot validate general pool without isolated CPU topology")
            pools.validate_disjoint()
    except Exception as exc:
        logging.error("CPU pool discovery failed for general: %s", exc)
        # Unknown/invalid configuration is unavailable, never an alternate pool.
        # Keep the named recovery endpoint available for existing general claims.
        pools.general = CpuPoolProvider.without_capacity("configured")
    return pools


def validate_snap_configuration() -> None:
    """Configure hook: validate proposed configuration against topology and claims."""
    # Import here so parser/provider users do not initialize a state database.
    from .allocations_db import allocations_db

    configuration = read_snap_cpu_pool()
    accepted = _read_snap_pool_option(VALIDATED_POOL_OPTION)
    normalized = "isolated"
    if configuration is not None and configuration.strip() != "isolated":
        normalized = to_ranges(sorted(parse_cpu_list(configuration)))
        if normalized != accepted:
            # Only new input requires present CPUs. Refresh can replay an accepted
            # pool after hardware disappears; startup retains those IDs as unavailable.
            parse_cpu_list(configuration, read_cpu_list(cpu_pinning.PRESENT_CPUS_PATH))
    pools = CpuPools(configuration)
    for pool in CpuPoolName:
        claimed = allocations_db.get_claimed_cpus(pool)
        if pool == CpuPoolName.GENERAL and pools.general is None and not claimed:
            continue
        pools.select(pool).validate_claims(claimed)
    if normalized != accepted:
        # snapctl writes join the configure-hook transaction, so failed changes
        # cannot replace the last accepted pool. Do not use a separate state file.
        try:
            subprocess.run(
                ["snapctl", "set", f"{VALIDATED_POOL_OPTION}={json.dumps(normalized)}"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"Failed to record validated cpu-pool configuration: {exc}") from exc


def main() -> None:
    """Report a clear hook error, rolling back the snap configuration transaction."""
    try:
        validate_snap_configuration()
    except Exception as exc:
        print(f"Invalid cpu-pool configuration: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
