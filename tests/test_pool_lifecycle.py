#!/usr/bin/env python3
"""
Tests for StoreConnectionPool and WebSocketManager lifecycle.

Verifies:
- Pool capacity limits (WS_MAX_CLIENTS_PER_POOL)
- Parallel send error handling (timeout, disconnect)
- Pool cleanup after all clients leave
- WebSocketManager pool creation and cleanup
- Token management in pools
- Stats reporting
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import WebSocket

from utils.websocket_manager import (
    StoreConnectionPool,
    WebSocketManager,
    WS_MAX_CLIENTS_PER_POOL,
    WS_STOCK_UPDATE_INTERVAL,
)


def _mock_ws(name: str = "client") -> MagicMock:
    """Create a mock WebSocket client that passes isinstance(ws, WebSocket)."""
    ws = MagicMock(spec=WebSocket)
    ws.send_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.__repr__ = lambda self: f"<MockWS:{name}>"
    return ws


class TestPoolCapacity:
    """Test pool capacity management."""

    def setup_method(self):
        self.pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

    @pytest.mark.asyncio
    async def test_reject_at_capacity(self):
        """Pool should reject clients when at max capacity."""
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            # Fill up to capacity
            for i in range(WS_MAX_CLIENTS_PER_POOL):
                ws = _mock_ws(f"c{i}")
                result = await self.pool.add_client(ws, channel="store")
                assert result is True

            # Next client should be rejected
            extra = _mock_ws("overflow")
            result = await self.pool.add_client(extra, channel="store")
            assert result is False
            assert extra not in self.pool.store_clients

    @pytest.mark.asyncio
    async def test_capacity_shared_across_channels(self):
        """Capacity limit is shared across all channels."""
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            # Add clients across channels
            limit = WS_MAX_CLIENTS_PER_POOL
            per_channel = limit // 3
            remainder = limit - (per_channel * 3)

            for i in range(per_channel):
                await self.pool.add_client(_mock_ws(f"e{i}"), channel="events")
            for i in range(per_channel):
                await self.pool.add_client(_mock_ws(f"s{i}"), channel="stock")
            for i in range(per_channel + remainder):
                await self.pool.add_client(_mock_ws(f"st{i}"), channel="store")

            assert self.pool._client_count() == limit

            # Should reject
            result = await self.pool.add_client(_mock_ws("over"), channel="events")
            assert result is False

    @pytest.mark.asyncio
    async def test_remove_then_add_again(self):
        """After removing a client, a new one should be accepted."""
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            clients = []
            for i in range(WS_MAX_CLIENTS_PER_POOL):
                ws = _mock_ws(f"c{i}")
                await self.pool.add_client(ws, channel="store")
                clients.append(ws)

            # Remove one
            await self.pool.remove_client(clients[0])

            # Should now accept
            new_ws = _mock_ws("new")
            result = await self.pool.add_client(new_ws, channel="store")
            assert result is True


class TestParallelSend:
    """Test _parallel_send error handling."""

    def setup_method(self):
        self.pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

    @pytest.mark.asyncio
    async def test_send_to_empty_list(self):
        """Sending to empty list should be no-op."""
        await self.pool._parallel_send([], "test")

    @pytest.mark.asyncio
    async def test_successful_send(self):
        ws = _mock_ws("ok")
        await self.pool._parallel_send([ws], '{"msg":"hi"}')
        ws.send_text.assert_called_once_with('{"msg":"hi"}')

    @pytest.mark.asyncio
    async def test_failed_client_removed(self):
        """Client that raises on send should be removed from pool."""
        ws_ok = _mock_ws("ok")
        ws_bad = _mock_ws("bad")
        ws_bad.send_text.side_effect = ConnectionError("closed")

        self.pool.store_clients.add(ws_ok)
        self.pool.store_clients.add(ws_bad)

        await self.pool._parallel_send([ws_ok, ws_bad], "test")

        # Good client still there
        assert ws_ok in self.pool.store_clients
        # Bad client removed
        assert ws_bad not in self.pool.store_clients

    @pytest.mark.asyncio
    async def test_timeout_client_removed(self):
        """Client that times out should be removed."""
        ws = _mock_ws("slow")

        async def slow_send(msg):
            await asyncio.sleep(100)

        ws.send_text = slow_send  # type: ignore[assignment]
        self.pool.store_clients.add(ws)

        # Use a short send timeout
        with patch("utils.websocket_manager.WS_SEND_TIMEOUT", 0.01):
            await self.pool._parallel_send([ws], "test")

        assert ws not in self.pool.store_clients

    @pytest.mark.asyncio
    async def test_multiple_clients_partial_failure(self):
        """Some clients succeed, some fail — only failures removed."""
        ok1 = _mock_ws("ok1")
        ok2 = _mock_ws("ok2")
        bad = _mock_ws("bad")
        bad.send_text.side_effect = Exception("disconnected")

        self.pool.events_clients.add(ok1)
        self.pool.events_clients.add(ok2)
        self.pool.events_clients.add(bad)

        await self.pool._parallel_send([ok1, ok2, bad], "msg")

        assert ok1 in self.pool.events_clients
        assert ok2 in self.pool.events_clients
        assert bad not in self.pool.events_clients


class TestDelayedCleanup:
    """Test delayed cleanup after clients leave."""

    @pytest.mark.asyncio
    async def test_cleanup_disconnects_stock_ws(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.is_connected = True
        pool.disconnect_from_stock = AsyncMock()

        ws = _mock_ws("last")
        pool.store_clients.add(ws)

        # Remove client — triggers _delayed_cleanup
        with patch("utils.websocket_manager.WS_CLEANUP_GRACE_SECONDS", 0.01):
            await pool.remove_client(ws)
            await asyncio.sleep(0.1)

        pool.disconnect_from_stock.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_cancelled_if_new_client(self):
        """If a new client connects during grace period, don't cleanup."""
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.is_connected = True
        pool.disconnect_from_stock = AsyncMock()

        ws1 = _mock_ws("c1")
        pool.store_clients.add(ws1)

        # Remove client
        with patch("utils.websocket_manager.WS_CLEANUP_GRACE_SECONDS", 0.2):
            await pool.remove_client(ws1)

            # Quickly add a new client before grace period expires
            ws2 = _mock_ws("c2")
            pool.store_clients.add(ws2)

            await asyncio.sleep(0.3)

        # Should NOT have disconnected (new client arrived)
        pool.disconnect_from_stock.assert_not_called()


