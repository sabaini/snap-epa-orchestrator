# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Safety and readiness checks for the installed-snap regression probe."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.scripts import nonpreemptive_probe
from tests.scripts.nonpreemptive_probe import Probe


@pytest.fixture
def socket_responder(monkeypatch):
    """Exercise Probe.request through JSON/socket handling instead of replacing it."""

    def install(respond):
        requests = []

        def socket_factory(*args):
            connection = MagicMock()
            connection.__enter__.return_value = connection

            def send(data):
                payload = json.loads(data)
                requests.append(payload)
                connection.recv.side_effect = [json.dumps(respond(payload)).encode(), b""]

            connection.sendall.side_effect = send
            return connection

        monkeypatch.setattr(nonpreemptive_probe.socket, "socket", socket_factory)
        return requests

    return install


@pytest.mark.parametrize(
    "response", [{"allocations": []}, {"supported_cpu_features": [], "allocations": []}]
)
def test_probe_refuses_old_daemon(response, monkeypatch):
    """Missing capability cannot be mistaken for support from a version string."""
    probe = Probe(SimpleNamespace())
    monkeypatch.setattr(probe, "request", lambda *args, **kwargs: response)
    with pytest.raises(AssertionError):
        probe.wait_for_allocations()


@pytest.mark.parametrize("policy", [None, "legacy"])
def test_probe_never_applies_unconfirmed_claim(policy, monkeypatch):
    """A missing or incorrect policy confirmation must fail before process affinity."""
    probe = Probe(SimpleNamespace())
    monkeypatch.setattr(probe, "request", lambda *args, **kwargs: {"preemption_policy": policy})
    applied = []
    monkeypatch.setattr(probe, "pinned_worker", lambda cpus: applied.append(cpus))
    with pytest.raises(AssertionError):
        probe.protected_round("allocate_cores", {"num_of_cores": 2}, {0, 1})
    assert not applied


def test_probe_waits_for_socket_readiness(monkeypatch):
    """A just-installed daemon may not yet have bound its socket."""
    probe = Probe(SimpleNamespace())
    attempts = []

    def allocations():
        attempts.append(True)
        if len(attempts) == 1:
            raise FileNotFoundError("socket not yet created")
        return {}

    monkeypatch.setattr(probe, "allocations", allocations)
    assert probe.wait_for_allocations() == {}
    assert len(attempts) == 2


def test_probe_readiness_timeout_is_bounded(monkeypatch):
    """Readiness never hides a permanently unavailable daemon."""
    probe = Probe(SimpleNamespace())

    def allocations():
        raise FileNotFoundError("unavailable")

    monkeypatch.setattr(probe, "allocations", allocations)
    with pytest.raises(FileNotFoundError):
        probe.wait_for_allocations(timeout=0)


@pytest.fixture
def two_cpu_probe(mock_cpu_files_empty, monkeypatch):
    """Exercise the real dispatcher while simulating immutable writes, never host commands."""
    import errno
    import os
    from pathlib import Path

    from epa_orchestrator.allocations_db import allocations_db
    from epa_orchestrator.cpu_pool import CpuPools
    from epa_orchestrator.daemon_handler import handle_daemon_request
    from tests.scripts.nonpreemptive_probe import OWNERS

    provider = CpuPools("0-1")
    probe = Probe(
        SimpleNamespace(state=Path(allocations_db._state_store._file_path), pool="general")
    )
    immutable = False
    attempts = []
    replace = os.replace

    def command(argv, check=True):
        nonlocal immutable
        assert argv == ["chattr", "+i" if not immutable else "-i", str(probe.args.state)]
        immutable = argv[1] == "+i"

    def guarded_replace(source, target):
        if immutable:
            attempts.append(json.loads(Path(source).read_text())["allocations_db"]["allocations"])
            raise PermissionError(errno.EPERM, "Operation not permitted", str(target))
        return replace(source, target)

    def request(action, owner=OWNERS[0], **fields):
        return json.loads(
            handle_daemon_request(
                json.dumps(
                    {
                        "action": action,
                        "pool": probe.pool,
                        "service_name": owner,
                        **fields,
                    }
                ).encode(),
                provider,
            )
        )

    monkeypatch.setattr(probe, "command", command)
    monkeypatch.setattr(probe, "request", request)
    monkeypatch.setattr("epa_orchestrator.state_store.os.replace", guarded_replace)
    return probe, attempts


def test_two_cpu_immutable_probe_exercises_both_writes(two_cpu_probe):
    """Both replacement and new-owner requests must reach storage with two eligible CPUs."""
    from tests.scripts.nonpreemptive_probe import OWNERS

    probe, attempts = two_cpu_probe
    probe.immutable_failure()
    assert attempts == [{OWNERS[0]: "0-1"}, {OWNERS[0]: "0", OWNERS[1]: "1"}]
    assert probe.allocations() == {}
    assert probe.report["checks"] == [
        "immutable state rejects new allocation and protected replacement"
    ]


