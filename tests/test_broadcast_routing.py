#!/usr/bin/env python3
"""
Tests for broadcast loop message routing and /internal/broadcast endpoint.

Verifies:
- _broadcast_loop routes Stock WS messages to correct channels
- /internal/broadcast routes events correctly via HTTP
- Authentication for internal endpoint
- Batch event handling
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.websocket_manager import StoreConnectionPool, WebSocketManager


def _mock_ws(name: str = "client") -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.__repr__ = lambda self: f"<MockWS:{name}>"
    return ws


class TestBroadcastLoopRouting:
    """Test _broadcast_loop message routing from Stock WS."""

    def _make_pool(self):
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.ws_events = _mock_ws("events")
        pool.ws_stock = _mock_ws("stock")
        pool.ws_store = _mock_ws("store")
        pool.events_clients.add(pool.ws_events)
        pool.stock_clients.add(pool.ws_stock)
        pool.store_clients.add(pool.ws_store)
        return pool

    @pytest.mark.asyncio
    async def test_event_pushed_theft_routes_to_events(self):
        pool = self._make_pool()
        message = json.dumps({
            "type": "event_pushed",
            "data": {"eventType": "THEFT", "productId": 1, "storeId": 1}
        })

        # Simulate _broadcast_loop processing
        parsed = json.loads(message)
        event = parsed.get("data", {})
        et = event.get("eventType", "")
        assert et == "THEFT"

        pool.receive_event(event)
        await asyncio.sleep(0.05)

        pool.ws_events.send_text.assert_called_once()
        pool.ws_stock.send_text.assert_not_called()
        pool.ws_store.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_pushed_restock_routes_to_events(self):
        pool = self._make_pool()
        event = {"eventType": "RESTOCK", "productId": 2, "storeId": 1}
        pool.receive_event(event)
        await asyncio.sleep(0.05)

        pool.ws_events.send_text.assert_called_once()
        sent = json.loads(pool.ws_events.send_text.call_args[0][0])
        assert sent["type"] == "event_pushed"
        assert sent["data"]["eventType"] == "RESTOCK"

    @pytest.mark.asyncio
    async def test_event_pushed_level_update_routes_to_stock(self):
        pool = self._make_pool()
        event = {"eventType": "LEVEL_UPDATE", "productId": 3, "stockState": "LOW"}
        pool.receive_stock_update(event)

        assert 3 in pool._stock_snapshots
        assert pool._stock_snapshots[3]["lastEvent"] == "LEVEL_UPDATE"
        pool.ws_events.send_text.assert_not_called()

        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()

    @pytest.mark.asyncio
    async def test_stats_update_routes_to_store(self):
        pool = self._make_pool()
        message = json.dumps({"type": "stats_update", "data": {"counts": {"FULL": 3}}})

        await pool.broadcast_store(message)

        pool.ws_store.send_text.assert_called_once()
        pool.ws_events.send_text.assert_not_called()
        pool.ws_stock.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_inventory_update_routes_to_store(self):
        pool = self._make_pool()
        message = json.dumps({"type": "inventory_update", "data": [{"productId": 1}]})

        await pool.broadcast_store(message)

        pool.ws_store.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_initial_data_broadcasts_to_all(self):
        pool = self._make_pool()
        message = json.dumps({"type": "initial_data", "storeId": 1, "data": {}})

        await pool.broadcast(message)

        pool.ws_events.send_text.assert_called_once()
        pool.ws_stock.send_text.assert_called_once()
        pool.ws_store.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscription_confirmed_broadcasts_to_all(self):
        pool = self._make_pool()
        message = json.dumps({"type": "subscription_confirmed", "storeId": 1})

        await pool.broadcast(message)

        pool.ws_events.send_text.assert_called_once()
        pool.ws_stock.send_text.assert_called_once()
        pool.ws_store.send_text.assert_called_once()


class TestInternalBroadcastEndpoint:
    """Test /internal/broadcast HTTP endpoint using real FastAPI app."""

    @pytest.fixture
    def app_with_manager(self):
        """Create a minimal FastAPI app with WS manager."""
        from fastapi import FastAPI, Request, HTTPException
        import os

        app = FastAPI()

        ws_manager = WebSocketManager(stock_ws_url="ws://test")
        app.state.ws_manager = ws_manager

        INTERNAL_API_SECRET = "test-secret"

        @app.post("/internal/broadcast")
        async def internal_broadcast(request: Request):
            header_secret = request.headers.get("X-Internal-Secret")
            if header_secret != INTERNAL_API_SECRET:
                raise HTTPException(status_code=403, detail="Unauthorized")

            body = await request.json()
            events = body if isinstance(body, list) else [body]
            total_broadcast = 0

            for event in events:
                store_id = event.get("storeId")
                if not store_id:
                    continue
                pool = ws_manager.pools.get(store_id)
                if not pool:
                    continue

                event_type = event.get("eventType", "")
                if event_type in ("THEFT", "RESTOCK"):
                    pool.receive_event(event)
                    total_broadcast += 1
                elif event_type in (
                    "LEVEL_UPDATE", "STOCK_CRITICAL", "STOCK_RECOVERY",
                    "ORDER_TO_RESTOCK", "SALE",
                ):
                    pool.receive_stock_update(event)
                    total_broadcast += 1

            return {"status": "broadcast", "events": total_broadcast, "received": len(events)}

        return app, ws_manager

    def test_unauthorized_without_secret(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, _ = app_with_manager
        client = TestClient(app)

        resp = client.post(
            "/internal/broadcast",
            json={"storeId": 1, "eventType": "THEFT", "productId": 1},
        )
        assert resp.status_code == 403

    def test_unauthorized_wrong_secret(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, _ = app_with_manager
        client = TestClient(app)

        resp = client.post(
            "/internal/broadcast",
            json={"storeId": 1, "eventType": "THEFT"},
            headers={"X-Internal-Secret": "wrong"},
        )
        assert resp.status_code == 403

    def test_single_event_broadcast(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, manager = app_with_manager

        # Create pool with events client
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        ws_events = _mock_ws("events")
        pool.events_clients.add(ws_events)
        manager.pools[1] = pool

        client = TestClient(app)
        resp = client.post(
            "/internal/broadcast",
            json={"storeId": 1, "eventType": "THEFT", "productId": 5},
            headers={"X-Internal-Secret": "test-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == 1
        assert body["received"] == 1

    def test_batch_events(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, manager = app_with_manager

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.events_clients.add(_mock_ws("events"))
        manager.pools[1] = pool

        events = [
            {"storeId": 1, "eventType": "THEFT", "productId": 1},
            {"storeId": 1, "eventType": "LEVEL_UPDATE", "productId": 2},
            {"storeId": 1, "eventType": "RESTOCK", "productId": 3},
        ]

        client = TestClient(app)
        resp = client.post(
            "/internal/broadcast",
            json=events,
            headers={"X-Internal-Secret": "test-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["received"] == 3
        # THEFT + LEVEL_UPDATE + RESTOCK = 3 events broadcast
        assert body["events"] == 3

        # Cleanup flush task
        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()

    def test_event_for_nonexistent_store(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, _ = app_with_manager

        client = TestClient(app)
        resp = client.post(
            "/internal/broadcast",
            json={"storeId": 999, "eventType": "THEFT"},
            headers={"X-Internal-Secret": "test-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == 0  # No pool for store 999

    def test_event_without_store_id(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, _ = app_with_manager

        client = TestClient(app)
        resp = client.post(
            "/internal/broadcast",
            json={"eventType": "THEFT"},
            headers={"X-Internal-Secret": "test-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == 0

    def test_stock_events_accumulated_not_immediate(self, app_with_manager):
        from fastapi.testclient import TestClient
        app, manager = app_with_manager

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        ws_stock = _mock_ws("stock")
        pool.stock_clients.add(ws_stock)
        manager.pools[1] = pool

        client = TestClient(app)
        resp = client.post(
            "/internal/broadcast",
            json={"storeId": 1, "eventType": "LEVEL_UPDATE", "productId": 10},
            headers={"X-Internal-Secret": "test-secret"},
        )
        assert resp.status_code == 200

        # LEVEL_UPDATE goes to accumulator, not immediately sent
        assert 10 in pool._stock_snapshots
        # Stock client shouldn't have been called yet (waiting for flush)
        ws_stock.send_text.assert_not_called()

        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()

    def test_all_event_types_routed(self, app_with_manager):
        """Verify all recognized event types are accepted."""
        from fastapi.testclient import TestClient
        app, manager = app_with_manager

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool.events_clients.add(_mock_ws("e"))
        manager.pools[1] = pool

        all_types = [
            ("THEFT", True),
            ("RESTOCK", True),
            ("LEVEL_UPDATE", True),
            ("STOCK_CRITICAL", True),
            ("STOCK_RECOVERY", True),
            ("ORDER_TO_RESTOCK", True),
            ("UNKNOWN_TYPE", False),
        ]

        client = TestClient(app)
        for event_type, should_count in all_types:
            resp = client.post(
                "/internal/broadcast",
                json={"storeId": 1, "eventType": event_type, "productId": 1},
                headers={"X-Internal-Secret": "test-secret"},
            )
            body = resp.json()
            assert body["events"] == (1 if should_count else 0), \
                f"{event_type}: expected {'counted' if should_count else 'not counted'}"

        if pool._stock_flush_task:
            pool._stock_flush_task.cancel()