class TestDisconnectFromStock:
    """Test disconnect_from_stock cleanup."""

    @pytest.mark.asyncio
    async def test_cancels_flush_task(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        # Simulate a running flush task
        async def dummy():
            await asyncio.sleep(100)

        pool._stock_flush_task = asyncio.create_task(dummy())
        pool.stock_ws = MagicMock()
        pool.stock_ws.close = AsyncMock()

        await pool.disconnect_from_stock()

        assert pool._stock_flush_task is None
        assert pool.stock_ws is None
        assert pool.is_connected is False

    @pytest.mark.asyncio
    async def test_cancels_broadcast_task(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        async def dummy():
            await asyncio.sleep(100)

        pool.stock_ws_task = asyncio.create_task(dummy())
        pool.stock_ws = MagicMock()
        pool.stock_ws.close = AsyncMock()

        await pool.disconnect_from_stock()
        assert pool.is_connected is False


class TestWebSocketManagerPoolManagement:
    """Test WebSocketManager pool creation and lifecycle."""

    def test_get_or_create_pool_new(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = manager.get_or_create_pool(1, "tok")
        assert pool.store_id == 1
        assert 1 in manager.pools

    def test_get_or_create_pool_existing(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool1 = manager.get_or_create_pool(1, "tok")
        pool2 = manager.get_or_create_pool(1, "tok")
        assert pool1 is pool2

    def test_service_token_preferred(self):
        """Service token should be used over per-client token."""
        manager = WebSocketManager(stock_ws_url="ws://test")
        manager._service_token = "service-tok"
        pool = manager.get_or_create_pool(1, "client-tok")
        assert pool.token == "service-tok"

    def test_fallback_to_client_token(self):
        """When no service token, use client token."""
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = manager.get_or_create_pool(1, "client-tok")
        assert pool.token == "client-tok"

    @pytest.mark.asyncio
    async def test_cleanup_empty_pools(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool1 = manager.get_or_create_pool(1, "tok")
        pool2 = manager.get_or_create_pool(2, "tok")

        # Pool 1 has clients, pool 2 is empty
        pool1.store_clients.add(_mock_ws("c1"))

        await manager.cleanup_empty_pools()

        assert 1 in manager.pools
        assert 2 not in manager.pools

    @pytest.mark.asyncio
    async def test_cleanup_keeps_connected_pools(self):
        """Don't clean up pools that are still connected to Stock WS."""
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = manager.get_or_create_pool(1, "tok")
        pool.is_connected = True  # Still connected

        await manager.cleanup_empty_pools()
        assert 1 in manager.pools

    @pytest.mark.asyncio
    async def test_remove_client_nonexistent_store(self):
        """Removing from non-existent store should not raise."""
        manager = WebSocketManager(stock_ws_url="ws://test")
        ws = _mock_ws("orphan")
        await manager.remove_client(999, ws)  # No-op


class TestWebSocketManagerStats:
    """Test stats reporting."""

    def test_stats_empty(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        stats = manager.get_stats()
        assert stats["total_pools"] == 0
        assert stats["total_clients"] == 0
        assert stats["connected_stores"] == []
        assert stats["cache_size"] == 0

    def test_stats_with_pools(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.events_clients.add(_mock_ws("e1"))
        pool.stock_clients.add(_mock_ws("s1"))
        pool.stock_clients.add(_mock_ws("s2"))
        pool.store_clients.add(_mock_ws("st1"))
        pool.is_connected = True
        manager.pools[1] = pool

        stats = manager.get_stats()
        assert stats["total_pools"] == 1
        assert stats["total_clients"] == 4
        assert len(stats["connected_stores"]) == 1
        store = stats["connected_stores"][0]
        assert store["store_id"] == 1
        assert store["events_clients"] == 1
        assert store["stock_clients"] == 2
        assert store["store_clients"] == 1
        assert store["connected"] is True

    def test_stats_multiple_stores(self):
        manager = WebSocketManager(stock_ws_url="ws://test")

        for sid in [1, 2, 3]:
            pool = StoreConnectionPool(store_id=sid, stock_ws_url="ws://test", token="tok")
            pool.store_clients.add(_mock_ws(f"c{sid}"))
            manager.pools[sid] = pool

        stats = manager.get_stats()
        assert stats["total_pools"] == 3
        assert stats["total_clients"] == 3

    def test_stats_cache_size(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        manager.initial_data_cache.set(1, {"data": True})
        manager.initial_data_cache.set(2, {"data": True})
        stats = manager.get_stats()
        assert stats["cache_size"] == 2


class TestStockFlushIntegration:
    """Integration test: stock updates → accumulation → flush → delivery."""

    @pytest.mark.asyncio
    async def test_full_cycle(self):
        """Receive multiple updates, flush, verify delivery to stock clients only."""
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        ws_stock = _mock_ws("stock")
        ws_events = _mock_ws("events")
        pool.stock_clients.add(ws_stock)
        pool.events_clients.add(ws_events)

        # Accumulate updates
        pool.receive_stock_update({
            "eventType": "LEVEL_UPDATE", "productId": 1,
            "productName": "Doritos", "stockState": "NORMAL", "stockAfter": 8,
        })
        pool.receive_stock_update({
            "eventType": "LEVEL_UPDATE", "productId": 2,
            "productName": "Lays", "stockState": "LOW", "stockAfter": 2,
        })
        # Overwrite product 1
        pool.receive_stock_update({
            "eventType": "SALE", "productId": 1,
            "productName": "Doritos", "stockState": "LOW", "stockAfter": 5,
        })

        assert len(pool._stock_snapshots) == 2
        assert pool._stock_snapshots[1]["lastEvent"] == "SALE"
        assert pool._stock_snapshots[1]["stockAfter"] == 5

        # Manually trigger flush
        task = asyncio.create_task(pool._stock_flush_loop())
        await asyncio.sleep(WS_STOCK_UPDATE_INTERVAL + 0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Stock client received the batch
        ws_stock.send_text.assert_called()
        sent = json.loads(ws_stock.send_text.call_args[0][0])
        assert sent["type"] == "stock_update"
        assert sent["storeId"] == 1
        assert len(sent["products"]) == 2

        product_ids = {p["productId"] for p in sent["products"]}
        assert product_ids == {1, 2}

        # Events client should NOT receive stock updates
        ws_events.send_text.assert_not_called()

        # Snapshots cleared after flush
        assert len(pool._stock_snapshots) == 0

        # Cancel flush task if still around
        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()
