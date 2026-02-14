#!/usr/bin/env python3
"""
Tests for WebSocket channel separation.

Verifies that:
- StoreConnectionPool routes events to correct client sets
- THEFT/RESTOCK → events_clients (immediate)
- Stock events (LEVEL_UPDATE etc.) → stock_clients (accumulated, flushed at interval)
- Store messages (stats_update, inventory_update) → store_clients
- Broadcast to ALL clients still works for initial_data etc.
- Stock snapshot accumulation and flush loop work correctly
- Client add/remove works per-channel
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.websocket_manager import StoreConnectionPool, WebSocketManager, WS_STOCK_UPDATE_INTERVAL


def _mock_ws(name: str = "client") -> MagicMock:
    """Create a mock WebSocket client."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.__repr__ = lambda self: f"<MockWS:{name}>"
    return ws


class TestStoreConnectionPoolClientSets:
    """Test client set management across channels."""

    def setup_method(self):
        self.pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

    @pytest.mark.asyncio
    async def test_add_client_events_channel(self):
        ws = _mock_ws("events1")
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            result = await self.pool.add_client(ws, channel="events")
        assert result is True
        assert ws in self.pool.events_clients
        assert ws not in self.pool.stock_clients
        assert ws not in self.pool.store_clients

    @pytest.mark.asyncio
    async def test_add_client_stock_channel(self):
        ws = _mock_ws("stock1")
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            result = await self.pool.add_client(ws, channel="stock")
        assert result is True
        assert ws in self.pool.stock_clients
        assert ws not in self.pool.events_clients

    @pytest.mark.asyncio
    async def test_add_client_store_channel(self):
        ws = _mock_ws("store1")
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            result = await self.pool.add_client(ws, channel="store")
        assert result is True
        assert ws in self.pool.store_clients

    @pytest.mark.asyncio
    async def test_add_client_default_channel_is_store(self):
        ws = _mock_ws("default")
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            await self.pool.add_client(ws)
        assert ws in self.pool.store_clients

    @pytest.mark.asyncio
    async def test_remove_client_from_any_set(self):
        ws = _mock_ws("multi")
        self.pool.events_clients.add(ws)
        await self.pool.remove_client(ws)
        assert ws not in self.pool.events_clients

    @pytest.mark.asyncio
    async def test_client_count(self):
        with patch.object(self.pool, "connect_to_stock", new_callable=AsyncMock):
            await self.pool.add_client(_mock_ws("e"), channel="events")
            await self.pool.add_client(_mock_ws("s"), channel="stock")
            await self.pool.add_client(_mock_ws("st"), channel="store")
        assert self.pool._client_count() == 3

    @pytest.mark.asyncio
    async def test_clients_property_returns_union(self):
        ws_e = _mock_ws("e")
        ws_s = _mock_ws("s")
        ws_st = _mock_ws("st")
        self.pool.events_clients.add(ws_e)
        self.pool.stock_clients.add(ws_s)
        self.pool.store_clients.add(ws_st)
        all_clients = self.pool.clients
        assert ws_e in all_clients
        assert ws_s in all_clients
        assert ws_st in all_clients
        assert len(all_clients) == 3


class TestEventRouting:
    """Test that receive_event routes THEFT/RESTOCK to events_clients."""

    def setup_method(self):
        self.pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

    @pytest.mark.asyncio
    async def test_receive_event_broadcasts_to_events_clients(self):
        ws_events = _mock_ws("events")
        self.pool.events_clients.add(ws_events)

        event = {
            "eventType": "THEFT",
            "productId": 5,
            "productName": "Doritos",
            "storeId": 1,
        }
        self.pool.receive_event(event)

        # Let the created task run
        await asyncio.sleep(0.05)

        ws_events.send_text.assert_called_once()
        sent = json.loads(ws_events.send_text.call_args[0][0])
        assert sent["type"] == "event_pushed"
        assert sent["data"]["eventType"] == "THEFT"
        assert sent["data"]["productId"] == 5

    @pytest.mark.asyncio
    async def test_receive_event_skips_when_no_events_clients(self):
        """receive_event should not create a task when no events_clients."""
        ws_stock = _mock_ws("stock")
        self.pool.stock_clients.add(ws_stock)

        event = {"eventType": "THEFT", "productId": 1, "storeId": 1}
        self.pool.receive_event(event)
        await asyncio.sleep(0.05)

        # Stock client should NOT receive THEFT
        ws_stock.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_receive_event_does_not_go_to_stock_clients(self):
        ws_stock = _mock_ws("stock")
        ws_events = _mock_ws("events")
        self.pool.stock_clients.add(ws_stock)
        self.pool.events_clients.add(ws_events)

        event = {"eventType": "RESTOCK", "productId": 2, "storeId": 1}
        self.pool.receive_event(event)
        await asyncio.sleep(0.05)

        ws_events.send_text.assert_called_once()
        ws_stock.send_text.assert_not_called()


