#!/usr/bin/env python3
"""
Tests for InitialDataCache.

Verifies:
- Cache hits within TTL
- Cache misses after TTL expires
- Manual invalidation
- Multiple stores
- Edge cases (empty data, zero TTL, re-set)
"""

import time
from datetime import datetime, timedelta

from utils.websocket_manager import InitialDataCache


class TestCacheHitMiss:
    """Test basic cache hit/miss behavior."""

    def test_set_and_get_within_ttl(self):
        cache = InitialDataCache(ttl_seconds=60)
        data = {"store": {"id": 1, "name": "Test"}, "aisles": []}
        cache.set(1, data)
        result = cache.get(1)
        assert result == data

    def test_get_nonexistent_store(self):
        cache = InitialDataCache(ttl_seconds=60)
        assert cache.get(999) is None

    def test_get_expired_returns_none(self):
        cache = InitialDataCache(ttl_seconds=1)
        cache.set(1, {"store": {"id": 1}})

        # Manually backdate the timestamp
        cache.timestamps[1] = datetime.now() - timedelta(seconds=5)

        result = cache.get(1)
        assert result is None
        # Entry should be cleaned up
        assert 1 not in cache.cache
        assert 1 not in cache.timestamps

    def test_multiple_stores_independent(self):
        cache = InitialDataCache(ttl_seconds=60)
        cache.set(1, {"store": {"id": 1}})
        cache.set(2, {"store": {"id": 2}})
        cache.set(3, {"store": {"id": 3}})

        assert cache.get(1)["store"]["id"] == 1
        assert cache.get(2)["store"]["id"] == 2
        assert cache.get(3)["store"]["id"] == 3

    def test_overwrite_resets_timestamp(self):
        cache = InitialDataCache(ttl_seconds=60)
        cache.set(1, {"version": "old"})

        # Backdate
        cache.timestamps[1] = datetime.now() - timedelta(seconds=55)

        # Overwrite with fresh data
        cache.set(1, {"version": "new"})

        # Should be cache hit (fresh timestamp)
        result = cache.get(1)
        assert result["version"] == "new"


class TestCacheInvalidation:
    """Test manual invalidation."""

    def test_invalidate_existing(self):
        cache = InitialDataCache(ttl_seconds=60)
        cache.set(1, {"data": True})
        cache.invalidate(1)
        assert cache.get(1) is None

    def test_invalidate_nonexistent_is_safe(self):
        cache = InitialDataCache(ttl_seconds=60)
        cache.invalidate(999)  # Should not raise

    def test_invalidate_one_leaves_others(self):
        cache = InitialDataCache(ttl_seconds=60)
        cache.set(1, {"id": 1})
        cache.set(2, {"id": 2})
        cache.invalidate(1)
        assert cache.get(1) is None
        assert cache.get(2)["id"] == 2


class TestCacheEdgeCases:
    """Edge cases for cache behavior."""

    def test_empty_data(self):
        cache = InitialDataCache(ttl_seconds=60)
        cache.set(1, {})
        result = cache.get(1)
        assert result == {}

    def test_very_short_ttl(self):
        cache = InitialDataCache(ttl_seconds=1)
        cache.set(1, {"fast": True})
        # Immediately should still be cached
        assert cache.get(1) is not None

    def test_default_ttl_uses_config(self):
        cache = InitialDataCache()
        # Should use WS_INITIAL_DATA_CACHE_TTL from env (default 30)
        assert cache.ttl_seconds > 0

    def test_large_data_payload(self):
        cache = InitialDataCache(ttl_seconds=60)
        large_data = {
            "products": [{"id": i, "name": f"Product {i}"} for i in range(1000)],
            "shelves": [{"id": i} for i in range(200)],
        }
        cache.set(1, large_data)
        result = cache.get(1)
        assert len(result["products"]) == 1000
        assert len(result["shelves"]) == 200

    def test_cache_size_tracking(self):
        cache = InitialDataCache(ttl_seconds=60)
        assert len(cache.cache) == 0
        cache.set(1, {"a": 1})
        cache.set(2, {"b": 2})
        assert len(cache.cache) == 2
        cache.invalidate(1)
        assert len(cache.cache) == 1
