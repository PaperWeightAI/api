#!/usr/bin/env python3
"""
WebSocket Manager - Efficient connection pooling and broadcasting.

Maintains ONE Stock service WebSocket per store and broadcasts to all clients.
Caches initial data to avoid repeated REST API calls.
Configurable via env for ping, timeout, cleanup grace, cache TTL, and limits.
"""

import asyncio
import json
import logging
import os
from typing import Any

import httpx
import websockets
from datetime import datetime
from fastapi import WebSocket

logger = logging.getLogger("api.ws_manager")

# --- Config (env with safe defaults) ---
def _int_env(name: str, default: int, min_val: int, max_val: int) -> int:
    try:
        v = int(os.getenv(name, str(default)))
        return max(min_val, min(max_val, v))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, min_val: float, max_val: float) -> float:
    try:
        v = float(os.getenv(name, str(default)))
        return max(min_val, min(max_val, v))
    except (TypeError, ValueError):
        return default


WS_PING_INTERVAL = _float_env("WS_PING_INTERVAL", 20.0, 5.0, 300.0)
WS_PING_TIMEOUT = _float_env("WS_PING_TIMEOUT", 10.0, 2.0, 60.0)
WS_OPEN_TIMEOUT = _float_env("WS_OPEN_TIMEOUT", 10.0, 2.0, 60.0)
WS_CLEANUP_GRACE_SECONDS = _int_env("WS_CLEANUP_GRACE_SECONDS", 30, 5, 600)
WS_RECONNECT_BASE_DELAY = _float_env("WS_RECONNECT_BASE_DELAY", 2.0, 0.5, 60.0)
WS_RECONNECT_MAX_DELAY = _float_env("WS_RECONNECT_MAX_DELAY", 30.0, 5.0, 300.0)
WS_INITIAL_DATA_CACHE_TTL = _int_env("WS_INITIAL_DATA_CACHE_TTL", 30, 5, 600)
WS_MAX_CLIENTS_PER_POOL = _int_env("WS_MAX_CLIENTS_PER_POOL", 500, 1, 10_000)
WS_SEND_TIMEOUT = _float_env("WS_SEND_TIMEOUT", 5.0, 1.0, 60.0)  # per-client send timeout
WS_STOCK_UPDATE_INTERVAL = _float_env("WS_STOCK_UPDATE_INTERVAL", 1.0, 0.1, 60.0)


