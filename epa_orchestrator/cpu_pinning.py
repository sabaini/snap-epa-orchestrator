# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Utility methods for calculating dedicated and shared vCPUs."""

import logging

from .utils import _read_file_strict, parse_cpu_ranges, to_ranges

ISOLATED_CPUS_PATH = "/sys/devices/system/cpu/isolated"
PRESENT_CPUS_PATH = "/sys/devices/system/cpu/present"
THREAD_SIBLINGS_LIST_TEMPLATE = "/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
MAX_ALLOCATION_PERCENTAGE = 80  # Maximum percentage of CPUs that can be allocated
LARGE_SYSTEM_THRESHOLD = 100  # Threshold for considering a system "large"
RESERVED_CORES_LARGE_SYSTEM = 16  # Number of cores to reserve on large systems


def get_isolated_cpus() -> str:
    """Get the list of isolated CPUs from the system file.

    Returns:
        str: Comma-separated list of CPU ranges that are isolated
    """
    try:
        with open(ISOLATED_CPUS_PATH, "r") as f:
            value = f.read().strip()
    except OSError as exc:
        raise ValueError(f"Failed to read isolated CPUs from {ISOLATED_CPUS_PATH}: {exc}") from exc
    if value:
        logging.info(f"Found isolated CPUs: {value}")
    else:
        logging.info("No Isolated CPUs configured")
    return value


def get_thread_siblings_map(cpus: set[int]) -> dict[int, set[int]]:
    """Return mapping of each CPU to its thread siblings.

    Summary:
    - Includes the CPU itself.
    - Intersected with the provided CPU set.
    - Uses thread_siblings_list from sysfs.
    - Falls back to a singleton set if topology is unavailable.
    """
    result: dict[int, set[int]] = {}
    if not cpus:
        return result

    for cpu in sorted(cpus):
        path = THREAD_SIBLINGS_LIST_TEMPLATE.format(cpu=cpu)
        content = _read_file_strict(path)
        if content:
            siblings_all = parse_cpu_ranges(content)
            siblings = siblings_all.intersection(cpus)
            result[cpu] = siblings if siblings else {cpu}
        else:
            result[cpu] = {cpu}
    return result


def calculate_cpu_pinning(cpu_list: str, cores_requested: int = 0) -> "tuple[str, str]":
    """Calculate CPU pinning configuration from isolated CPU list.

    Args:
        cpu_list: Comma-separated list of CPU ranges
        cores_requested: Number of dedicated cores requested. If 0, allocates based on system size:
            - Small systems (≤100 cores): 80% of total CPUs
            - Large systems (>100 cores): All cores except 16 reserved

    Returns:
        tuple: (cpu_shared_set, allocated_cores) where each is a comma-separated
              list of CPU ranges.

    Examples:
        >>> calculate_cpu_pinning("0-3", 2)
        ('2-3', '0-1')
        >>> calculate_cpu_pinning("0,2,4,6", 1)
        ('2,4,6', '0')
        >>> calculate_cpu_pinning("0-7", 0)  # Small system, uses 80% default
        ('6-7', '0-5')
        >>> calculate_cpu_pinning("0-39", 0)  # Large system, reserves 16 cores
        ('24-39', '0-23')
        >>> calculate_cpu_pinning("0-5", 4)
        ('4-5', '0-3')
        >>> calculate_cpu_pinning("0-9", 8)
        ('8-9', '0-7')
        >>> calculate_cpu_pinning("0-3", 5)  # More requested than available
        ('', '')
        >>> calculate_cpu_pinning("", 2)  # Empty CPU list
        ('', '')
        >>> calculate_cpu_pinning("0-3", -1)  # Negative requested
        ('0-3', '')
    """
    if not cpu_list:
        return "", ""

    # Handle negative cores_requested
    if cores_requested < 0:
        logging.warning(f"Negative cores_requested ({cores_requested}), treating as 0")
        cores_requested = 0

    cpus: set[int] = set()
    for part in cpu_list.split(","):
        if "-" in part:
            start, end = map(int, part.split("-"))
            cpus.update(range(start, end + 1))
        else:
            cpus.add(int(part))

    cpus_list = sorted(list(cpus))
    total_cpus = len(cpus_list)

    if cores_requested == 0:
        # Determine allocation strategy based on system size
        if total_cpus > LARGE_SYSTEM_THRESHOLD:
            # Large system: allocate all cores except reserved amount
            cores_requested = total_cpus - RESERVED_CORES_LARGE_SYSTEM
            logging.info(
                f"Large system detected ({total_cpus} cores > {LARGE_SYSTEM_THRESHOLD} threshold). "
                f"Allocating {cores_requested} cores (reserving {RESERVED_CORES_LARGE_SYSTEM} cores)"
            )
        else:
            # Small system: allocate 80% of total CPUs
            cores_requested = int(total_cpus * MAX_ALLOCATION_PERCENTAGE / 100)
            logging.info(
                f"Small system detected ({total_cpus} cores ≤ {LARGE_SYSTEM_THRESHOLD} threshold). "
                f"Allocating {cores_requested} cores (80% of {total_cpus} total CPUs)"
            )

    # Validate that we have enough CPUs available
    if cores_requested > total_cpus:
        logging.error(f"Requested {cores_requested} cores but only {total_cpus} available")
        return "", ""

    dedicated_cpus = cpus_list[:cores_requested]
    shared_cpus = cpus_list[cores_requested:]

    try:
        shared_str = to_ranges(shared_cpus)
        dedicated_str = to_ranges(dedicated_cpus)
    except Exception as e:
        logging.error(f"Failed to convert CPU lists to ranges: {e}")
        return "", ""
    return shared_str, dedicated_str
