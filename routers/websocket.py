#!/usr/bin/env python3
"""
WebSocket Router - Unified WebSocket endpoint for dashboard and events

This router provides WebSocket endpoints that:
1. Accept client connections with authentication
2. Send complete initial data immediately (no REST API needed)
3. Subscribe to Stock service for real-time updates
4. Forward stats, inventory, and event updates to clients

Endpoints:
- /ws/dashboard/{store_id} - Dashboard page WebSocket
- /ws/events/{store_id} - Retail events page WebSocket
"""

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import websockets

from common.auth import OAuth2BearerTokenValidator
from services.aggregator import DataAggregator

logger = logging.getLogger("api.websocket")
router = APIRouter(tags=["websocket"])

# Configuration
STOCK_SERVICE_URL = os.getenv("STOCK_SERVICE_URL", "http://stock:8000")
WS_PUSH_INTERVAL = float(os.getenv("WS_PUSH_INTERVAL", "1.0"))


def _get_stock_ws_url() -> str:
    """Build the Stock service WebSocket URL."""
    base = STOCK_SERVICE_URL
    return base.replace("http://", "ws://").replace("https://", "wss://") + "/ws/stock/events"


async def _forward_stock_updates(
    stock_ws: websockets.WebSocketClientProtocol,
    client_ws: WebSocket,
    store_id: int
):
    """
    Forward messages from Stock service WebSocket to client.

    Args:
        stock_ws: Stock service WebSocket connection
        client_ws: Client WebSocket connection
        store_id: Store ID being monitored
    """
    try:
        async for message in stock_ws:
            try:
                # Parse and forward the message
                data = json.loads(message)
                msg_type = data.get("type")

                # Forward all message types: stats_update, inventory_update, event_pushed, etc.
                logger.debug(f"Forwarding {msg_type} to client for store {store_id}")
                await client_ws.send_json(data)

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON from stock service: {e}")
            except Exception as e:
                logger.error(f"Error forwarding message: {e}")
                break

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Stock service WebSocket closed for store {store_id}")
    except Exception as e:
        logger.error(f"Stock service relay error: {e}")


async def _handle_client_messages(
    client_ws: WebSocket,
    stock_ws: websockets.WebSocketClientProtocol,
    store_id: int
):
    """
    Forward messages from client to Stock service (if any).

    Args:
        client_ws: Client WebSocket connection
        stock_ws: Stock service WebSocket connection
        store_id: Store ID being monitored
    """
    try:
        while True:
            # Receive message from client
            data = await client_ws.receive_text()

            # Forward to stock service
            await stock_ws.send(data)
            logger.debug(f"Forwarded client message to stock service for store {store_id}")

    except WebSocketDisconnect:
        logger.info(f"Client disconnected for store {store_id}")
    except Exception as e:
        logger.error(f"Client message relay error: {e}")


async def _websocket_handler(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str],
    endpoint_type: str  # "dashboard" or "events"
):
    """
    Core WebSocket handler logic.

    Args:
        websocket: Client WebSocket connection
        store_id: Store ID to monitor
        token: Authentication token
        endpoint_type: Type of endpoint ("dashboard" or "events")
    """
    # --- Authenticate ---
    auth_token = token
    if not auth_token:
        auth_header = websocket.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            auth_token = auth_header.split(" ")[1]

    if not auth_token:
        auth_token = websocket.cookies.get("access_token")

    if not auth_token:
        logger.warning(f"WebSocket rejected: No token (store {store_id}, {endpoint_type})")
        await websocket.accept()
        await websocket.close(code=1008, reason="No token provided")
        return

    validator = OAuth2BearerTokenValidator()
    user = validator.authenticate_token(auth_token)
    if not user:
        logger.warning(f"WebSocket rejected: Invalid token (store {store_id}, {endpoint_type})")
        await websocket.accept()
        await websocket.close(code=1008, reason="Invalid token")
        return

    # Accept connection
    logger.info(f"🔌 WebSocket accepted: store {store_id}, user {user.get('sub')}, {endpoint_type}")
    await websocket.accept()

    try:
        # 1. Send initial aggregated data
        logger.info(f"Sending initial data for store {store_id} ({endpoint_type})")

        http_client = websocket.app.state.http_client
        aggregator = DataAggregator(http_client, auth_token)

        initial_data = await aggregator.aggregate_initial_data(store_id)

        await websocket.send_json({
            "type": "initial_data",
            "storeId": store_id,
            "data": initial_data
        })

        logger.info(f"✓ Initial data sent for store {store_id}")

        # 2. Connect to Stock service WebSocket for real-time updates
        stock_ws_url = _get_stock_ws_url()
        logger.info(f"Connecting to stock service: {stock_ws_url}")

        extra_headers = {"Authorization": f"Bearer {auth_token}"}

        async with websockets.connect(
            f"{stock_ws_url}?token={auth_token}",
            additional_headers=extra_headers,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=10,
        ) as stock_ws:

            # Subscribe to store events
            await stock_ws.send(json.dumps({
                "action": "subscribe",
                "filters": {"storeId": store_id},
            }))

            logger.info(f"✓ Subscribed to stock updates for store {store_id}")

            # 3. Run bidirectional relay
            relay_tasks = [
                asyncio.create_task(_forward_stock_updates(stock_ws, websocket, store_id)),
                asyncio.create_task(_handle_client_messages(websocket, stock_ws, store_id)),
            ]

            # Wait for either task to complete (disconnect)
            done, pending = await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: store {store_id}, {endpoint_type}")
    except Exception as e:
        logger.error(f"WebSocket error (store {store_id}, {endpoint_type}): {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": "Service unavailable - real-time updates disabled"
            })
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"🔌 WebSocket closed: store {store_id}, {endpoint_type}")


@router.websocket("/ws/dashboard/{store_id}")
async def websocket_dashboard(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for dashboard page.

    Provides complete initial data followed by real-time updates for:
    - Store configuration
    - Aisles, bays, shelves structure
    - Products list
    - Device counts
    - Stock statistics
    - Inventory levels
    - Real-time stock events

    Args:
        websocket: WebSocket connection
        store_id: Store ID to monitor
        token: Optional bearer token (can also use cookie or header)
    """
    await _websocket_handler(websocket, store_id, token, "dashboard")


@router.websocket("/ws/events/{store_id}")
async def websocket_events(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for retail events page.

    Provides complete initial data followed by real-time updates for:
    - Stock statistics and configuration
    - Restock needs (filtered inventory)
    - Real-time stock events

    Args:
        websocket: WebSocket connection
        store_id: Store ID to monitor
        token: Optional bearer token (can also use cookie or header)
    """
    await _websocket_handler(websocket, store_id, token, "events")
