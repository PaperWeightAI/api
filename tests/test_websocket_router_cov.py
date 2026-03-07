#!/usr/bin/env python3
"""
Coverage tests for routers/websocket.py.

Covers _websocket_handler through all branches, the three WS endpoint wrappers,
/stats endpoint, and /internal/broadcast endpoint.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_token_obj(*, is_admin=True, username="testadmin", user_id=1,
                    is_operator=False, store_ids=None):
    """Create a fake TokenObject matching common.auth's inner class."""
    claims = {"store_ids": store_ids or []}
    obj = MagicMock()
    obj.username = username
    obj.user_id = user_id
    obj.is_admin = is_admin
    obj.is_operator = is_operator
    obj.claims = claims
    return obj


def _mock_websocket(*, headers=None, cookies=None, store_id=1):
    """Build a mock FastAPI WebSocket with the async methods the handler calls."""
    ws = MagicMock()
    ws.headers = headers or {}
    ws.cookies = cookies or {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock()

    # app.state.ws_manager / app.state.http_client
    ws.app = MagicMock()
    ws.app.state.ws_manager = MagicMock()
    ws.app.state.ws_manager.add_client = AsyncMock(return_value=True)
    ws.app.state.ws_manager.remove_client = AsyncMock()
    ws.app.state.ws_manager.initial_data_cache = MagicMock()
    ws.app.state.ws_manager.initial_data_cache.get.return_value = None
    ws.app.state.http_client = MagicMock()

    return ws


# ---------------------------------------------------------------------------
# _websocket_handler — No token (query, header, cookie all missing)
# ---------------------------------------------------------------------------

class TestWebsocketHandlerNoToken:

    @pytest.mark.asyncio
    async def test_no_token_closes_1008(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket()
        # No token in query, no auth header, no cookie
        await _websocket_handler(ws, store_id=1, token=None, endpoint_type="events")

        ws.accept.assert_awaited_once()
        ws.close.assert_any_await(code=1008, reason="No token provided")


# ---------------------------------------------------------------------------
# _websocket_handler — Token from Authorization header
# ---------------------------------------------------------------------------

class TestWebsocketHandlerAuthHeader:

    @pytest.mark.asyncio
    async def test_token_from_bearer_header(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket(headers={"authorization": "Bearer hdr-token"})
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj
            with patch("routers.websocket.DataAggregator") as MockAgg:
                MockAgg.return_value.aggregate_initial_data = AsyncMock(
                    return_value={"store": {"id": 1}}
                )
                ws.receive_text.side_effect = Exception("disconnect")

                await _websocket_handler(ws, store_id=1, token=None, endpoint_type="stock")

        # Should have extracted token from header and accepted
        MockVal.return_value.authenticate_token.assert_called_once_with("hdr-token")
        ws.accept.assert_awaited_once()


# ---------------------------------------------------------------------------
# _websocket_handler — Token from cookie
# ---------------------------------------------------------------------------

class TestWebsocketHandlerCookieToken:

    @pytest.mark.asyncio
    async def test_token_from_cookie(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket(cookies={"access_token": "cookie-token"})
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj
            with patch("routers.websocket.DataAggregator") as MockAgg:
                MockAgg.return_value.aggregate_initial_data = AsyncMock(
                    return_value={"store": {"id": 1}}
                )
                ws.receive_text.side_effect = Exception("disconnect")

                await _websocket_handler(ws, store_id=1, token=None, endpoint_type="store")

        MockVal.return_value.authenticate_token.assert_called_once_with("cookie-token")


# ---------------------------------------------------------------------------
# _websocket_handler — Invalid token
# ---------------------------------------------------------------------------

class TestWebsocketHandlerInvalidToken:

    @pytest.mark.asyncio
    async def test_invalid_token_closes_1008(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = None

            await _websocket_handler(ws, store_id=1, token="bad-token", endpoint_type="events")

        ws.accept.assert_awaited_once()
        ws.close.assert_any_await(code=1008, reason="Invalid token")


# ---------------------------------------------------------------------------
# _websocket_handler — Access denied
# ---------------------------------------------------------------------------

class TestWebsocketHandlerAccessDenied:

    @pytest.mark.asyncio
    async def test_access_denied_non_admin(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket()
        token_obj = _make_token_obj(is_admin=False, username="user1", store_ids=[99])

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="events")

        ws.close.assert_any_await(code=1008, reason="Access denied to this store")


# ---------------------------------------------------------------------------
# _websocket_handler — Cached data path
# ---------------------------------------------------------------------------

class TestWebsocketHandlerCachedData:

    @pytest.mark.asyncio
    async def test_cached_initial_data_used(self):
        from routers.websocket import _websocket_handler, WebSocketDisconnect

        ws = _mock_websocket()
        cached = {"store": {"id": 1}, "aisles": []}
        ws.app.state.ws_manager.initial_data_cache.get.return_value = cached
        token_obj = _make_token_obj()

        # Simulate client disconnect on receive_text
        ws.receive_text.side_effect = WebSocketDisconnect()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="stock")

        # Should have sent the cached data
        ws.send_json.assert_awaited_once()
        sent = ws.send_json.call_args[0][0]
        assert sent["type"] == "initial_data"
        assert sent["data"] == cached


# ---------------------------------------------------------------------------
# _websocket_handler — Fresh data path (cache miss)
# ---------------------------------------------------------------------------

class TestWebsocketHandlerFreshData:

    @pytest.mark.asyncio
    async def test_fresh_data_fetched_and_cached(self):
        from routers.websocket import _websocket_handler, WebSocketDisconnect

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = None
        token_obj = _make_token_obj()

        fresh = {"store": {"id": 1}, "shelves": []}

        ws.receive_text.side_effect = WebSocketDisconnect()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj
            with patch("routers.websocket.DataAggregator") as MockAgg:
                MockAgg.return_value.aggregate_initial_data = AsyncMock(return_value=fresh)

                await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="events")

        # Cache should have been set
        ws.app.state.ws_manager.initial_data_cache.set.assert_called_once_with(1, fresh)


# ---------------------------------------------------------------------------
# _websocket_handler — Pool at capacity
# ---------------------------------------------------------------------------

class TestWebsocketHandlerPoolCapacity:

    @pytest.mark.asyncio
    async def test_pool_at_capacity_closes_1013(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket()
        ws.app.state.ws_manager.add_client = AsyncMock(return_value=False)
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="stock")

        # Should send error and close with 1013
        error_sent = ws.send_json.call_args_list[-1][0][0]
        assert error_sent["type"] == "error"
        assert "capacity" in error_sent["message"].lower()
        ws.close.assert_any_await(code=1013, reason="Try again later")


# ---------------------------------------------------------------------------
# _websocket_handler — WebSocketDisconnect in keep-alive loop
# ---------------------------------------------------------------------------

class TestWebsocketHandlerDisconnect:

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self):
        from routers.websocket import _websocket_handler, WebSocketDisconnect

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        token_obj = _make_token_obj()
        ws.receive_text.side_effect = WebSocketDisconnect()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="events")

        ws.app.state.ws_manager.remove_client.assert_awaited_once_with(1, ws)


# ---------------------------------------------------------------------------
# _websocket_handler — Exception path (service error)
# ---------------------------------------------------------------------------

class TestWebsocketHandlerError:

    @pytest.mark.asyncio
    async def test_exception_sends_error_and_cleans_up(self):
        from routers.websocket import _websocket_handler

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        ws.app.state.ws_manager.add_client = AsyncMock(side_effect=RuntimeError("boom"))
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="store")

        # Should have tried to send error message
        error_calls = [
            c for c in ws.send_json.call_args_list
            if c[0][0].get("type") == "error" and "reconnect" in c[0][0].get("message", "")
        ]
        assert len(error_calls) >= 1

        # Cleanup still ran
        ws.app.state.ws_manager.remove_client.assert_awaited()

    @pytest.mark.asyncio
    async def test_error_sending_error_message_is_swallowed(self):
        """If sending the error message itself fails, the handler should not crash."""
        from routers.websocket import _websocket_handler

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        ws.app.state.ws_manager.add_client = AsyncMock(side_effect=RuntimeError("boom"))
        # Make send_json fail on error message
        ws.send_json.side_effect = [None, RuntimeError("send failed")]
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            # Should not raise
            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="store")

    @pytest.mark.asyncio
    async def test_cleanup_remove_client_error_swallowed(self):
        """If remove_client in finally block fails, handler should not crash."""
        from routers.websocket import _websocket_handler, WebSocketDisconnect

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        ws.app.state.ws_manager.remove_client = AsyncMock(side_effect=RuntimeError("cleanup err"))
        ws.receive_text.side_effect = WebSocketDisconnect()
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="events")

    @pytest.mark.asyncio
    async def test_close_in_finally_error_swallowed(self):
        """If websocket.close() in finally block fails, handler should not crash."""
        from routers.websocket import _websocket_handler, WebSocketDisconnect

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        ws.receive_text.side_effect = WebSocketDisconnect()
        ws.close.side_effect = RuntimeError("already closed")
        token_obj = _make_token_obj()

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="stock")