class TestStockSnapshotAccumulator:
    """Test stock snapshot accumulation and flush."""

    def setup_method(self):
        self.pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

    @pytest.mark.asyncio
    async def test_receive_stock_update_accumulates(self):
        event1 = {
            "eventType": "LEVEL_UPDATE",
            "productId": 5,
            "productName": "Doritos",
            "stockState": "LOW",
            "stockAfter": 3,
            "stockPercentageAfter": 0.3,
        }
        event2 = {
            "eventType": "LEVEL_UPDATE",
            "productId": 5,
            "productName": "Doritos",
            "stockState": "CRITICAL",
            "stockAfter": 1,
            "stockPercentageAfter": 0.1,
        }
        self.pool.receive_stock_update(event1)
        assert 5 in self.pool._stock_snapshots
        assert self.pool._stock_snapshots[5]["stockState"] == "LOW"

        # Second update overwrites
        self.pool.receive_stock_update(event2)
        assert self.pool._stock_snapshots[5]["stockState"] == "CRITICAL"
        assert self.pool._stock_snapshots[5]["stockAfter"] == 1

        # Cancel flush task spawned by receive_stock_update
        if self.pool._stock_flush_task:
            self.pool._stock_flush_task.cancel()

    @pytest.mark.asyncio
    async def test_receive_stock_update_multiple_products(self):
        self.pool.receive_stock_update({
            "eventType": "LEVEL_UPDATE", "productId": 1, "productName": "A"
        })
        self.pool.receive_stock_update({
            "eventType": "STOCK_CRITICAL", "productId": 2, "productName": "B"
        })
        assert len(self.pool._stock_snapshots) == 2
        assert self.pool._stock_snapshots[1]["lastEvent"] == "LEVEL_UPDATE"
        assert self.pool._stock_snapshots[2]["lastEvent"] == "STOCK_CRITICAL"

        if self.pool._stock_flush_task:
            self.pool._stock_flush_task.cancel()

    def test_receive_stock_update_ignores_missing_product_id(self):
        self.pool.receive_stock_update({"eventType": "LEVEL_UPDATE"})
        assert len(self.pool._stock_snapshots) == 0

    @pytest.mark.asyncio
    async def test_stock_flush_loop_sends_to_stock_clients(self):
        ws_stock = _mock_ws("stock")
        self.pool.stock_clients.add(ws_stock)

        self.pool._stock_snapshots = {
            5: {
                "productId": 5,
                "productName": "Doritos",
                "stockState": "LOW",
                "stockAfter": 3,
                "stockPercentageAfter": 0.3,
                "lastEvent": "LEVEL_UPDATE",
            }
        }

        # Start flush loop and let it run once
        task = asyncio.create_task(self.pool._stock_flush_loop())
        await asyncio.sleep(WS_STOCK_UPDATE_INTERVAL + 0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        ws_stock.send_text.assert_called()
        sent = json.loads(ws_stock.send_text.call_args[0][0])
        assert sent["type"] == "stock_update"
        assert sent["storeId"] == 1
        assert len(sent["products"]) == 1
        assert sent["products"][0]["productId"] == 5

        # Snapshot should be cleared after flush
        assert len(self.pool._stock_snapshots) == 0

    @pytest.mark.asyncio
    async def test_stock_flush_skips_when_no_stock_clients(self):
        """Flush loop should not send when no stock_clients."""
        ws_events = _mock_ws("events")
        self.pool.events_clients.add(ws_events)

        self.pool._stock_snapshots = {1: {"productId": 1}}

        task = asyncio.create_task(self.pool._stock_flush_loop())
        await asyncio.sleep(WS_STOCK_UPDATE_INTERVAL + 0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # No clients received anything
        ws_events.send_text.assert_not_called()
        # Snapshot should NOT be cleared (no stock_clients to receive it)
        assert len(self.pool._stock_snapshots) == 1

    @pytest.mark.asyncio
    async def test_stock_flush_does_not_send_to_events_clients(self):
        ws_events = _mock_ws("events")
        ws_stock = _mock_ws("stock")
        self.pool.events_clients.add(ws_events)
        self.pool.stock_clients.add(ws_stock)

        self.pool._stock_snapshots = {1: {"productId": 1, "productName": "A"}}

        task = asyncio.create_task(self.pool._stock_flush_loop())
        await asyncio.sleep(WS_STOCK_UPDATE_INTERVAL + 0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        ws_stock.send_text.assert_called()
        ws_events.send_text.assert_not_called()


class TestBroadcastMethods:
    """Test channel-specific broadcast methods."""

    def setup_method(self):
        self.pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        ws_e = _mock_ws("e")
        ws_s = _mock_ws("s")
        ws_st = _mock_ws("st")
        self.pool.events_clients.add(ws_e)
        self.pool.stock_clients.add(ws_s)
        self.pool.store_clients.add(ws_st)

        await self.pool.broadcast('{"type":"test"}')

        ws_e.send_text.assert_called_once()
        ws_s.send_text.assert_called_once()
        ws_st.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_events_only_events(self):
        ws_e = _mock_ws("e")
        ws_s = _mock_ws("s")
        self.pool.events_clients.add(ws_e)
        self.pool.stock_clients.add(ws_s)

        await self.pool.broadcast_events('{"type":"test"}')

        ws_e.send_text.assert_called_once()
        ws_s.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_stock_only_stock(self):
        ws_e = _mock_ws("e")
        ws_s = _mock_ws("s")
        self.pool.events_clients.add(ws_e)
        self.pool.stock_clients.add(ws_s)

        await self.pool.broadcast_stock('{"type":"test"}')

        ws_s.send_text.assert_called_once()
        ws_e.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_store_only_store(self):
        ws_st = _mock_ws("st")
        ws_e = _mock_ws("e")
        self.pool.store_clients.add(ws_st)
        self.pool.events_clients.add(ws_e)

        await self.pool.broadcast_store('{"type":"test"}')

        ws_st.send_text.assert_called_once()
        ws_e.send_text.assert_not_called()


class TestInternalBroadcastRouting:
    """Test that /internal/broadcast routes events correctly."""

    @pytest.mark.asyncio
    async def test_theft_routed_to_events(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        ws_events = _mock_ws("events")
        ws_stock = _mock_ws("stock")
        pool.events_clients.add(ws_events)
        pool.stock_clients.add(ws_stock)

        event = {"storeId": 1, "eventType": "THEFT", "productId": 5}
        pool.receive_event(event)
        await asyncio.sleep(0.05)

        ws_events.send_text.assert_called_once()
        ws_stock.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_restock_routed_to_events(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        ws_events = _mock_ws("events")
        pool.events_clients.add(ws_events)

        event = {"storeId": 1, "eventType": "RESTOCK", "productId": 5}
        pool.receive_event(event)
        await asyncio.sleep(0.05)

        sent = json.loads(ws_events.send_text.call_args[0][0])
        assert sent["data"]["eventType"] == "RESTOCK"

    @pytest.mark.asyncio
    async def test_level_update_accumulated(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        event = {
            "storeId": 1,
            "eventType": "LEVEL_UPDATE",
            "productId": 5,
            "stockState": "LOW",
        }
        pool.receive_stock_update(event)
        assert 5 in pool._stock_snapshots
        assert pool._stock_snapshots[5]["lastEvent"] == "LEVEL_UPDATE"
        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()

    @pytest.mark.asyncio
    async def test_stock_critical_accumulated(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        event = {"storeId": 1, "eventType": "STOCK_CRITICAL", "productId": 3}
        pool.receive_stock_update(event)
        assert 3 in pool._stock_snapshots
        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()

    @pytest.mark.asyncio
    async def test_sale_accumulated(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        event = {"storeId": 1, "eventType": "SALE", "productId": 7}
        pool.receive_stock_update(event)
        assert 7 in pool._stock_snapshots
        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()


class TestWebSocketManagerChannels:
    """Test WebSocketManager channel routing."""

    @pytest.mark.asyncio
    async def test_add_client_with_channel(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        ws = _mock_ws("test")

        with patch.object(StoreConnectionPool, "connect_to_stock", new_callable=AsyncMock):
            result = await manager.add_client(1, ws, "token123", channel="events")

        assert result is True
        pool = manager.pools[1]
        assert ws in pool.events_clients
        assert ws not in pool.stock_clients
        assert ws not in pool.store_clients

    @pytest.mark.asyncio
    async def test_add_client_default_store_channel(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        ws = _mock_ws("test")

        with patch.object(StoreConnectionPool, "connect_to_stock", new_callable=AsyncMock):
            await manager.add_client(1, ws, "token123")

        pool = manager.pools[1]
        assert ws in pool.store_clients

    def test_get_stats_shows_per_channel_counts(self):
        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.events_clients.add(_mock_ws("e1"))
        pool.events_clients.add(_mock_ws("e2"))
        pool.stock_clients.add(_mock_ws("s1"))
        pool.store_clients.add(_mock_ws("st1"))
        manager.pools[1] = pool

        stats = manager.get_stats()
        assert stats["total_clients"] == 4
        store_info = stats["connected_stores"][0]
        assert store_info["events_clients"] == 2
        assert store_info["stock_clients"] == 1
        assert store_info["store_clients"] == 1
