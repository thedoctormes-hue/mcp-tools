"""Sprint B / P4: ALM fan-out throttle + latency telemetry (read-only checks)."""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_gateway import search, config


def test_alm_latency_stats_empty():
    # fresh module state -> no samples
    stats = search.alm_latency_stats()
    assert stats["count"] == 0
    assert stats["last_ms"] is None
    assert stats["p50_ms"] is None
    assert stats["p95_ms"] is None
    assert stats["inflight_limit"] == max(1, int(config.VECTOR_MAX_INFLIGHT))


def test_semaphore_exists_and_bounds():
    assert isinstance(search._ALM_SEM, type(search.threading.Semaphore()))
    assert search._ALM_SEM._value == max(1, int(config.VECTOR_MAX_INFLIGHT))


def test_alm_latency_stats_records():
    # simulate recording a few latencies by calling the private append path
    with search._ALM_LAT_LOCK:
        search._ALM_LATENCY.clear()
    # directly exercise the recording lambda used in _vector_search_one
    for v in (10.0, 20.0, 30.0, 40.0, 50.0):
        with search._ALM_LAT_LOCK:
            search._ALM_LATENCY.append(v)
            if len(search._ALM_LATENCY) > search._ALM_LATENCY_MAX:
                del search._ALM_LATENCY[: len(search._ALM_LATENCY) - search._ALM_LATENCY_MAX]
    stats = search.alm_latency_stats()
    assert stats["count"] == 5
    assert stats["last_ms"] == 50.0
    # p50 of [10,20,30,40,50] -> index 2 -> 30.0
    assert stats["p50_ms"] == 30.0
    # p95 -> index min(4, int(0.95*5)=4) -> 50.0
    assert stats["p95_ms"] == 50.0
    # cleanup
    with search._ALM_LAT_LOCK:
        search._ALM_LATENCY.clear()
