# SPDX-FileCopyrightText: 2026 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""CPU pool parser, topology, snap configuration and hook regression tests."""

import json
import subprocess
from unittest.mock import patch

import pytest

from epa_orchestrator.cpu_pool import (
    MAX_CPU_ID,
    CpuPoolProvider,
    load_startup_pool,
    parse_cpu_list,
    read_snap_cpu_pool,
    validate_snap_configuration,
)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "1,",
        ",1",
        "1,,2",
        "-1",
        "4-2",
        "x",
        "1:2",
        "0-7:2/4",
        "+1",
        "1.0",
        "1--2",
        "8",
        "0-999999999999999999999",
    ],
)
def test_invalid_cpu_list(value):
    """Malformed/unbounded inputs fail before expanding ranges."""
    with pytest.raises(ValueError):
        parse_cpu_list(value, set(range(8)))


def test_normalization_and_topology_holes():
    """Whitespace, duplicates and order normalize; non-present interior IDs fail."""
    assert parse_cpu_list(" 7, 2 - 4,3,0, 2 ", set(range(8))) == {0, 2, 3, 4, 7}
    with pytest.raises(ValueError, match="not present"):
        parse_cpu_list("0-2", {0, 2, 4, 6})


@pytest.mark.parametrize("choice", [None, "isolated", " isolated "])
def test_default_pool(choice, mock_cpu_files):
    """Default configuration retains the isolated CPU selection."""
    provider = CpuPoolProvider(choice)
    snapshot = provider.snapshot()
    assert snapshot.source == "isolated"
    assert snapshot.configured_cpus == snapshot.eligible_cpus == {0, 1, 2, 3, 6, 7}
    mock_cpu_files["isolated"].write_text("4-5")
    assert provider.snapshot() == snapshot  # fixed until restart


def test_empty_isolated_no_fallback(mock_cpu_files_empty):
    """Empty isolation is a valid zero-capacity pool, never host-wide eligibility."""
    assert CpuPoolProvider().snapshot().eligible_cpus == set()
    assert CpuPoolProvider("2-4").snapshot().eligible_cpus == {2, 3, 4}


def test_offline_and_online_refresh(mock_cpu_files_empty):
    """Offline configured CPUs are retained, becoming eligible when online again."""
    mock_cpu_files_empty["online"].write_text("0-3")
    provider = CpuPoolProvider("2-5")
    assert provider.snapshot().configured_cpus == {2, 3, 4, 5}
    assert provider.snapshot().eligible_cpus == {2, 3}
    mock_cpu_files_empty["online"].write_text("0-7")
    assert provider.snapshot().eligible_cpus == {2, 3, 4, 5}


@pytest.mark.parametrize("filename", ["isolated", "online"])
def test_required_sysfs_read_failure(filename, mock_cpu_files):
    """A missing required topology file is an error, unlike empty isolation."""
    mock_cpu_files[filename].unlink()
    with pytest.raises(ValueError, match="Failed to read"):
        CpuPoolProvider().snapshot()


def test_absent_configured_cpus_are_retained_not_fatal(mock_cpu_files_empty):
    """A CPU that leaves the machine behaves exactly like an offline CPU."""
    mock_cpu_files_empty["present"].write_text("0-3")
    mock_cpu_files_empty["online"].write_text("0-3")
    provider = CpuPoolProvider("2-5")
    snapshot = provider.snapshot()
    assert snapshot.configured_cpus == {2, 3, 4, 5}
    assert snapshot.eligible_cpus == {2, 3}
    # Unreadable present topology must not stop a configured pool from loading either.
    mock_cpu_files_empty["present"].unlink()
    assert CpuPoolProvider("2-5").snapshot().eligible_cpus == {2, 3}


@pytest.mark.parametrize("value", [f"0-{MAX_CPU_ID + 1}", str(MAX_CPU_ID + 1)])
def test_unbounded_input_rejected_without_present_topology(value):
    """Dropping the membership check must not allow unbounded range expansion."""
    with pytest.raises(ValueError, match=f"exceeds {MAX_CPU_ID}"):
        parse_cpu_list(value)


