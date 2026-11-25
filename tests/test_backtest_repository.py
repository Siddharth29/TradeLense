"""Unit tests for backtest repository."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from packages.db.repositories import backtest as backtest_repo
from tests.fixtures.backtest_fixtures import MockMongoCollection


@pytest.fixture
def mock_mongo_client():
    """Create a mock MongoDB client."""
    client = MagicMock()
    db = MagicMock()
    backtests_coll = MockMongoCollection()
    iterations_coll = MockMongoCollection()
    
    db.__getitem__ = lambda self, name: (
        backtests_coll if name == "backtests" else iterations_coll
    )
    client.__getitem__ = lambda self, name: db
    
    return client, backtests_coll, iterations_coll


class TestBacktestRepository:
    """Test backtest repository functions."""
    
    @pytest.mark.asyncio
    async def test_create_backtest(self, mock_mongo_client):
        """Test creating a new backtest."""
        client, backtests_coll, _ = mock_mongo_client
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            backtest_id = await backtest_repo.create_backtest(
                test_name="Test Backtest",
                config={"candle_count_5m": 100},
                asset="NIFTY",
                start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end_date=datetime(2024, 1, 10, tzinfo=timezone.utc)
            )
            
            assert backtest_id is not None
            assert len(backtests_coll.documents) == 1
            doc = backtests_coll.documents[0]
            assert doc["test_name"] == "Test Backtest"
            assert doc["asset"] == "NIFTY"
            assert doc["status"] == "pending"
    
    @pytest.mark.asyncio
    async def test_get_backtest(self, mock_mongo_client):
        """Test retrieving a backtest by ID."""
        client, backtests_coll, _ = mock_mongo_client
        
        # Insert a test document
        test_doc = {
            "_id": "test123",
            "test_name": "Test",
            "asset": "NIFTY",
            "status": "completed"
        }
        backtests_coll.documents.append(test_doc)
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            with patch('bson.ObjectId', return_value="test123"):
                result = await backtest_repo.get_backtest("test123")
                
                assert result is not None
                assert result["test_name"] == "Test"
                assert result["asset"] == "NIFTY"
    
    @pytest.mark.asyncio
    async def test_list_backtests(self, mock_mongo_client):
        """Test listing backtests with filters."""
        client, backtests_coll, _ = mock_mongo_client
        
        # Insert test documents
        for i in range(5):
            backtests_coll.documents.append({
                "_id": str(i),
                "test_name": f"Test {i}",
                "asset": "NIFTY" if i % 2 == 0 else "BANKNIFTY",
                "created_at": datetime.now(timezone.utc) - timedelta(days=i),
                "status": "completed"
            })
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            # Test without filters
            results = await backtest_repo.list_backtests()
            assert len(results) <= 5
            
            # Test with asset filter
            results = await backtest_repo.list_backtests(asset="NIFTY")
            for result in results:
                assert result["asset"] == "NIFTY"
    
    @pytest.mark.asyncio
    async def test_update_backtest_status(self, mock_mongo_client):
        """Test updating backtest status."""
        client, backtests_coll, _ = mock_mongo_client
        
        # Insert a test document
        test_doc = {
            "_id": "test123",
            "status": "pending"
        }
        backtests_coll.documents.append(test_doc)
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            with patch('bson.ObjectId', return_value="test123"):
                success = await backtest_repo.update_backtest_status(
                    "test123",
                    "running"
                )
                
                assert success is True
                assert backtests_coll.documents[0]["status"] == "running"
    
    @pytest.mark.asyncio
    async def test_update_backtest_progress(self, mock_mongo_client):
        """Test updating backtest progress metrics."""
        client, backtests_coll, _ = mock_mongo_client
        
        # Insert a test document
        test_doc = {
            "_id": "test123",
            "iterations": 0,
            "total_signals": 0
        }
        backtests_coll.documents.append(test_doc)
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            with patch('bson.ObjectId', return_value="test123"):
                success = await backtest_repo.update_backtest_progress(
                    "test123",
                    iterations=10,
                    total_signals=8,
                    wins=5,
                    losses=3,
                    total_pnl=500.0,
                    max_drawdown=-100.0
                )
                
                assert success is True
                assert backtests_coll.documents[0]["iterations"] == 10
                assert backtests_coll.documents[0]["wins"] == 5
    
    @pytest.mark.asyncio
    async def test_update_backtest_completion(self, mock_mongo_client):
        """Test updating backtest with completion data."""
        client, backtests_coll, _ = mock_mongo_client
        
        # Insert a test document
        test_doc = {
            "_id": "test123",
            "status": "running"
        }
        backtests_coll.documents.append(test_doc)
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            with patch('bson.ObjectId', return_value="test123"):
                success = await backtest_repo.update_backtest_completion(
                    "test123",
                    iterations=20,
                    total_signals=18,
                    wins=12,
                    losses=6,
                    total_pnl=1500.0,
                    max_drawdown=-200.0,
                    win_rate=0.67,
                    avg_rr=2.5
                )
                
                assert success is True
                assert backtests_coll.documents[0]["status"] == "completed"
                assert backtests_coll.documents[0]["win_rate"] == 0.67
                assert backtests_coll.documents[0]["avg_rr"] == 2.5
    
    @pytest.mark.asyncio
    async def test_add_iteration(self, mock_mongo_client):
        """Test adding an iteration to a backtest."""
        client, _, iterations_coll = mock_mongo_client
        
        iteration_data = {
            "iteration": 1,
            "iteration_timestamp": datetime.now(timezone.utc),
            "prediction": {"direction": "long"},
            "trade_outcome": {"result": "Win"},
            "status": "completed"
        }
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            iteration_id = await backtest_repo.add_iteration(
                "backtest123",
                iteration_data
            )
            
            assert iteration_id is not None
            assert len(iterations_coll.documents) == 1
            doc = iterations_coll.documents[0]
            assert doc["backtest_id"] == "backtest123"
            assert doc["iteration"] == 1
    
    @pytest.mark.asyncio
    async def test_get_backtest_iterations(self, mock_mongo_client):
        """Test retrieving iterations for a backtest."""
        client, _, iterations_coll = mock_mongo_client
        
        # Insert test iterations
        for i in range(5):
            iterations_coll.documents.append({
                "_id": str(i),
                "backtest_id": "backtest123",
                "iteration": i + 1,
                "status": "completed"
            })
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            results = await backtest_repo.get_backtest_iterations("backtest123")
            
            assert len(results) == 5
            for result in results:
                assert result["backtest_id"] == "backtest123"
    
    @pytest.mark.asyncio
    async def test_delete_backtest(self, mock_mongo_client):
        """Test deleting a backtest and its iterations."""
        client, backtests_coll, iterations_coll = mock_mongo_client
        
        # Insert backtest and iterations
        backtests_coll.documents.append({
            "_id": "backtest123",
            "test_name": "Test"
        })
        for i in range(3):
            iterations_coll.documents.append({
                "_id": str(i),
                "backtest_id": "backtest123",
                "iteration": i + 1
            })
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            with patch('bson.ObjectId', return_value="backtest123"):
                success = await backtest_repo.delete_backtest("backtest123")
                
                assert success is True
                # Verify iterations are deleted
                assert len(iterations_coll.documents) == 0
                # Verify backtest is deleted
                assert len(backtests_coll.documents) == 0
    
    @pytest.mark.asyncio
    async def test_ensure_indexes(self, mock_mongo_client):
        """Test that indexes are created."""
        client, backtests_coll, iterations_coll = mock_mongo_client
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            # Call ensure_indexes
            await backtest_repo.ensure_indexes()
            
            # Should complete without error
            # In real implementation, would verify indexes are created
    
    @pytest.mark.asyncio
    async def test_serialization_of_datetime(self, mock_mongo_client):
        """Test that datetime objects are properly serialized."""
        client, _, iterations_coll = mock_mongo_client
        
        # Create iteration with nested datetime
        iteration_data = {
            "iteration": 1,
            "iteration_timestamp": datetime.now(timezone.utc),
            "prediction": {
                "created_at": datetime.now(timezone.utc)
            },
            "status": "completed"
        }
        
        with patch('packages.db.repositories.backtest.get_client', return_value=client):
            iteration_id = await backtest_repo.add_iteration(
                "backtest123",
                iteration_data
            )
            
            assert iteration_id is not None
            # Datetime should be serialized properly (stays as datetime in MongoDB)
            doc = iterations_coll.documents[0]
            assert isinstance(doc["iteration_timestamp"], datetime)