# ---------------------------------------------------------------------------
# Endpoint wrappers — verify they delegate to _websocket_handler
# ---------------------------------------------------------------------------

class TestEndpointWrappers:

    @pytest.mark.asyncio
    async def test_websocket_events_delegates(self):
        from routers.websocket import websocket_events

        ws = _mock_websocket()
        with patch("routers.websocket._websocket_handler", new_callable=AsyncMock) as mock_h:
            await websocket_events(ws, store_id=1, token="t")
        mock_h.assert_awaited_once_with(ws, 1, "t", "events")

    @pytest.mark.asyncio
    async def test_websocket_stock_delegates(self):
        from routers.websocket import websocket_stock

        ws = _mock_websocket()
        with patch("routers.websocket._websocket_handler", new_callable=AsyncMock) as mock_h:
            await websocket_stock(ws, store_id=2, token="t2")
        mock_h.assert_awaited_once_with(ws, 2, "t2", "stock")

    @pytest.mark.asyncio
    async def test_websocket_store_delegates(self):
        from routers.websocket import websocket_store

        ws = _mock_websocket()
        with patch("routers.websocket._websocket_handler", new_callable=AsyncMock) as mock_h:
            await websocket_store(ws, store_id=3, token=None)
        mock_h.assert_awaited_once_with(ws, 3, None, "store")


