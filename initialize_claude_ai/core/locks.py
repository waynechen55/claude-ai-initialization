"""One lock per key, for state that threads working on different things share.

Lives in ``core`` so that anything below the phases -- the trust store the HTTP
client reads, as much as phase 4's pipelines -- can serialise by *what* is
shared without importing a phase's package.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable


class KeyedLocks:
    """One lock per key, created on first use and kept for the run.

    ``with locks(key):`` serialises the callers that share ``key`` and leaves
    every other key alone -- two addresses installing the same package wait for
    each other, two installing different packages do not.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Hashable, threading.RLock] = {}

    def __call__(self, key: Hashable) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                # Re-entrant, so a guarded step that reaches the same guard
                # again -- a repair that re-runs an install -- cannot deadlock
                # its own thread.
                lock = self._locks[key] = threading.RLock()
            return lock


__all__ = ["KeyedLocks"]
