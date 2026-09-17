# SPDX-FileCopyrightText: 2024 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Persistent state storage for EPA Orchestrator.

Provides a simple JSON-backed store with:
- Exclusive file locking (fcntl) to serialize access across processes
- Atomic writes (write to temp file then replace) to avoid torn writes
- Sectioned updates (so independent modules can update their own section)

The state file lives under $SNAP_DATA/data/state.json when SNAP_DATA is set.
Outside snap, it falls back to ~/.local/share/epa-orchestrator/data/state.json.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, Optional, TypeVar

T = TypeVar("T")


class StateCorruptionError(Exception):
    """Raised when the persisted state file is detected as corrupt/invalid JSON."""


class StateUncertainError(Exception):
    """Mutations are blocked until observed state durability is recovered."""


def _default_base_dir() -> str:
    snap_data = os.environ.get("SNAP_DATA")
    if snap_data:
        return snap_data
    # Fallback for non-snap environments (tests/dev)
    return os.path.join(os.path.expanduser("~"), ".local", "share", "epa-orchestrator")


class StateStore:
    """JSON-backed state store with file locking and atomic writes.

    The state is a JSON object with structure like:
    {
        "version": 1,
        "updated_at": "...",
        "allocations_db": { ... },
        "hugepages_db": { ... }
    }
    """

    _uncertain_paths: set[str] = set()

    def __init__(self, *, filename: str = "state.json", subdir: str = "data") -> None:
        """Initialize the store paths and ensure the base directory exists."""
        base_dir = _default_base_dir()
        self._dir_path = os.path.join(base_dir, subdir)
        self._file_path = os.path.join(self._dir_path, filename)
        self._lock_path = f"{self._file_path}.lock"
        # Enable persistence by default in all environments.
        self._disabled = False
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            os.makedirs(self._dir_path, exist_ok=True)
        except Exception as e:
            logging.error(f"Failed to ensure state directory {self._dir_path}: {e}")
            raise

    @contextmanager
    def _locked(self) -> Generator[None, None, None]:
        fd: Optional[int] = None
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
            except Exception:
                # Best-effort unlock/close
                pass

    def _read_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                obj: object = json.load(f)
                if not isinstance(obj, dict):
                    raise ValueError("State root must be an object")
                return obj
        except Exception as e:
            # Treat invalid JSON as fatal corruption: raise to crash the daemon
            raise StateCorruptionError(
                f"State file is corrupt or invalid JSON: {self._file_path}"
            ) from e

    def _atomic_write_unlocked(self, data: Dict[str, Any]) -> None:
        self._check_health_unlocked()
        temp_fd = None
        temp_path = None
        replaced = False
        marked = False
        try:
            # Write to a temp file in the same directory for atomic replace
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self._dir_path, prefix=".state.", suffix=".tmp"
            )
            with os.fdopen(temp_fd, "w", encoding="utf-8") as tmp_fp:
                temp_fd = None  # fd owned by tmp_fp now
                json.dump(data, tmp_fp, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                tmp_fp.flush()
                os.fsync(tmp_fp.fileno())
            self._set_uncertain_unlocked(True)
            marked = True
            os.replace(temp_path, self._file_path)
            replaced = True
            self._sync_directory()
            self._set_uncertain_unlocked(False)
        except Exception as e:
            logging.error(f"Failed to atomically write state file {self._file_path}: {e}")
            if replaced:
                raise StateUncertainError(
                    "State commit uncertain; recover storage before mutating"
                ) from e
            if marked:
                self._set_uncertain_unlocked(False)
            raise
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except Exception:
                    pass
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        logging.debug(f"Cleanup temp file failed: {temp_path}: {e}")

    def _sync_directory(self) -> None:
        fd = os.open(self._dir_path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _check_health_unlocked(self) -> None:
        if self._lock_path in self._uncertain_paths:
            raise StateUncertainError("State commit uncertain; explicit recovery required")
        with open(self._lock_path, "rb") as lock:
            if lock.read():
                raise StateUncertainError("State commit uncertain; explicit recovery required")

    def _set_uncertain_unlocked(self, uncertain: bool) -> None:
        # The lock inode is never replaced: all processes observe the same latch.
        self._uncertain_paths.add(self._lock_path)
        try:
            with open(self._lock_path, "r+b") as lock:
                if uncertain:
                    lock.write(b"uncertain\n")
                else:
                    lock.truncate(0)
                lock.flush()
                os.fsync(lock.fileno())
            self._sync_directory()
        except Exception:
            # Keep a visible latch even when its durability cannot be confirmed.
            with open(self._lock_path, "r+b", buffering=0) as lock:
                lock.write(b"uncertain\n")
            raise
        if not uncertain:
            self._uncertain_paths.discard(self._lock_path)

    def recover(self) -> None:
        """Durably retain observed state and unblock writes after operator reconciliation."""
        with self._locked():
            self._read_unlocked()
            if os.path.exists(self._file_path):
                with open(self._file_path, "rb") as state:
                    os.fsync(state.fileno())
            self._sync_directory()
            self._set_uncertain_unlocked(False)

    def transaction_section(
        self, section: str, update: Callable[[Dict[str, Any]], tuple[Dict[str, Any], T]]
    ) -> T:
        """Read, validate and replace one section under the same exclusive lock."""
        with self._locked():
            self._check_health_unlocked()
            state = self._read_unlocked()
            content = state.get(section, {})
            if not isinstance(content, dict):
                raise StateCorruptionError(f"Invalid state section: {section}")
            replacement, result = update(dict(content))
            state[section] = replacement
            state.setdefault("version", 1)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._atomic_write_unlocked(state)
            return result

    def read_all(self) -> Dict[str, Any]:
        """Read the entire state under an exclusive lock."""
        with self._locked():
            return self._read_unlocked()

    def write_all(self, data: Dict[str, Any]) -> None:
        """Write the entire state under an exclusive lock."""
        data = dict(data or {})
        data.setdefault("version", 1)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._locked():
            self._atomic_write_unlocked(data)

    def read_section(self, section: str) -> Dict[str, Any]:
        """Read a single top-level section dictionary from the state file."""
        with self._locked():
            state = self._read_unlocked()
            sec = state.get(section, {})
            if not isinstance(sec, dict):
                raise StateCorruptionError(f"Invalid state section: {section}")
            return dict(sec)

    def update_section(self, section: str, content: Dict[str, Any]) -> None:
        """Atomically update a single top-level section, preserving others."""
        self.transaction_section(section, lambda current: (dict(content), None))