class StoreConnectionPool:
    """
    Manages a single Stock service WebSocket per store and broadcasts to multiple clients.
    Clients are separated into three channels: events, stock, and store.
    """

    def __init__(self, store_id: int, stock_ws_url: str, token: str):
        self.store_id = store_id
        self.stock_ws_url = stock_ws_url
        self.token = token

        # Connected frontend clients by channel
        self.events_clients: set[WebSocket] = set()  # /ws/events — THEFT/RESTOCK
        self.stock_clients: set[WebSocket] = set()    # /ws/stock — stock snapshots
        self.store_clients: set[WebSocket] = set()    # /ws/store — retail info

        # Stock service WebSocket connection
        self.stock_ws: websockets.WebSocketClientProtocol | None = None
        self.stock_ws_task: asyncio.Task | None = None

        # Connection state
        self.is_connecting = False
        self.is_connected = False
        self.reconnect_attempts = 0

        # Stock snapshot accumulator (flushed at WS_STOCK_UPDATE_INTERVAL)
        self._stock_snapshots: dict[int, dict] = {}
        self._stock_flush_task: asyncio.Task | None = None

    @property
    def clients(self) -> set[WebSocket]:
        """All connected clients across all channels."""
        return self.events_clients | self.stock_clients | self.store_clients

    def _client_count(self) -> int:
        return len(self.events_clients) + len(self.stock_clients) + len(self.store_clients)

    async def add_client(self, client: WebSocket, channel: str = "store") -> bool:
        """
        Add a frontend client to this store's broadcast pool.
        Returns False if pool is at capacity (WS_MAX_CLIENTS_PER_POOL).
        """
        if self._client_count() >= WS_MAX_CLIENTS_PER_POOL:
            logger.warning(
                "Store %s pool at max clients (%s), rejecting",
                self.store_id,
                WS_MAX_CLIENTS_PER_POOL,
            )
            return False

        if channel == "events":
            self.events_clients.add(client)
        elif channel == "stock":
            self.stock_clients.add(client)
        else:
            self.store_clients.add(client)

        total = self._client_count()
        logger.info(
            "Client added to store %s pool [%s] (total: %s)", self.store_id, channel, total
        )
        if total == 1 and not self.is_connected:
            await self.connect_to_stock()
        return True

    async def remove_client(self, client: WebSocket):
        """Remove a frontend client from this store's broadcast pool."""
        self.events_clients.discard(client)
        self.stock_clients.discard(client)
        self.store_clients.discard(client)

        remaining = self._client_count()
        logger.info(f"Client removed from store {self.store_id} pool (remaining: {remaining})")

        if remaining == 0:
            logger.info(
                "No clients for store %s, scheduling cleanup in %ss",
                self.store_id,
                WS_CLEANUP_GRACE_SECONDS,
            )
            asyncio.create_task(self._delayed_cleanup())

    async def _delayed_cleanup(self) -> None:
        """Wait WS_CLEANUP_GRACE_SECONDS before closing Stock WS (allows quick reconnects)."""
        await asyncio.sleep(WS_CLEANUP_GRACE_SECONDS)
        if self._client_count() == 0:
            logger.info("Closing Stock WS for store %s (no clients)", self.store_id)
            await self.disconnect_from_stock()

    async def connect_to_stock(self):
        """Connect to Stock service WebSocket."""
        if self.is_connecting or self.is_connected:
            return

        self.is_connecting = True
        logger.info(f"Connecting to Stock service for store {self.store_id}")

        try:
            extra_headers = {"Authorization": f"Bearer {self.token}"}
            self.stock_ws = await websockets.connect(
                f"{self.stock_ws_url}?token={self.token}",
                additional_headers=extra_headers,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
                open_timeout=WS_OPEN_TIMEOUT,
            )

            # Subscribe to this store
            await self.stock_ws.send(json.dumps({
                "action": "subscribe",
                "filters": {"storeId": self.store_id},
            }))

            self.is_connected = True
            self.is_connecting = False
            self.reconnect_attempts = 0
            logger.info(f"✓ Connected to Stock service for store {self.store_id}")

            # Start broadcast task
            self.stock_ws_task = asyncio.create_task(self._broadcast_loop())

        except Exception as e:
            logger.error(f"Failed to connect to Stock service for store {self.store_id}: {e}")
            self.is_connecting = False
            self.is_connected = False

            self.reconnect_attempts += 1
            delay = min(
                WS_RECONNECT_BASE_DELAY * (2 ** self.reconnect_attempts),
                WS_RECONNECT_MAX_DELAY,
            )
            logger.info("Retrying Stock connection in %ss", delay)
            await asyncio.sleep(delay)
            await self.connect_to_stock()

    async def disconnect_from_stock(self):
        """Disconnect from Stock service WebSocket."""
        if self._stock_flush_task:
            self._stock_flush_task.cancel()
            try:
                await self._stock_flush_task
            except asyncio.CancelledError:
                pass
            self._stock_flush_task = None

        if self.stock_ws_task:
            self.stock_ws_task.cancel()
            try:
                await self.stock_ws_task
            except asyncio.CancelledError:
                pass

        if self.stock_ws:
            await self.stock_ws.close()
            self.stock_ws = None

        self.is_connected = False
        logger.info(f"Disconnected from Stock service for store {self.store_id}")

    # --- Channel-aware broadcast ---

    async def _parallel_send(self, targets: list[WebSocket], message: str) -> None:
        """Send message to a list of clients in parallel, removing failures."""
        if not targets:
            return

        async def _send(client: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(
                    client.send_text(message), timeout=WS_SEND_TIMEOUT
                )
                return None
            except asyncio.TimeoutError:
                logger.warning("Send timeout to client (store %s), disconnecting", self.store_id)
                return client
            except Exception:
                return client

        results = await asyncio.gather(
            *(_send(c) for c in targets),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, WebSocket):
                await self.remove_client(result)

    async def broadcast(self, message: str) -> None:
        """Broadcast message to ALL connected clients."""
        all_clients = list(self.clients)
        await self._parallel_send(all_clients, message)

    async def broadcast_events(self, message: str) -> None:
        """Broadcast message to events channel clients only."""
        await self._parallel_send(list(self.events_clients), message)

    async def broadcast_stock(self, message: str) -> None:
        """Broadcast message to stock channel clients only."""
        await self._parallel_send(list(self.stock_clients), message)

    async def broadcast_store(self, message: str) -> None:
        """Broadcast message to store channel clients only."""
        await self._parallel_send(list(self.store_clients), message)

    # --- Event routing ---

    def receive_event(self, event: dict) -> None:
        """Immediately broadcast a THEFT/RESTOCK event to events_clients."""
        if not self.events_clients:
            return
        payload = json.dumps({"type": "event_pushed", "data": event})
        asyncio.create_task(self.broadcast_events(payload))

    def receive_stock_update(self, event: dict) -> None:
        """Accumulate a stock event into the snapshot buffer (flushed periodically)."""
        product_id = event.get("productId")
        if product_id is None:
            return
        self._stock_snapshots[product_id] = {
            "productId": product_id,
            "productName": event.get("productName", f"Product {product_id}"),
            "shelfId": event.get("shelfId"),
            "stockState": event.get("stockState"),
            "stockAfter": event.get("stockAfter"),
            "stockPercentageAfter": event.get("stockPercentageAfter"),
            "levelPercentageAfter": event.get("levelPercentageAfter"),
            "countPercentageAfter": event.get("countPercentageAfter"),
            "lastEvent": event.get("eventType"),
            "timestamp": event.get("timestamp"),
        }
        if self._stock_flush_task is None:
            self._stock_flush_task = asyncio.create_task(self._stock_flush_loop())

    async def _stock_flush_loop(self) -> None:
        """Periodically flush accumulated stock snapshots to stock_clients."""
        try:
            while True:
                await asyncio.sleep(WS_STOCK_UPDATE_INTERVAL)
                if not self._stock_snapshots or not self.stock_clients:
                    continue
                snapshot = dict(self._stock_snapshots)
                self._stock_snapshots.clear()
                payload = json.dumps({
                    "type": "stock_update",
                    "storeId": self.store_id,
                    "products": list(snapshot.values()),
                })
                await self.broadcast_stock(payload)
        except asyncio.CancelledError:
            pass

    # --- Stock WS loops ---

    async def _broadcast_loop(self):
        """Receive from Stock WS and route to appropriate channel."""
        try:
            async for message in self.stock_ws:
                try:
                    parsed = json.loads(message)
                    msg_type = parsed.get("type", "")
                except (json.JSONDecodeError, AttributeError):
                    continue

                if msg_type == "event_pushed":
                    event = parsed.get("data", {})
                    et = event.get("eventType", "")
                    if et in ("THEFT", "RESTOCK"):
                        self.receive_event(event)
                    else:
                        self.receive_stock_update(event)
                elif msg_type in ("stats_update", "inventory_update"):
                    await self.broadcast_store(message)
                else:
                    # initial_data, subscription_confirmed, error → all clients
                    await self.broadcast(message)
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"Stock WS closed for store {self.store_id}, reconnecting...")
            self.is_connected = False
            await self.connect_to_stock()
        except Exception as e:
            logger.error(f"Broadcast loop error for store {self.store_id}: {e}")


