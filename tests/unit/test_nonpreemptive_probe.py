# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Safety and readiness checks for the installed-snap regression probe."""

from types import SimpleNamespace

import pytest

from tests.scripts.nonpreemptive_probe import Probe


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
    import json
    import os
    from pathlib import Path

    from epa_orchestrator.allocations_db import allocations_db
    from epa_orchestrator.cpu_pool import CpuPoolProvider
    from epa_orchestrator.daemon_handler import handle_daemon_request
    from tests.scripts.nonpreemptive_probe import OWNERS

    provider = CpuPoolProvider("0-1")
    probe = Probe(SimpleNamespace(state=Path(allocations_db._state_store._file_path)))
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
