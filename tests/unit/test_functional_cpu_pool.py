# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Validate configured-pool functional assertions without requiring an installed snap."""

import json
from unittest.mock import MagicMock

import pytest

from epa_orchestrator.cpu_pool import CpuPools
from epa_orchestrator.daemon_handler import handle_daemon_request
from tests.functional import test_cpu_pool as functional


@pytest.fixture
def socket_responder(monkeypatch):
    """Feed wire responses through the functional test's real request helper."""

    def install(respond):
        requests = []

        def socket_factory(*args):
            connection = MagicMock()
            connection.__enter__.return_value = connection

            def send(data):
                payload = json.loads(data)
                requests.append(payload)
                connection.recv.return_value = json.dumps(respond(payload)).encode()

            connection.sendall.side_effect = send
            return connection

        monkeypatch.setattr(functional.socket, "socket", socket_factory)
        return requests

    return install


@pytest.mark.parametrize(
    "action,params,field",
    [
        ("allocate_cores", {"num_of_cores": 1}, "allocated_cores"),
        ("allocate_cores_percent", {"percent": 1}, "allocated_cores"),
        ("allocate_numa_cores", {"num_of_cores": 1, "numa_node": 0}, "cores_allocated"),
    ],
)
def test_functional_expectations_for_large_pool(
    monkeypatch, mock_cpu_files_empty, socket_responder, action, params, field
):
    """The functional check must accept ceiling-rounded percentages above 100 CPUs."""
    mock_cpu_files_empty["online"].write_text("0-100")
    cpus = set(range(101))
    provider = CpuPools("0-100")
    monkeypatch.setattr("epa_orchestrator.utils.get_numa_node_cpus", lambda: {0: cpus})
    monkeypatch.setattr("epa_orchestrator.daemon_handler.get_numa_node_cpus", lambda: {0: cpus})

    socket_responder(
        lambda payload: json.loads(handle_daemon_request(json.dumps(payload).encode(), provider))
    )
    functional.test_configured_pool_success(None, cpus, action, params, field)


@pytest.fixture
def functional_guest(mock_cpu_files_empty, monkeypatch):
    """Model the opt-in empty-isolation guest with the real named-pool dispatcher."""
    monkeypatch.setenv("EPA_TEST_CPU_POOL", "2-5")
    path = MagicMock()
    path.return_value.read_text.return_value = ""
    monkeypatch.setattr(functional, "Path", path)
    provider = CpuPools("2-5")

    def respond(payload):
        return json.loads(handle_daemon_request(json.dumps(payload).encode(), provider))

    return respond


@pytest.mark.parametrize(
    "selected,field,value",
    [
        ("isolated", "supported_cpu_features", None),
        ("isolated", "supported_cpu_features", ["non-preemptive-allocations"]),
        ("isolated", "pool", None),
        ("isolated", "pool", "general"),
        ("general", "pool", None),
        ("general", "pool", "isolated"),
    ],
)
def test_functional_handshake_rejects_unverified_pool_before_mutation(
    functional_guest, socket_responder, selected, field, value
):
    """Old-style or wrong-pool listings must fail before grants or cleanup releases."""

    def respond(payload):
        response = functional_guest(payload)
        if payload["action"] == "list_allocations" and payload.get("pool", "isolated") == selected:
            response.pop(field)
            if value is not None:
                response[field] = value
        return response

    requests = socket_responder(respond)
    fixture = functional.configured_pool.__wrapped__("socket")
    try:
        with pytest.raises(AssertionError):
            next(fixture)
        assert requests
        assert all(request["action"] == "list_allocations" for request in requests)
        assert "pool" not in requests[0]
    finally:
        fixture.close()


@pytest.mark.parametrize("confirmation", [None, "isolated", "general"])
def test_functional_release_requires_general_confirmation(
    functional_guest, socket_responder, confirmation
):
    """A valid handshake checks the default pool separately and verifies cleanup too."""

    def respond(payload):
        response = functional_guest(payload)
        if payload["action"] == "allocate_cores":
            response.pop("pool")
            if confirmation is not None:
                response["pool"] = confirmation
        return response

    requests = socket_responder(respond)
    fixture = functional.configured_pool.__wrapped__("socket")
    try:
        assert next(fixture) == {2, 3, 4, 5}
        assert "pool" not in requests[0]
        assert requests[1]["pool"] == "general"
        with pytest.raises(StopIteration if confirmation == "general" else AssertionError):
            next(fixture)
        assert requests[-1]["pool"] == "general"
        assert requests[-1]["num_of_cores"] == -1
    finally:
        fixture.close()


@pytest.mark.parametrize("confirmation", [None, "isolated"])
@pytest.mark.parametrize("target", ["grant", "owner"])
def test_functional_rejects_unconfirmed_grants_and_owners(
    functional_guest, socket_responder, confirmation, target
):
    """Matching CPU IDs cannot compensate for absent or incorrect ownership metadata."""

    def respond(payload):
        response = functional_guest(payload)
        entry = None
        if target == "grant" and payload["action"] == "allocate_cores":
            entry = response
        if target == "owner" and payload["action"] == "list_allocations":
            entry = response["allocations"][0]
        if entry is not None:
            entry.pop("pool")
            if confirmation is not None:
                entry["pool"] = confirmation
        return response

    socket_responder(respond)
    with pytest.raises(AssertionError):
        functional.test_configured_pool_success(
            "socket", {2, 3, 4, 5}, "allocate_cores", {"num_of_cores": 1}, "allocated_cores"
        )


def test_functional_request_preserves_error_response(socket_responder):
    """Expected daemon errors need not carry successful-response pool confirmation."""
    response = {"error": "CPU pool general is not configured"}
    socket_responder(lambda payload: response)
    assert functional._request("socket", "list_allocations", pool="general") == response