class InitialDataCache:
    """
    In-memory cache for initial data with TTL.
    Reduces REST API calls to Retail/IoT services.
    """

    def __init__(self, ttl_seconds: int | None = None):
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else WS_INITIAL_DATA_CACHE_TTL
        self.cache: dict[int, dict[str, Any]] = {}
        self.timestamps: dict[int, datetime] = {}

    def get(self, store_id: int) -> dict[str, Any] | None:
        """Get cached data if not expired."""
        if store_id not in self.cache:
            return None

        cached_at = self.timestamps[store_id]
        age = (datetime.now() - cached_at).total_seconds()

        if age > self.ttl_seconds:
            logger.debug(f"Cache expired for store {store_id} (age: {age}s)")
            del self.cache[store_id]
            del self.timestamps[store_id]
            return None

        logger.debug(f"Cache hit for store {store_id} (age: {age}s)")
        return self.cache[store_id]

    def set(self, store_id: int, data: dict[str, Any]) -> None:
        """Cache data with current timestamp."""
        self.cache[store_id] = data
        self.timestamps[store_id] = datetime.now()
        logger.debug(f"Cached initial data for store {store_id}")

    def invalidate(self, store_id: int) -> None:
        """Manually invalidate cache for a store."""
        if store_id in self.cache:
            del self.cache[store_id]
            del self.timestamps[store_id]
            logger.debug(f"Cache invalidated for store {store_id}")


