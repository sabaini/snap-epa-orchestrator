# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Validate configured-pool functional assertions without requiring an installed snap."""

import json

import pytest

from epa_orchestrator.cpu_pool import CpuPoolProvider
from epa_orchestrator.daemon_handler import handle_daemon_request
from tests.functional import test_cpu_pool as functional


@pytest.mark.parametrize(
    "action,params,field",
    [
        ("allocate_cores", {"num_of_cores": 1}, "allocated_cores"),
        ("allocate_cores_percent", {"percent": 1}, "allocated_cores"),
        ("allocate_numa_cores", {"num_of_cores": 1, "numa_node": 0}, "cores_allocated"),
    ],
)
def test_functional_expectations_for_large_pool(monkeypatch, action, params, field):
    """The functional check must accept ceiling-rounded percentages above 100 CPUs."""
    cpus = set(range(101))
    provider = CpuPoolProvider("0-100")
    monkeypatch.setattr("epa_orchestrator.utils.get_numa_node_cpus", lambda: {0: cpus})
    monkeypatch.setattr("epa_orchestrator.daemon_handler.get_numa_node_cpus", lambda: {0: cpus})

    def request(socket_path, action, **fields):
        return json.loads(
            handle_daemon_request(
                json.dumps(
                    {
                        "action": action,
                        "service_name": "test-configured-pool",
                        **fields,
                    }
                ).encode(),
                provider,
            )
        )

    monkeypatch.setattr(functional, "_request", request)
    functional.test_configured_pool_success(None, cpus, action, params, field)
