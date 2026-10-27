"""In-memory, bounded, TTL-based LRU query result cache for inspect_query_result."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class CachedQueryResult:
    """Stored immutable snapshot of an executed query result."""

    query_id: str
    request_id: str
    sql: str
    headers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    total: int | None
    time_used_ms: int | None
    executed_at: datetime
    expires_at: datetime

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0

    def slice_rows(self, offset: int, limit: int) -> tuple[tuple[Any, ...], ...]:
        """Return a bounded window of rows."""
        if offset < 0:
            offset = 0
        if limit < 1:
            limit = 1
        return self.rows[offset : offset + limit]


class QueryResultCache:
    """Thread-safe, bounded LRU result cache with TTL expiration tracking."""

    def __init__(self, max_size: int = 100, ttl_seconds: int = 1800) -> None:
        self.max_size = max_size
        self.ttl = timedelta(seconds=ttl_seconds)
        self._cache: OrderedDict[str, CachedQueryResult] = OrderedDict()
        self._expired_ids: set[str] = set()
        self._lock = threading.Lock()

    def store(
        self,
        query_id: str,
        request_id: str,
        sql: str,
        headers: list[str],
        rows: list[list[Any]],
        *,
        total: int | None = None,
        time_used_ms: int | None = None,
        executed_at: datetime | None = None,
    ) -> CachedQueryResult:
        """Store an executed query result into the cache."""
        now = executed_at or datetime.now(UTC)
        expires_at = now + self.ttl

        frozen_headers = tuple(headers)
        frozen_rows = tuple(tuple(row) for row in rows)

        entry = CachedQueryResult(
            query_id=query_id,
            request_id=request_id,
            sql=sql,
            headers=frozen_headers,
            rows=frozen_rows,
            row_count=len(frozen_rows),
            total=total,
            time_used_ms=time_used_ms,
            executed_at=now,
            expires_at=expires_at,
        )

        with self._lock:
            self._purge_expired_locked(now)

            # If replacing an existing key
            if query_id in self._cache:
                self._cache.move_to_end(query_id)
            elif len(self._cache) >= self.max_size:
                # Evict oldest LRU item
                evicted_id, _ = self._cache.popitem(last=False)
                self._expired_ids.add(evicted_id)
                # Keep expired IDs bounded
                if len(self._expired_ids) > self.max_size * 2:
                    self._expired_ids.pop()

            self._cache[query_id] = entry
            self._expired_ids.discard(query_id)

        return entry

    def get(self, query_id: str) -> tuple[str, CachedQueryResult | None]:
        """Retrieve a cached query result by query_id.

        Returns:
            (status, entry):
            - ("AVAILABLE", entry)
            - ("EXPIRED", None)
            - ("UNAVAILABLE", None)
        """
        now = datetime.now(UTC)
        with self._lock:
            entry = self._cache.get(query_id)
            if entry is not None:
                if now > entry.expires_at:
                    # Expired
                    del self._cache[query_id]
                    self._expired_ids.add(query_id)
                    return "EXPIRED", None
                # Move to end for LRU freshness
                self._cache.move_to_end(query_id)
                return "AVAILABLE", entry

            if query_id in self._expired_ids:
                return "EXPIRED", None

            return "UNAVAILABLE", None

    def _purge_expired_locked(self, now: datetime) -> None:
        """Remove expired entries."""
        expired = [qid for qid, entry in self._cache.items() if now > entry.expires_at]
        for qid in expired:
            del self._cache[qid]
            self._expired_ids.add(qid)
