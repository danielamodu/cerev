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


def test_cache_concurrency_multithreaded_store_and_get():
    """Verify thread safety under heavy concurrent store and retrieve operations."""
    import concurrent.futures

    cache = QueryResultCache(max_size=50, ttl_seconds=300)
    num_threads = 8
    items_per_thread = 25

    def worker(thread_idx: int):
        for i in range(items_per_thread):
            qid = f"t{thread_idx}-q{i}"
            cache.store(
                query_id=qid,
                request_id=f"req-{qid}",
                sql=f"SELECT {i}",
                headers=["col"],
                rows=[[f"val-{i}"]],
            )
            # Retrieve immediately
            status, entry = cache.get(qid)
            assert status == "AVAILABLE"
            assert entry is not None
            assert entry.rows[0][0] == f"val-{i}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, t) for t in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # Will re-raise exceptions if any occurred


def test_cache_concurrent_slice_pagination():
    """Verify concurrent reads with offset pagination."""
    import concurrent.futures

    cache = QueryResultCache(max_size=10, ttl_seconds=300)
    large_rows = [[f"item-{i}"] for i in range(100)]
    cache.store("q-paginate", "req-p", "SELECT *", ["val"], large_rows)

    def reader(offset: int, limit: int):
        status, entry = cache.get("q-paginate")
        assert status == "AVAILABLE"
        assert entry is not None
        sliced = entry.slice_rows(offset, limit)
        assert len(sliced) == min(limit, max(0, 100 - offset))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reader, offset, 10) for offset in range(0, 100, 10)]
        for f in concurrent.futures.as_completed(futures):
            f.result()
