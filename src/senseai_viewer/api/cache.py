# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""A small thread-safe in-memory cache with a TTL and a byte budget (LRU eviction)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["TTLCache"]

T = TypeVar("T")


class TTLCache:
    """Cache values for ``ttl_s`` seconds, evicting least-recently-used entries over budget."""

    def __init__(
        self,
        ttl_s: float,
        max_bytes: int = 256 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_bytes = max_bytes
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get_or_set(
        self, key: str, fn: Callable[[], T], size: Callable[[T], int] | None = None
    ) -> T:
        """Return the cached value of ``key``, or compute it with ``fn`` and cache it.

        ``None`` results are cached too (a missing object stays missing until the TTL expires).
        """
        now = self._clock()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] > now:
                self._data.move_to_end(key)
                value: T = hit[1]
                return value
        value = fn()
        if self.ttl_s <= 0:
            return value
        n = size(value) if size is not None else 64
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
            if n <= self.max_bytes:
                self._data[key] = (now + self.ttl_s, value, n)
                self._bytes += n
            while self._bytes > self.max_bytes and self._data:
                _, (_, _, sz) = self._data.popitem(last=False)
                self._bytes -= sz
        return value

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def __len__(self) -> int:
        return len(self._data)
