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
