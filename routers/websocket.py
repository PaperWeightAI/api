#!/usr/bin/env python3
"""
WebSocket Router - Channel-separated WebSocket endpoints with connection pooling

Three specialized channels per store:
- /ws/events/{store_id} - Critical events only (THEFT, RESTOCK) — immediate delivery
- /ws/stock/{store_id} - Stock state snapshots — accumulated, flushed at configurable interval
- /ws/store/{store_id} - Store metadata (initial_data, stats_update, inventory_update)
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Request

from common.auth import OAuth2BearerTokenValidator
from services.aggregator import DataAggregator

logger = logging.getLogger("api.websocket")
router = APIRouter(tags=["websocket"])

INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


def _check_store_access(user: dict, store_id: int) -> bool:
    """
    Check if user has access to the requested store.

    Args:
        user: Decoded JWT token payload
        store_id: Store ID to check access for

    Returns:
        True if user has access, False otherwise
    """
    # Admins have access to all stores
    if user.get("is_admin"):
        return True

    # Check if user has store_ids in their token
    user_store_ids = user.get("store_ids", [])
    if not user_store_ids:
        # If no store_ids specified, deny access (unless admin)
        return False

    # Check if requested store is in user's allowed stores
    return store_id in user_store_ids


def _filter_data_by_permissions(data: dict, user: dict, store_id: int) -> dict:
    """
    Filter aggregated data based on user permissions.

    For non-admin users, only show data they have access to.

    Args:
        data: Initial aggregated data
        user: Decoded JWT token payload
        store_id: Store ID being accessed

    Returns:
        Filtered data dictionary
    """
    # Admins see everything
    if user.get("is_admin"):
        return data

    # For regular users, ensure store matches
    if data.get("store", {}).get("id") != store_id:
        # Security: Don't expose data from other stores
        return {
            "store": {"id": store_id, "name": "Store", "workingMode": "LEVEL"},
            "aisles": [],
            "bays": [],
            "shelves": [],
            "products": [],
            "devices": {"total": 0, "online": 0, "offline": 0},
            "stats": {"counts": {}, "config": {}},
            "inventory": []
        }

    # TODO: Add more granular permissions (aisle-level, shelf-level) if needed
    # For now, if user has access to store, they see all data in that store

    return data


async def _websocket_handler(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str],
    endpoint_type: str
):
    """
    Core WebSocket handler with connection pooling and access control.

    Flow:
    1. Authenticate user
    2. Check store access
    3. Get/create connection pool for store
    4. Add client to pool (shares ONE Stock WS connection)
    5. Send initial data (cached or fresh)
    6. Client receives broadcasts from pool

    Args:
        websocket: Client WebSocket connection
        store_id: Store ID to monitor
        token: Authentication token
        endpoint_type: Type of endpoint ("dashboard" or "events")
    """
    # --- 1. Authenticate ---
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
    token_obj = validator.authenticate_token(auth_token)
    if not token_obj:
        logger.warning(
            f"WebSocket rejected: Invalid token (store {store_id}, {endpoint_type}). "
            "Ensure API has the same JWT public key as the login service and token is not expired."
        )
        await websocket.accept()
        await websocket.close(code=1008, reason="Invalid token")
        return

    # Convert TokenObject to dict for helper functions
    user = {
        'sub': token_obj.username,
        'user_id': token_obj.user_id,
        'is_admin': token_obj.is_admin,
        'is_operator': token_obj.is_operator,
        **token_obj.claims
    }

    # --- 2. Check store access ---
    if not _check_store_access(user, store_id):
        logger.warning(
            f"WebSocket rejected: Access denied to store {store_id} "
            f"for user {user.get('sub')} ({endpoint_type})"
        )
        await websocket.accept()
        await websocket.close(code=1008, reason="Access denied to this store")
        return

    # --- 3. Accept connection ---
    logger.info(
        f"🔌 WebSocket accepted: store {store_id}, user {user.get('sub')} "
        f"(admin={user.get('is_admin')}), {endpoint_type}"
    )
    await websocket.accept()

    try:
        # --- 4. Get WebSocket manager ---
        ws_manager = websocket.app.state.ws_manager

        # --- 5. Check cache for initial data ---
        cached_data = ws_manager.initial_data_cache.get(store_id)

        if cached_data:
            logger.info(f"Using cached initial data for store {store_id}")
            initial_data = cached_data
        else:
            # --- 6. Fetch fresh initial data ---
            logger.info(f"Fetching fresh initial data for store {store_id} ({endpoint_type})")
            http_client = websocket.app.state.http_client
            aggregator = DataAggregator(http_client, auth_token)

            initial_data = await aggregator.aggregate_initial_data(store_id)

            # Cache for 30 seconds
            ws_manager.initial_data_cache.set(store_id, initial_data)
            logger.info(f"Cached initial data for store {store_id}")

        # --- 7. Filter data by user permissions ---
        filtered_data = _filter_data_by_permissions(initial_data, user, store_id)

        # --- 8. Send initial data ---
        await websocket.send_json({
            "type": "initial_data",
            "storeId": store_id,
            "data": filtered_data
        })
        logger.info(f"✓ Initial data sent for store {store_id}")

        # --- 9. Add client to connection pool ---
        channel = endpoint_type  # "events", "stock", or "store"
        added = await ws_manager.add_client(store_id, websocket, auth_token, channel=channel)
        if not added:
            await websocket.send_json({
                "type": "error",
                "message": "Server at capacity for this store — try again later",
            })
            await websocket.close(code=1013, reason="Try again later")
            return
        logger.info("Client added to broadcast pool for store %s", store_id)

        # --- 10. Keep connection alive (pool handles broadcasts) ---
        # The WebSocketManager will broadcast Stock updates to all clients
        # We just need to keep this connection open
        try:
            while True:
                # Wait for client disconnect or messages
                data = await websocket.receive_text()
                # Client shouldn't send messages (read-only for now)
                logger.debug(f"Received message from client (store {store_id}): {data}")
        except WebSocketDisconnect:
            logger.info(f"Client disconnected: store {store_id}, {endpoint_type}")

    except Exception as e:
        logger.error(f"WebSocket error (store {store_id}, {endpoint_type}): {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error",
                "message": "Service error - please reconnect"
            })
        except Exception:
            pass
    finally:
        # --- 11. Remove client from pool ---
        try:
            ws_manager = websocket.app.state.ws_manager
            await ws_manager.remove_client(store_id, websocket)
            logger.info(f"Client removed from pool: store {store_id}, {endpoint_type}")
        except Exception as e:
            logger.error(f"Error removing client from pool: {e}")

        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"🔌 WebSocket closed: store {store_id}, {endpoint_type}")


@router.websocket("/ws/events/{store_id}")
async def websocket_events(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for critical events (THEFT, RESTOCK only).
    Events are delivered immediately with no accumulation.
    """
    await _websocket_handler(websocket, store_id, token, "events")