# ---------------------------------------------------------------------------
# /stats endpoint
# ---------------------------------------------------------------------------

class TestStatsEndpoint:

    @pytest.mark.asyncio
    async def test_stats_returns_manager_stats(self):
        from routers.websocket import websocket_stats

        request = MagicMock()
        request.app.state.ws_manager.get_stats.return_value = {
            "total_pools": 2, "total_clients": 5
        }

        result = await websocket_stats(request)
        assert result == {"total_pools": 2, "total_clients": 5}

    @pytest.mark.asyncio
    async def test_stats_error_raises_500(self):
        from routers.websocket import websocket_stats
        from fastapi import HTTPException

        request = MagicMock()
        request.app.state.ws_manager.get_stats.side_effect = RuntimeError("fail")

        with pytest.raises(HTTPException) as exc_info:
            await websocket_stats(request)
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# /internal/broadcast endpoint
# ---------------------------------------------------------------------------

class TestInternalBroadcast:

    @pytest.mark.asyncio
    async def test_single_theft_event(self):
        from routers.websocket import internal_broadcast

        pool = MagicMock()
        pool.receive_event = MagicMock()
        pool.receive_stock_update = MagicMock()

        request = MagicMock()
        request.json = AsyncMock(return_value={
            "storeId": 1, "eventType": "THEFT", "productId": 5
        })
        request.app.state.ws_manager.pools = {1: pool}

        result = await internal_broadcast(request, _user={"sub": "svc"})

        pool.receive_event.assert_called_once()
        assert result["events"] == 1
        assert result["received"] == 1

    @pytest.mark.asyncio
    async def test_batch_events(self):
        from routers.websocket import internal_broadcast

        pool = MagicMock()
        pool.receive_event = MagicMock()
        pool.receive_stock_update = MagicMock()

        events = [
            {"storeId": 1, "eventType": "THEFT", "productId": 1},
            {"storeId": 1, "eventType": "LEVEL_UPDATE", "productId": 2},
            {"storeId": 1, "eventType": "RESTOCK", "productId": 3},
            {"storeId": 1, "eventType": "RESTOCK_FULL", "productId": 4},
            {"storeId": 1, "eventType": "RESTOCK_PARTIAL", "productId": 5},
            {"storeId": 1, "eventType": "STOCK_UPDATE", "productId": 6},
            {"storeId": 1, "eventType": "STOCK_CRITICAL", "productId": 7},
            {"storeId": 1, "eventType": "STOCK_RECOVERY", "productId": 8},
            {"storeId": 1, "eventType": "ORDER_TO_RESTOCK", "productId": 9},
        ]

        request = MagicMock()
        request.json = AsyncMock(return_value=events)
        request.app.state.ws_manager.pools = {1: pool}

        result = await internal_broadcast(request, _user={"sub": "svc"})

        # THEFT, RESTOCK, RESTOCK_FULL, RESTOCK_PARTIAL -> receive_event (4)
        assert pool.receive_event.call_count == 4
        # LEVEL_UPDATE, STOCK_UPDATE, STOCK_CRITICAL, STOCK_RECOVERY, ORDER_TO_RESTOCK -> receive_stock_update (5)
        assert pool.receive_stock_update.call_count == 5
        assert result["events"] == 9
        assert result["received"] == 9

    @pytest.mark.asyncio
    async def test_missing_store_id_skipped(self):
        from routers.websocket import internal_broadcast

        request = MagicMock()
        request.json = AsyncMock(return_value=[
            {"eventType": "THEFT"},  # no storeId
        ])
        request.app.state.ws_manager.pools = {}

        result = await internal_broadcast(request, _user={"sub": "svc"})
        assert result["events"] == 0

    @pytest.mark.asyncio
    async def test_no_pool_for_store_skipped(self):
        from routers.websocket import internal_broadcast

        request = MagicMock()
        request.json = AsyncMock(return_value=[
            {"storeId": 999, "eventType": "THEFT"},
        ])
        request.app.state.ws_manager.pools = {}  # no pool for store 999

        result = await internal_broadcast(request, _user={"sub": "svc"})
        assert result["events"] == 0

    @pytest.mark.asyncio
    async def test_unknown_event_type_not_broadcast(self):
        from routers.websocket import internal_broadcast

        pool = MagicMock()
        pool.receive_event = MagicMock()
        pool.receive_stock_update = MagicMock()

        request = MagicMock()
        request.json = AsyncMock(return_value=[
            {"storeId": 1, "eventType": "UNKNOWN_TYPE"},
        ])
        request.app.state.ws_manager.pools = {1: pool}

        result = await internal_broadcast(request, _user={"sub": "svc"})

        pool.receive_event.assert_not_called()
        pool.receive_stock_update.assert_not_called()
        assert result["events"] == 0


# ---------------------------------------------------------------------------
# _websocket_handler — receive_text returns data (debug log branch)
# ---------------------------------------------------------------------------

class TestWebsocketHandlerReceiveData:

    @pytest.mark.asyncio
    async def test_receive_text_then_disconnect(self):
        """Test that receiving a message before disconnect hits the debug log line."""
        from routers.websocket import _websocket_handler, WebSocketDisconnect

        ws = _mock_websocket()
        ws.app.state.ws_manager.initial_data_cache.get.return_value = {"store": {"id": 1}}
        token_obj = _make_token_obj()

        # First call returns data, second raises disconnect
        ws.receive_text.side_effect = ["hello", WebSocketDisconnect()]

        with patch("routers.websocket.OAuth2BearerTokenValidator") as MockVal:
            MockVal.return_value.authenticate_token.return_value = token_obj

            await _websocket_handler(ws, store_id=1, token="tok", endpoint_type="events")

        assert ws.receive_text.await_count == 2
