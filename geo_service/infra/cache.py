"""
TTLCache — thread-safe in-memory cache.
ttl_seconds=3600 per Section 7.4 / 23.6.
No external dependency (no Redis); all Day 3 caching is in-process.
"""
import threading
import time
from typing import Any


class TTLCache:
    """
    Thread-safe TTL cache backed by a plain dict.
    Eviction is lazy: expired entries are removed on get(), not on a background timer.
    Single-process scope — no cross-worker sharing (acceptable for single-container Day 3).
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, value)
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """Return cached value or None if absent / expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        """Store value with TTL expiry."""
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    @property
    def size(self) -> int:
        """Number of entries including expired (not yet evicted)."""
        return len(self)

    def purge_expired(self) -> int:
        """Manually evict all expired entries. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (exp, _) in self._store.items() if now > exp]
            for k in expired:
                del self._store[k]
        return len(expired)
