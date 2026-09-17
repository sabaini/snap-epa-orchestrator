# SPDX-FileCopyrightText: 2026 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""CPU pool configuration, discovery and online eligibility.

A provider freezes the configured IDs for its lifetime. Only online status is
refreshed for each request; no configuration or read failure widens the pool.
"""

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Literal, Optional

from . import cpu_pinning
from .utils import to_ranges

ONLINE_CPUS_PATH = "/sys/devices/system/cpu/online"


def parse_cpu_list(
    value: str, present: Optional[AbstractSet[int]] = None, *, allow_empty: bool = False
) -> frozenset[int]:
    """Parse IDs/ranges, checking topology bounds before expanding user input."""
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
        if present is not None and (
            start not in present or end not in present or end - start + 1 > len(present)
        ):
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
    try:
        result = subprocess.run(
            ["snapctl", "get", "-d", "cpu-pool"],
            check=True,
            capture_output=True,
            text=True,
        )
        document = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to read snap cpu-pool configuration: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("Invalid snap cpu-pool configuration document")
    value = document.get("cpu-pool")
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
        """Validate configuration against present CPUs, retaining offline IDs."""
        present = read_cpu_list(cpu_pinning.PRESENT_CPUS_PATH)
        self.source: Literal["isolated", "configured"]
        if configuration is None or configuration.strip() == "isolated":
            self.source = "isolated"
            self.configured_cpus = parse_cpu_list(
                cpu_pinning.get_isolated_cpus(), present, allow_empty=True
            )
        else:
            self.source = "configured"
            self.configured_cpus = parse_cpu_list(configuration, present)

    def snapshot(self) -> CpuPoolSnapshot:
        """Refresh online status once for request selection and accounting."""
        return CpuPoolSnapshot(
            self.source,
            self.configured_cpus,
            self.configured_cpus & read_cpu_list(ONLINE_CPUS_PATH),
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


def validate_snap_configuration() -> None:
    """Configure hook: validate proposed configuration against persisted claims."""
    # Import here so parser/provider users do not initialize a state database.
    from .allocations_db import allocations_db

    provider = CpuPoolProvider(read_snap_cpu_pool())
    provider.validate_claims(allocations_db.get_claimed_cpus())


def main() -> None:
    """Report a clear hook error without committing any secondary configuration."""
    try:
        validate_snap_configuration()
    except Exception as exc:
        print(f"Invalid cpu-pool configuration: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
