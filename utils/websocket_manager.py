#!/usr/bin/env python3
"""
WebSocket Manager - Efficient connection pooling and broadcasting

Maintains ONE Stock service WebSocket per store and broadcasts to all clients.
Caches initial data to avoid repeated REST API calls.
"""

import asyncio
import json
import logging
import time
from typing import Dict, Set, Optional, Any
from datetime import datetime, timedelta

import websockets
from fastapi import WebSocket

logger = logging.getLogger("api.ws_manager")


class StoreConnectionPool:
    """
    Manages a single Stock service WebSocket per store and broadcasts to multiple clients.
    """

    def __init__(self, store_id: int, stock_ws_url: str, token: str):
        self.store_id = store_id
        self.stock_ws_url = stock_ws_url
        self.token = token

        # Connected frontend clients for this store
        self.clients: Set[WebSocket] = set()

        # Stock service WebSocket connection
        self.stock_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.stock_ws_task: Optional[asyncio.Task] = None

        # Connection state
        self.is_connecting = False
        self.is_connected = False
        self.reconnect_attempts = 0

    async def add_client(self, client: WebSocket):
        """Add a frontend client to this store's broadcast pool."""
        self.clients.add(client)
        logger.info(f"Client added to store {self.store_id} pool (total: {len(self.clients)})")

        # Start Stock WS if this is the first client
        if len(self.clients) == 1 and not self.is_connected:
            await self.connect_to_stock()

    async def remove_client(self, client: WebSocket):
        """Remove a frontend client from this store's broadcast pool."""
        self.clients.discard(client)
        logger.info(f"Client removed from store {self.store_id} pool (remaining: {len(self.clients)})")

        # Close Stock WS if no more clients (after 30s grace period)
        if len(self.clients) == 0:
            logger.info(f"No clients for store {self.store_id}, scheduling cleanup in 30s")
            asyncio.create_task(self._delayed_cleanup())

    async def _delayed_cleanup(self):
        """Wait 30s before closing Stock WS (allows quick reconnects)."""
        await asyncio.sleep(30)
        if len(self.clients) == 0:
            logger.info(f"Closing Stock WS for store {self.store_id} (no clients)")
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
                ping_interval=20,
                ping_timeout=10,
                open_timeout=10,
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

            # Retry with exponential backoff
            self.reconnect_attempts += 1
            delay = min(2 ** self.reconnect_attempts, 30)
            logger.info(f"Retrying Stock connection in {delay}s")
            await asyncio.sleep(delay)
            await self.connect_to_stock()

    async def disconnect_from_stock(self):
        """Disconnect from Stock service WebSocket."""
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

    async def _broadcast_loop(self):
        """Receive from Stock WS and broadcast to all clients."""
        try:
            async for message in self.stock_ws:
                await self.broadcast(message)
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"Stock WS closed for store {self.store_id}, reconnecting...")
            self.is_connected = False
            await self.connect_to_stock()
        except Exception as e:
            logger.error(f"Broadcast loop error for store {self.store_id}: {e}")

    async def broadcast(self, message: str):
        """Broadcast message to all connected clients."""
        if not self.clients:
            return

        # Parse message to log type
        try:
            msg = json.loads(message)
            msg_type = msg.get("type", "unknown")
        except:
            msg_type = "unknown"

        logger.debug(f"Broadcasting {msg_type} to {len(self.clients)} clients (store {self.store_id})")

        # Send to all clients (remove disconnected ones)
        disconnected = []
        for client in self.clients:
            try:
                await client.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send to client: {e}")
                disconnected.append(client)

        # Clean up disconnected clients
        for client in disconnected:
            await self.remove_client(client)


class InitialDataCache:
    """
    In-memory cache for initial data with TTL.
    Reduces REST API calls to Retail/IoT services.
    """

    def __init__(self, ttl_seconds: int = 30):
        self.ttl_seconds = ttl_seconds
        self.cache: Dict[int, Dict[str, Any]] = {}  # store_id -> data
        self.timestamps: Dict[int, datetime] = {}  # store_id -> cached_at

    def get(self, store_id: int) -> Optional[Dict[str, Any]]:
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

    def set(self, store_id: int, data: Dict[str, Any]):
        """Cache data with current timestamp."""
        self.cache[store_id] = data
        self.timestamps[store_id] = datetime.now()
        logger.debug(f"Cached initial data for store {store_id}")

    def invalidate(self, store_id: int):
        """Manually invalidate cache for a store."""
        if store_id in self.cache:
            del self.cache[store_id]
            del self.timestamps[store_id]
            logger.debug(f"Cache invalidated for store {store_id}")


class WebSocketManager:
    """
    Global WebSocket manager for efficient connection pooling.
    """

    def __init__(self, stock_ws_url: str):
        self.stock_ws_url = stock_ws_url
        self.pools: Dict[int, StoreConnectionPool] = {}  # store_id -> pool
        self.initial_data_cache = InitialDataCache(ttl_seconds=30)

    def get_or_create_pool(self, store_id: int, token: str) -> StoreConnectionPool:
        """Get existing pool or create new one for a store."""
        if store_id not in self.pools:
            logger.info(f"Creating new connection pool for store {store_id}")
            self.pools[store_id] = StoreConnectionPool(store_id, self.stock_ws_url, token)
        return self.pools[store_id]

    async def add_client(self, store_id: int, client: WebSocket, token: str):
        """Add a client to the appropriate store pool."""
        pool = self.get_or_create_pool(store_id, token)
        await pool.add_client(client)

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

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        return {
            "total_pools": len(self.pools),
            "total_clients": sum(len(pool.clients) for pool in self.pools.values()),
            "connected_stores": [
                {
                    "store_id": store_id,
                    "clients": len(pool.clients),
                    "connected": pool.is_connected
                }
                for store_id, pool in self.pools.items()
            ],
            "cache_size": len(self.initial_data_cache.cache)
        }