@pytest.mark.parametrize(
    "failure,expected_source",
    [("snapctl", "isolated"), ("isolated", "isolated"), ("configured", "configured")],
)
def test_startup_pool_degrades_to_zero_capacity(mock_cpu_files, caplog, failure, expected_source):
    """Failed discovery keeps the daemon serving instead of aborting startup."""
    if failure == "snapctl":
        context = patch(
            "epa_orchestrator.cpu_pool.read_snap_cpu_pool", side_effect=ValueError("snapctl")
        )
    else:
        mock_cpu_files["isolated"].unlink()
        value = None if failure == "isolated" else "nonsense"
        context = patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value=value)
    with context:
        provider = load_startup_pool()
    snapshot = provider.snapshot()
    assert snapshot.source == expected_source
    assert snapshot.configured_cpus == snapshot.eligible_cpus == frozenset()
    assert "CPU pool discovery failed" in caplog.text
    provider.validate_claims(set())
    with pytest.raises(ValueError, match="excludes allocated CPUs"):
        provider.validate_claims({1})


def test_startup_pool_uses_configuration_when_readable(mock_cpu_files_empty):
    """The degradation path must not mask a usable configured pool."""
    with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value="2-4"):
        assert load_startup_pool().snapshot().eligible_cpus == {2, 3, 4}


def test_configured_mode_does_not_read_isolated(mock_cpu_files):
    """Explicit configuration has no dependency on isolated discovery."""
    mock_cpu_files["isolated"].unlink()
    assert CpuPoolProvider("2-3").snapshot().eligible_cpus == {2, 3}


@pytest.mark.parametrize(
    "document,expected",
    [
        ({}, None),
        ({"cpu-pool": None}, None),
        ({"cpu-pool": ""}, ""),
        ({"cpu-pool": "isolated"}, "isolated"),
        ({"cpu-pool": "2-3"}, "2-3"),
        ({"cpu-pool": 2}, "2"),
    ],
)
def test_snap_configuration_document(document, expected):
    """Absent keys and explicitly empty strings remain distinguishable."""
    with patch("epa_orchestrator.cpu_pool.subprocess.run") as run:
        run.return_value.stdout = json.dumps(document)
        assert read_snap_cpu_pool() == expected
        run.assert_called_once_with(
            ["snapctl", "get", "-d", "cpu-pool"], check=True, capture_output=True, text=True
        )


@pytest.mark.parametrize("value", [True, [], {}, 1.5])
def test_snap_configuration_invalid_type(value):
    """Only strings and single integer CPU IDs are accepted from snapd."""
    with patch("epa_orchestrator.cpu_pool.subprocess.run") as run:
        run.return_value.stdout = json.dumps({"cpu-pool": value})
        with pytest.raises(ValueError):
            read_snap_cpu_pool()


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("snapctl"), subprocess.CalledProcessError(1, "snapctl")]
)
def test_snap_read_failure_is_not_default(failure):
    """A snapctl failure cannot silently reset the configured pool."""
    with patch("epa_orchestrator.cpu_pool.subprocess.run", side_effect=failure):
        with pytest.raises(ValueError, match="Failed to read snap"):
            read_snap_cpu_pool()


def test_hook_claim_validation(mock_cpu_files_empty, fresh_allocations_db):
    """A hook allows supersets/offline owners but rejects shrinking and empty input."""
    fresh_allocations_db.allocate_cores("owner", "2-3")
    before = fresh_allocations_db._state_store.read_all()
    mock_cpu_files_empty["online"].write_text("0-2")
    for value in ("2-4", "2-3"):
        with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value=value):
            validate_snap_configuration()
    for value in ("2", "", None, "0-99999999999999999"):
        with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value=value):
            with pytest.raises(ValueError):
                validate_snap_configuration()
    assert fresh_allocations_db._state_store.read_all() == before


def test_hook_still_rejects_absent_cpus(mock_cpu_files_empty, fresh_allocations_db):
    """Operator input naming missing CPUs stays an error even though startup tolerates it."""
    mock_cpu_files_empty["present"].write_text("0-3")
    with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value="2-5"):
        with pytest.raises(ValueError, match="present CPU topology"):
            validate_snap_configuration()
    with patch("epa_orchestrator.cpu_pool.read_snap_cpu_pool", return_value="2-3"):
        validate_snap_configuration()