def test_immutable_probe_rejects_unrelated_error(two_cpu_probe, monkeypatch):
    """An arbitrary error response must not be credited as immutable-state protection."""
    probe, _ = two_cpu_probe
    request = probe.request

    def unrelated_error(action, *args, **fields):
        if action == "allocate_cores" and fields.get("num_of_cores") == 2:
            return {"error": "Insufficient CPUs available"}
        return request(action, *args, **fields)

    monkeypatch.setattr(probe, "request", unrelated_error)
    with pytest.raises(AssertionError):
        probe.immutable_failure()
    assert probe.report["checks"] == []


@pytest.mark.parametrize("pool", ["isolated", "general"])
@pytest.mark.parametrize("failure", ["capability", "missing-pool", "wrong-pool", "old-style"])
def test_probe_requires_pool_support_and_confirmation(pool, failure, socket_responder):
    """Neither selector accepts an old or misdirected daemon before mutations."""
    response = {
        "supported_cpu_features": ["non-preemptive-allocations", "cpu-pools"],
        "pool": pool,
        "allocations": [],
    }
    if failure in ("capability", "old-style"):
        response["supported_cpu_features"] = ["non-preemptive-allocations"]
    if failure in ("missing-pool", "old-style"):
        response.pop("pool")
    if failure == "wrong-pool":
        response["pool"] = "general" if pool == "isolated" else "isolated"
    requests = socket_responder(lambda payload: response)
    probe = Probe(SimpleNamespace(socket="socket", pool=pool))
    with pytest.raises(AssertionError):
        probe.wait_for_allocations()
    assert len(requests) == 1
    assert requests[0]["action"] == "list_allocations"
    assert requests[0]["pool"] == pool
    assert probe.report["events"][0]["response"] == response


@pytest.mark.parametrize("pool", ["isolated", "general"])
@pytest.mark.parametrize("confirmation", ["missing", "wrong"])
@pytest.mark.parametrize(
    "action,fields,result_field",
    [
        ("allocate_cores", {"num_of_cores": 2}, "allocated_cores"),
        ("allocate_cores_percent", {"percent": 100}, "allocated_cores"),
        ("allocate_numa_cores", {"num_of_cores": 2, "numa_node": 0}, "cores_allocated"),
    ],
)
def test_probe_rejects_unconfirmed_pool_before_affinity(
    pool, confirmation, action, fields, result_field, socket_responder, monkeypatch
):
    """Matching CPU IDs and policy cannot bypass pool confirmation on any grant API."""
    grant = {"preemption_policy": "non-preemptive", result_field: "0-1"}
    if confirmation == "wrong":
        grant["pool"] = "general" if pool == "isolated" else "isolated"

    def respond(payload):
        if payload["action"] == "list_allocations":
            return {
                "pool": pool,
                "supported_cpu_features": ["non-preemptive-allocations", "cpu-pools"],
                "allocations": [],
            }
        return grant

    requests = socket_responder(respond)
    probe = Probe(SimpleNamespace(socket="socket", pool=pool))
    affinity = MagicMock(side_effect=RuntimeError("must not reach affinity"))
    monkeypatch.setattr(probe, "pinned_worker", affinity)
    with pytest.raises(AssertionError):
        probe.protected_round(action, fields, {0, 1})
    affinity.assert_not_called()
    assert len(requests) == 1
    assert probe.report["events"][0]["response"] == grant


@pytest.mark.parametrize("pool", ["isolated", "general"])
@pytest.mark.parametrize("confirmation", ["missing", "wrong"])
def test_probe_release_requires_pool_confirmation(pool, confirmation, socket_responder):
    """Release successes must confirm the original pool, unlike error responses."""
    response = {"preemption_policy": None}
    if confirmation == "wrong":
        response["pool"] = "general" if pool == "isolated" else "isolated"
    socket_responder(lambda payload: response)
    probe = Probe(SimpleNamespace(socket="socket", pool=pool))
    with pytest.raises(AssertionError):
        probe.release("owner")
    assert probe.report["events"][0]["response"] == response


@pytest.mark.parametrize("pool", ["isolated", "general"])
def test_probe_accepts_confirmed_responses_and_preserves_errors(pool, socket_responder):
    """Valid listings, grants and releases pass; expected errors retain their transcript."""

    def respond(payload):
        if payload["action"] == "list_allocations":
            return {
                "pool": pool,
                "supported_cpu_features": ["non-preemptive-allocations", "cpu-pools"],
                "allocations": [],
            }
        if payload.get("num_of_cores") == -1:
            return {"pool": pool, "preemption_policy": None}
        if payload.get("num_of_cores") == 99:
            return {"error": "Insufficient CPUs available"}
        return {"pool": pool, "allocated_cores": "0-1", "preemption_policy": "non-preemptive"}

    requests = socket_responder(respond)
    probe = Probe(SimpleNamespace(socket="socket", pool=pool))
    assert probe.allocations() == {}
    assert probe.request("allocate_cores", num_of_cores=2)["allocated_cores"] == "0-1"
    probe.release("owner")
    error = probe.request("allocate_cores", num_of_cores=99)
    assert error == {"error": "Insufficient CPUs available"}
    assert probe.report["events"][-1]["response"] == error
    assert len(requests) == len(probe.report["events"]) == 4
    assert all(request["pool"] == pool for request in requests)
