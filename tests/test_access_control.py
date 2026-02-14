#!/usr/bin/env python3
"""
Tests for WebSocket access control and data filtering.

Verifies:
- Admin access to all stores
- Regular user access restricted to store_ids in token
- Data filtering by permissions
- Edge cases (empty store_ids, mismatched store)
"""

from routers.websocket import _check_store_access, _filter_data_by_permissions


class TestCheckStoreAccess:
    """Test _check_store_access logic."""

    def test_admin_can_access_any_store(self):
        user = {"is_admin": True, "sub": "admin"}
        assert _check_store_access(user, 1) is True
        assert _check_store_access(user, 2) is True
        assert _check_store_access(user, 999) is True

    def test_user_with_matching_store(self):
        user = {"is_admin": False, "store_ids": [1, 2, 3]}
        assert _check_store_access(user, 1) is True
        assert _check_store_access(user, 2) is True
        assert _check_store_access(user, 3) is True

    def test_user_without_matching_store(self):
        user = {"is_admin": False, "store_ids": [1, 2]}
        assert _check_store_access(user, 5) is False

    def test_user_with_no_store_ids(self):
        user = {"is_admin": False, "store_ids": []}
        assert _check_store_access(user, 1) is False

    def test_user_missing_store_ids_field(self):
        user = {"is_admin": False}
        assert _check_store_access(user, 1) is False

    def test_admin_flag_takes_priority(self):
        """Admin flag overrides missing store_ids."""
        user = {"is_admin": True, "store_ids": []}
        assert _check_store_access(user, 99) is True

    def test_single_store_user(self):
        user = {"is_admin": False, "store_ids": [5]}
        assert _check_store_access(user, 5) is True
        assert _check_store_access(user, 1) is False


class TestFilterDataByPermissions:
    """Test _filter_data_by_permissions logic."""

    def _sample_data(self, store_id=1):
        return {
            "store": {"id": store_id, "name": "Main Store", "workingMode": "LEVEL"},
            "aisles": [{"id": 1, "name": "Aisle 1"}],
            "bays": [{"id": 1}],
            "shelves": [{"id": 1}],
            "products": [{"id": 1, "name": "Doritos"}],
            "devices": {"total": 5, "online": 4, "offline": 1},
            "stats": {"counts": {"FULL": 2}},
            "inventory": [{"shelfId": 1, "productId": 1}],
        }

    def test_admin_sees_everything(self):
        user = {"is_admin": True}
        data = self._sample_data()
        result = _filter_data_by_permissions(data, user, 1)
        assert result == data

    def test_regular_user_matching_store(self):
        user = {"is_admin": False, "store_ids": [1]}
        data = self._sample_data(store_id=1)
        result = _filter_data_by_permissions(data, user, 1)
        # Should see all data for their store
        assert len(result["aisles"]) == 1
        assert len(result["products"]) == 1

    def test_regular_user_mismatched_store_gets_empty(self):
        """If data.store.id doesn't match requested store_id, return empty."""
        user = {"is_admin": False, "store_ids": [1]}
        data = self._sample_data(store_id=999)  # Data says store 999
        result = _filter_data_by_permissions(data, user, 1)  # User asks for store 1
        # Security filter: return empty data
        assert result["aisles"] == []
        assert result["products"] == []
        assert result["shelves"] == []

    def test_data_without_store_id(self):
        user = {"is_admin": False, "store_ids": [1]}
        data = {"store": {}, "aisles": [], "shelves": []}
        result = _filter_data_by_permissions(data, user, 1)
        # store.id is None != 1, so empty data returned
        assert result["aisles"] == []

    def test_admin_with_mismatched_store_still_sees_all(self):
        user = {"is_admin": True}
        data = self._sample_data(store_id=999)
        result = _filter_data_by_permissions(data, user, 1)
        # Admin bypasses all filtering
        assert result == data
