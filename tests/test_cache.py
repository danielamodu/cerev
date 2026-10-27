"""Tests for query result caching and inspect_query_result semantics."""

from datetime import UTC, datetime, timedelta

from cerev_mcp.cache import QueryResultCache


def test_cache_store_and_retrieve():
    cache = QueryResultCache(max_size=5, ttl_seconds=60)
    cache.store(
        query_id="q-1",
        request_id="req-1",
        sql="SELECT 1",
        headers=["a", "b"],
        rows=[["1", "2"], ["3", "4"], ["5", "6"]],
    )

    status, entry = cache.get("q-1")
    assert status == "AVAILABLE"
    assert entry is not None
    assert entry.row_count == 3
    assert entry.headers == ("a", "b")
    assert entry.slice_rows(0, 2) == (("1", "2"), ("3", "4"))
    assert entry.slice_rows(2, 2) == (("5", "6"),)


def test_cache_unavailable_status():
    cache = QueryResultCache(max_size=5, ttl_seconds=60)
    status, entry = cache.get("non-existent-id")
    assert status == "UNAVAILABLE"
    assert entry is None


def test_cache_expiration():
    cache = QueryResultCache(max_size=5, ttl_seconds=10)
    past_time = datetime.now(UTC) - timedelta(seconds=20)
    cache.store(
        query_id="q-old",
        request_id="req-old",
        sql="SELECT 1",
        headers=["a"],
        rows=[["1"]],
        executed_at=past_time,
    )

    status, entry = cache.get("q-old")
    assert status == "EXPIRED"
    assert entry is None


def test_cache_lru_eviction():
    cache = QueryResultCache(max_size=2, ttl_seconds=60)
    cache.store("q-1", "req-1", "SELECT 1", ["a"], [["1"]])
    cache.store("q-2", "req-2", "SELECT 2", ["a"], [["2"]])
    # Storing 3rd item should evict oldest (q-1)
    cache.store("q-3", "req-3", "SELECT 3", ["a"], [["3"]])

    status1, _ = cache.get("q-1")
    assert status1 == "EXPIRED"  # Tracked as expired/evicted

    status2, entry2 = cache.get("q-2")
    assert status2 == "AVAILABLE"
    assert entry2 is not None

    status3, entry3 = cache.get("q-3")
    assert status3 == "AVAILABLE"
    assert entry3 is not None