class WebSocketManager:
    """
    Global WebSocket manager for efficient connection pooling.
    Manages a service-level token that auto-refreshes before expiry.
    """

    def __init__(self, stock_ws_url: str):
        self.stock_ws_url = stock_ws_url
        self.pools: dict[int, StoreConnectionPool] = {}
        self.initial_data_cache = InitialDataCache()

        # Token refresh (initialized by start_token_refresh)
        self._service_token: str | None = None
        self._token_refresh_task: asyncio.Task | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._login_url = os.getenv("LOGIN_SERVICE_URL", "http://login:8005")
        self._admin_user = os.getenv("ADMIN_USERNAME", "admin")
        self._admin_pass = os.getenv("ADMIN_PASSWORD", "admin")

    # ---- Token Refresh ----

    async def start_token_refresh(
        self, http_client: httpx.AsyncClient, initial_token: str | None
    ) -> None:
        """Initialize the service token and start the refresh loop."""
        self._http_client = http_client
        self._service_token = initial_token
        self._token_refresh_task = asyncio.create_task(self._token_refresh_loop())

    async def stop_token_refresh(self) -> None:
        """Cancel the token refresh loop."""
        if self._token_refresh_task:
            self._token_refresh_task.cancel()
            try:
                await self._token_refresh_task
            except asyncio.CancelledError:
                pass

    async def _token_refresh_loop(self) -> None:
        """Refresh the service token every 20 min (tokens expire at 30 min)."""
        while True:
            await asyncio.sleep(20 * 60)
            await self._fetch_service_token()
            # Push refreshed token to all existing pools
            if self._service_token:
                for pool in self.pools.values():
                    pool.token = self._service_token

    async def _fetch_service_token(self) -> None:
        """Fetch a fresh token from the login service."""
        if not self._http_client:
            return
        try:
            resp = await self._http_client.post(
                f"{self._login_url}/oauth/token",
                data={
                    "grant_type": "password",
                    "username": self._admin_user,
                    "password": self._admin_pass,
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                self._service_token = resp.json().get("access_token")
                logger.info("Service token refreshed")
            else:
                logger.warning("Token refresh failed: %s", resp.status_code)
        except Exception as e:
            logger.warning("Token refresh error: %s", e)

    # ---- Pool Management ----

    def get_or_create_pool(self, store_id: int, token: str) -> StoreConnectionPool:
        """Get existing pool or create new one for a store."""
        effective_token = self._service_token or token
        if store_id not in self.pools:
            logger.info(f"Creating new connection pool for store {store_id}")
            self.pools[store_id] = StoreConnectionPool(
                store_id, self.stock_ws_url, effective_token
            )
        return self.pools[store_id]

    async def add_client(self, store_id: int, client: WebSocket, token: str, channel: str = "store") -> bool:
        """Add a client to the appropriate store pool. Returns False if pool at capacity."""
        pool = self.get_or_create_pool(store_id, token)
        return await pool.add_client(client, channel=channel)

    async def remove_client(self, store_id: int, client: WebSocket):
        """Remove a client from the store pool."""
        if store_id in self.pools:
            await self.pools[store_id].remove_client(client)

    async def cleanup_empty_pools(self):
        """Remove pools with no clients (called periodically)."""
        empty_stores = [
            store_id for store_id, pool in self.pools.items()
            if len(pool.clients) == 0 and not pool.is_connected
        ]
        for store_id in empty_stores:
            logger.info(f"Removing empty pool for store {store_id}")
            del self.pools[store_id]

    def get_stats(self) -> dict[str, Any]:
        """Get manager statistics."""
        return {
            "total_pools": len(self.pools),
            "total_clients": sum(pool._client_count() for pool in self.pools.values()),
            "connected_stores": [
                {
                    "store_id": store_id,
                    "clients": pool._client_count(),
                    "events_clients": len(pool.events_clients),
                    "stock_clients": len(pool.stock_clients),
                    "store_clients": len(pool.store_clients),
                    "connected": pool.is_connected,
                }
                for store_id, pool in self.pools.items()
            ],
            "cache_size": len(self.initial_data_cache.cache),
        }