@router.websocket("/ws/stock/{store_id}")
async def websocket_stock(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for stock state snapshots.
    Accumulated per-product state flushed at WS_STOCK_UPDATE_INTERVAL (default 1s).
    """
    await _websocket_handler(websocket, store_id, token, "stock")


@router.websocket("/ws/store/{store_id}")
async def websocket_store(
    websocket: WebSocket,
    store_id: int,
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for store metadata and retail info.
    Provides initial_data, stats_update, inventory_update.
    """
    await _websocket_handler(websocket, store_id, token, "store")


@router.get("/stats")
async def websocket_stats(request):
    """
    Get WebSocket manager statistics.

    Shows:
    - Total connection pools
    - Total connected clients
    - Clients per store
    - Cache size

    Useful for monitoring and debugging.
    """
    try:
        ws_manager = request.app.state.ws_manager
        stats = ws_manager.get_stats()
        return stats
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get statistics")


@router.post("/internal/broadcast")
async def internal_broadcast(request: Request):
    """
    Internal endpoint for the stock service to push events directly via HTTP.

    Accepts a single event dict or a list of event dicts (batched).
    Routes events to the appropriate channel:
    - THEFT/RESTOCK → events_clients (immediate)
    - Stock events → stock_clients (accumulated snapshot)
    """
    header_secret = request.headers.get("X-Internal-Secret")
    if not INTERNAL_API_SECRET or header_secret != INTERNAL_API_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized internal call")

    body = await request.json()
    events = body if isinstance(body, list) else [body]

    ws_manager = request.app.state.ws_manager
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
            "ORDER_TO_RESTOCK",
        ):
            pool.receive_stock_update(event)
            total_broadcast += 1

    return {"status": "broadcast", "events": total_broadcast, "received": len(events)}
