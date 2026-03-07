#!/usr/bin/env python3
"""
Coverage tests for utils/websocket_manager.py.

Targets the specific uncovered lines: _int_env/_float_env error paths,
_on_task_done exception logging, connect_to_stock, _broadcast_loop,
_parallel_send timeout, token refresh, shutdown, cleanup_empty_pools,
and remove_client.
"""

import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from fastapi import WebSocket


def _mock_ws_spec(name="client"):
    """Create a MagicMock that passes isinstance(x, WebSocket)."""
    ws = MagicMock(spec=WebSocket)
    ws.send_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.__repr__ = lambda self: f"<MockWS:{name}>"
    return ws


class _AsyncIterator:
    """Helper to make a list behave as an async iterator for `async for`."""

    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


# ---------------------------------------------------------------------------
# _int_env / _float_env error handling
# ---------------------------------------------------------------------------

class TestEnvParsing:

    def test_int_env_invalid_value_returns_default(self):
        with patch.dict(os.environ, {"TEST_INT": "not_a_number"}):
            from utils.websocket_manager import _int_env
            result = _int_env("TEST_INT", 42, 0, 100)
        assert result == 42

    def test_float_env_invalid_value_returns_default(self):
        with patch.dict(os.environ, {"TEST_FLOAT": "not_a_float"}):
            from utils.websocket_manager import _float_env
            result = _float_env("TEST_FLOAT", 3.14, 0.0, 10.0)
        assert result == 3.14

    def test_int_env_clamped_to_range(self):
        with patch.dict(os.environ, {"TEST_INT_BIG": "9999"}):
            from utils.websocket_manager import _int_env
            result = _int_env("TEST_INT_BIG", 50, 0, 100)
        assert result == 100

    def test_float_env_clamped_to_range(self):
        with patch.dict(os.environ, {"TEST_FLOAT_SMALL": "-5.0"}):
            from utils.websocket_manager import _float_env
            result = _float_env("TEST_FLOAT_SMALL", 1.0, 0.0, 10.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# _on_task_done — exception logging
# ---------------------------------------------------------------------------

class TestOnTaskDone:

    def test_on_task_done_logs_exception(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("task failed")
        task.get_name.return_value = "test_task"

        pool._background_tasks.add(task)

        with patch("utils.websocket_manager.logger") as mock_logger:
            pool._on_task_done(task)

        mock_logger.error.assert_called_once()
        assert "task failed" in str(mock_logger.error.call_args)

    def test_on_task_done_cancelled_no_log(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        task = MagicMock()
        task.cancelled.return_value = True
        pool._background_tasks.add(task)

        with patch("utils.websocket_manager.logger") as mock_logger:
            pool._on_task_done(task)

        mock_logger.error.assert_not_called()

    def test_on_task_done_no_exception_no_log(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None
        pool._background_tasks.add(task)

        with patch("utils.websocket_manager.logger") as mock_logger:
            pool._on_task_done(task)

        mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# connect_to_stock — success path
# ---------------------------------------------------------------------------

class TestConnectToStock:

    @pytest.mark.asyncio
    async def test_connect_success(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://stock", token="tok")

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        # Mock the async iterator for _broadcast_loop (just exit immediately)
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))

        with patch("utils.websocket_manager.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            # Patch _broadcast_loop to avoid it running forever
            with patch.object(pool, "_broadcast_loop", new_callable=AsyncMock):
                await pool.connect_to_stock()

        assert pool.is_connected is True
        assert pool.is_connecting is False
        assert pool.reconnect_attempts == 0

        # Verify subscribe message was sent
        mock_ws.send.assert_awaited_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["action"] == "subscribe"
        assert sent["filters"]["storeId"] == 1

        # Cancel the broadcast task
        if pool.stock_ws_task:
            pool.stock_ws_task.cancel()
            try:
                await pool.stock_ws_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_connect_already_connected_noop(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://stock", token="tok")
        pool.is_connected = True

        with patch("utils.websocket_manager.websockets.connect", new_callable=AsyncMock) as mock_connect:
            await pool.connect_to_stock()

        mock_connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connect_already_connecting_noop(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://stock", token="tok")
        pool.is_connecting = True

        with patch("utils.websocket_manager.websockets.connect", new_callable=AsyncMock) as mock_connect:
            await pool.connect_to_stock()

        mock_connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connect_failure_retries(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://stock", token="tok")

        call_count = 0

        async def mock_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("refused")
            # Second call succeeds
            ws = AsyncMock()
            ws.send = AsyncMock()
            return ws

        with patch("utils.websocket_manager.websockets.connect", side_effect=mock_connect):
            with patch("utils.websocket_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch.object(pool, "_broadcast_loop", new_callable=AsyncMock):
                    await pool.connect_to_stock()

        assert call_count == 2
        assert pool.is_connected is True

        if pool.stock_ws_task:
            pool.stock_ws_task.cancel()
            try:
                await pool.stock_ws_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# disconnect_from_stock — stock_ws.close() raises
# ---------------------------------------------------------------------------

class TestDisconnectFromStock:

    @pytest.mark.asyncio
    async def test_close_exception_swallowed(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock(side_effect=RuntimeError("close failed"))
        pool.stock_ws = mock_ws
        pool.is_connected = True

        await pool.disconnect_from_stock()

        assert pool.stock_ws is None
        assert pool.is_connected is False


# ---------------------------------------------------------------------------
# _broadcast_loop — message routing
# ---------------------------------------------------------------------------

class TestBroadcastLoop:

    @pytest.mark.asyncio
    async def test_event_pushed_theft_routes_to_receive_event(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        messages = [
            json.dumps({
                "type": "event_pushed",
                "data": {"eventType": "THEFT", "productId": 1, "storeId": 1}
            }),
        ]

        pool.stock_ws = _AsyncIterator(messages)

        with patch.object(pool, "receive_event") as mock_recv:
            with patch.object(pool, "receive_stock_update") as mock_stock:
                await pool._broadcast_loop()

        mock_recv.assert_called_once()
        assert mock_recv.call_args[0][0]["eventType"] == "THEFT"
        mock_stock.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_pushed_restock_routes_to_receive_event(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        messages = [
            json.dumps({
                "type": "event_pushed",
                "data": {"eventType": "RESTOCK", "productId": 2}
            }),
        ]

        pool.stock_ws = _AsyncIterator(messages)

        with patch.object(pool, "receive_event") as mock_recv:
            await pool._broadcast_loop()

        mock_recv.assert_called_once()

    @pytest.mark.asyncio
    async def test_event_pushed_level_update_routes_to_stock_update(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        messages = [
            json.dumps({
                "type": "event_pushed",
                "data": {"eventType": "LEVEL_UPDATE", "productId": 3}
            }),
        ]

        pool.stock_ws = _AsyncIterator(messages)

        with patch.object(pool, "receive_stock_update") as mock_stock:
            with patch.object(pool, "receive_event") as mock_event:
                await pool._broadcast_loop()

        mock_stock.assert_called_once()
        mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_stats_update_routes_to_store_clients(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        msg = json.dumps({"type": "stats_update", "data": {}})

        pool.stock_ws = _AsyncIterator([msg])

        with patch.object(pool, "broadcast_store", new_callable=AsyncMock) as mock_bs:
            await pool._broadcast_loop()

        mock_bs.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_inventory_update_routes_to_store_clients(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        msg = json.dumps({"type": "inventory_update", "data": {}})

        pool.stock_ws = _AsyncIterator([msg])

        with patch.object(pool, "broadcast_store", new_callable=AsyncMock) as mock_bs:
            await pool._broadcast_loop()

        mock_bs.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_unknown_type_broadcasts_to_all(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        msg = json.dumps({"type": "initial_data", "data": {}})

        pool.stock_ws = _AsyncIterator([msg])

        with patch.object(pool, "broadcast", new_callable=AsyncMock) as mock_b:
            await pool._broadcast_loop()

        mock_b.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_invalid_json_skipped(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        pool.stock_ws = _AsyncIterator(["not valid json", json.dumps({"type": "initial_data"})])

        with patch.object(pool, "broadcast", new_callable=AsyncMock) as mock_b:
            await pool._broadcast_loop()

        # Only the valid message should be broadcast
        mock_b.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connection_closed_triggers_reconnect(self):
        import websockets.exceptions
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        class _RaisingIter:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise websockets.exceptions.ConnectionClosed(None, None)

        pool.stock_ws = _RaisingIter()

        with patch.object(pool, "connect_to_stock", new_callable=AsyncMock) as mock_reconnect:
            await pool._broadcast_loop()

        assert pool.is_connected is False
        mock_reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unexpected_error_sets_disconnected(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        class _RaisingIter:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise RuntimeError("unexpected")

        pool.stock_ws = _RaisingIter()

        await pool._broadcast_loop()

        assert pool.is_connected is False


# ---------------------------------------------------------------------------
# _parallel_send — timeout path
# ---------------------------------------------------------------------------

class TestParallelSendTimeout:

    @pytest.mark.asyncio
    async def test_timeout_removes_client(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        ws_ok = _mock_ws_spec("ok")
        ws_slow = _mock_ws_spec("slow")
        ws_slow.send_text = AsyncMock(side_effect=asyncio.TimeoutError())

        pool.stock_clients.add(ws_slow)

        with patch.object(pool, "remove_client", new_callable=AsyncMock) as mock_remove:
            await pool._parallel_send([ws_ok, ws_slow], '{"test":1}')

        ws_ok.send_text.assert_awaited_once()
        mock_remove.assert_awaited_once_with(ws_slow)

    @pytest.mark.asyncio
    async def test_send_exception_removes_client(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")

        ws_bad = _mock_ws_spec("bad")
        ws_bad.send_text = AsyncMock(side_effect=ConnectionError("gone"))

        pool.events_clients.add(ws_bad)

        with patch.object(pool, "remove_client", new_callable=AsyncMock) as mock_remove:
            await pool._parallel_send([ws_bad], '{"test":1}')

        mock_remove.assert_awaited_once_with(ws_bad)

    @pytest.mark.asyncio
    async def test_empty_targets_noop(self):
        from utils.websocket_manager import StoreConnectionPool

        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        # Should not raise
        await pool._parallel_send([], '{"test":1}')


# ---------------------------------------------------------------------------
# Token refresh loop
# ---------------------------------------------------------------------------

class TestTokenRefresh:

    @pytest.mark.asyncio
    async def test_start_and_stop_token_refresh(self):
        from utils.websocket_manager import WebSocketManager

        manager = WebSocketManager(stock_ws_url="ws://test")

        mock_auth = MagicMock()
        mock_auth.get_token = AsyncMock(return_value="new-token")

        with patch("utils.websocket_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Make sleep raise to exit the loop after one iteration
            mock_sleep.side_effect = [None, asyncio.CancelledError()]

            await manager.start_token_refresh(
                http_client=MagicMock(),
                initial_token="old-token",
                service_auth=mock_auth,
            )

            # Let the task run
            try:
                await manager._token_refresh_task
            except asyncio.CancelledError:
                pass

        assert manager._service_token == "new-token"

    @pytest.mark.asyncio
    async def test_stop_token_refresh_when_no_task(self):
        from utils.websocket_manager import WebSocketManager

        manager = WebSocketManager(stock_ws_url="ws://test")
        manager._token_refresh_task = None

        # Should not raise
        await manager.stop_token_refresh()

    @pytest.mark.asyncio
    async def test_stop_token_refresh_cancels_running_task(self):
        from utils.websocket_manager import WebSocketManager

        manager = WebSocketManager(stock_ws_url="ws://test")

        # Create a real long-running task to cancel
        async def _long_sleep():
            await asyncio.sleep(9999)

        manager._token_refresh_task = asyncio.create_task(_long_sleep())

        await manager.stop_token_refresh()

        assert manager._token_refresh_task.cancelled()

    @pytest.mark.asyncio
    async def test_token_refresh_updates_pool_tokens(self):
        from utils.websocket_manager import WebSocketManager, StoreConnectionPool

        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="old")
        manager.pools[1] = pool

        mock_auth = MagicMock()
        mock_auth.get_token = AsyncMock(return_value="refreshed")

        with patch("utils.websocket_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]

            await manager.start_token_refresh(
                http_client=MagicMock(),
                initial_token="old",
                service_auth=mock_auth,
            )

            try:
                await manager._token_refresh_task
            except asyncio.CancelledError:
                pass

        assert pool.token == "refreshed"

    @pytest.mark.asyncio
    async def test_token_refresh_no_auth_client(self):
        """If _service_auth is None, token should not change."""
        from utils.websocket_manager import WebSocketManager

        manager = WebSocketManager(stock_ws_url="ws://test")

        with patch("utils.websocket_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]

            await manager.start_token_refresh(
                http_client=MagicMock(),
                initial_token="original",
                service_auth=None,
            )

            try:
                await manager._token_refresh_task
            except asyncio.CancelledError:
                pass

        assert manager._service_token == "original"

    @pytest.mark.asyncio
    async def test_token_refresh_get_token_returns_none(self):
        """If get_token returns None, token should not change."""
        from utils.websocket_manager import WebSocketManager

        manager = WebSocketManager(stock_ws_url="ws://test")

        mock_auth = MagicMock()
        mock_auth.get_token = AsyncMock(return_value=None)

        with patch("utils.websocket_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]

            await manager.start_token_refresh(
                http_client=MagicMock(),
                initial_token="original",
                service_auth=mock_auth,
            )

            try:
                await manager._token_refresh_task
            except asyncio.CancelledError:
                pass

        assert manager._service_token == "original"


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

class TestShutdown:

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_all_pools(self):
        from utils.websocket_manager import WebSocketManager, StoreConnectionPool

        manager = WebSocketManager(stock_ws_url="ws://test")

        pool1 = MagicMock(spec=StoreConnectionPool)
        pool1.disconnect_from_stock = AsyncMock()
        pool1._background_tasks = set()

        pool2 = MagicMock(spec=StoreConnectionPool)
        pool2.disconnect_from_stock = AsyncMock()
        task = MagicMock()
        task.cancel = MagicMock()
        pool2._background_tasks = {task}

        manager.pools = {1: pool1, 2: pool2}

        with patch.object(manager, "stop_token_refresh", new_callable=AsyncMock):
            await manager.shutdown()

        pool1.disconnect_from_stock.assert_awaited_once()
        pool2.disconnect_from_stock.assert_awaited_once()
        task.cancel.assert_called_once()
        assert len(manager.pools) == 0


# ---------------------------------------------------------------------------
# cleanup_empty_pools
# ---------------------------------------------------------------------------

class TestCleanupEmptyPools:

    @pytest.mark.asyncio
    async def test_removes_empty_disconnected_pools(self):
        from utils.websocket_manager import WebSocketManager, StoreConnectionPool

        manager = WebSocketManager(stock_ws_url="ws://test")

        pool_empty = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        pool_empty.is_connected = False
        # No clients — _client_count() == 0

        pool_active = StoreConnectionPool(store_id=2, stock_ws_url="ws://test", token="tok")
        pool_active.is_connected = True
        # No clients but still connected

        pool_with_clients = StoreConnectionPool(store_id=3, stock_ws_url="ws://test", token="tok")
        ws = MagicMock()
        pool_with_clients.store_clients.add(ws)
        pool_with_clients.is_connected = False

        manager.pools = {1: pool_empty, 2: pool_active, 3: pool_with_clients}

        await manager.cleanup_empty_pools()

        # Only pool 1 should be removed (empty and disconnected)
        assert 1 not in manager.pools
        assert 2 in manager.pools
        assert 3 in manager.pools


# ---------------------------------------------------------------------------
# remove_client on WebSocketManager
# ---------------------------------------------------------------------------

class TestManagerRemoveClient:

    @pytest.mark.asyncio
    async def test_remove_client_from_existing_pool(self):
        from utils.websocket_manager import WebSocketManager, StoreConnectionPool

        manager = WebSocketManager(stock_ws_url="ws://test")
        pool = StoreConnectionPool(store_id=1, stock_ws_url="ws://test", token="tok")
        ws = MagicMock()
        pool.store_clients.add(ws)
        manager.pools[1] = pool

        await manager.remove_client(1, ws)
        assert ws not in pool.store_clients

    @pytest.mark.asyncio
    async def test_remove_client_nonexistent_pool_noop(self):
        from utils.websocket_manager import WebSocketManager

        manager = WebSocketManager(stock_ws_url="ws://test")
        # Should not raise
        await manager.remove_client(999, MagicMock())
