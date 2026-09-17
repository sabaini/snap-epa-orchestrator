# SPDX-FileCopyrightText: 2026 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Wiring regression: bin/daemon must thread the startup pool into every request."""

import importlib.machinery
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import Mock

from epa_orchestrator.cpu_pool import CpuPoolProvider

DAEMON_PATH = Path(__file__).parents[2] / "bin" / "daemon"


def _load_daemon_module():
    """Load the extensionless bin/daemon script as a module."""
    loader = importlib.machinery.SourceFileLoader("epa_orchestrator_daemon", str(DAEMON_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _request(socket_path, action, **params):
    """Send one request per connection, mirroring the daemon's one-shot protocol."""
    payload = {"action": action, "service_name": "daemon-main-test", **params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(socket_path)
        client.sendall(json.dumps(payload).encode())
        return json.loads(client.recv(65536))


def test_daemon_main_wires_startup_pool_into_requests(mock_cpu_files_empty, monkeypatch):
    """main() must pass the startup pool to startup validation and the dispatcher.

    With an empty isolated list, dropping the provider argument would silently fall
    back to the lazy isolated-mode provider: the listing would report "isolated" and
    the grant would fail with "No CPUs available", failing this test loudly.
    """
    daemon = _load_daemon_module()
    provider = CpuPoolProvider("2-5")
    validate = Mock()
    monkeypatch.setattr(daemon, "load_startup_pool", lambda: provider)
    monkeypatch.setattr(daemon, "validate_startup_pool", validate)

    errors = []

    def run():
        try:
            daemon.main()
        except Exception as exc:  # surfaced by the readiness loop below
            errors.append(exc)

    threading.Thread(target=run, daemon=True).start()

    deadline = time.monotonic() + 5
    while True:
        if errors:
            raise errors[0]
        try:
            listing = _request(daemon.SOCKET_PATH, "list_allocations")
            break
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)

    validate.assert_called_once_with(provider)
    assert listing["cpu_pool"] == {
        "source": "configured",
        "configured_cpus": "2-5",
        "eligible_cpus": "2-5",
        "unavailable_allocated_cpus": "",
    }
    assert "non-preemptive-allocations" in listing["supported_cpu_features"]

    granted = _request(daemon.SOCKET_PATH, "allocate_cores", num_of_cores=1)
    assert "error" not in granted, granted
    assert granted["allocated_cores"] == "2"
    assert granted["total_available_cpus"] == 4
