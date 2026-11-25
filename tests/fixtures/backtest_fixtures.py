"""Mock data fixtures for backtest testing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import random


def generate_mock_candles(
    start_time: datetime,
    count: int = 100,
    interval_minutes: int = 5,
    base_price: float = 19500.0,
    volatility: float = 50.0
) -> List[Dict[str, Any]]:
    """Generate mock candle data for testing.
    
    Parameters
    ----------
    start_time : datetime
        Starting timestamp (timezone-aware)
    count : int
        Number of candles to generate
    interval_minutes : int
        Interval between candles in minutes (5 or 15)
    base_price : float
        Base price for candles
    volatility : float
        Price volatility (standard deviation)
    
    Returns
    -------
    List[Dict[str, Any]]
        List of candle dictionaries
    """
    candles = []
    current_time = start_time
    current_price = base_price
    
    for i in range(count):
        # Generate OHLCV data with realistic variations
        price_change = random.gauss(0, volatility)
        open_price = current_price
        close_price = current_price + price_change
        high_price = max(open_price, close_price) + abs(random.gauss(0, volatility * 0.5))
        low_price = min(open_price, close_price) - abs(random.gauss(0, volatility * 0.5))
        volume = random.randint(100000, 500000)
        
        candle = {
            "time": current_time,
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume,
        }
        candles.append(candle)
        
        current_price = close_price
        current_time = current_time + timedelta(minutes=interval_minutes)
    
    return candles


def generate_mock_gpt_response(
    direction: str = "long",
    entry: float = 19500.0,
    stop: float = 19450.0,
    target: float = 19600.0,
    pattern: str = "breakout",
    confidence: str = "high",
    status: str = "waiting"
) -> Dict[str, Any]:
    """Generate mock GPT response (MultiExpertSignal format).
    
    Parameters
    ----------
    direction : str
        Trade direction ('long' or 'short')
    entry : float
        Entry price
    stop : float
        Stop loss price
    target : float
        Target price
    pattern : str
        Pattern name
    confidence : str
        Confidence level
    status : str
        Signal status
    
    Returns
    -------
    Dict[str, Any]
        Mock GPT response
    """
    return {
        "direction": direction,
        "level": entry,
        "stop": stop,
        "target": target,
        "pattern": pattern,
        "condition": pattern,
        "confidence": confidence,
        "status": status,
    }


def generate_mock_multi_expert_response(
    direction: str = "long",
    entry: float = 19500.0,
    stop: float = 19450.0,
    target: float = 19600.0,
    pattern: str = "breakout",
    include_cycle_doc: bool = True
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Generate mock multi-expert response with cycle doc.
    
    Parameters
    ----------
    direction : str
        Trade direction ('long' or 'short')
    entry : float
        Entry price
    stop : float
        Stop loss price
    target : float
        Target price
    pattern : str
        Pattern name
    include_cycle_doc : bool
        Whether to include cycle document
    
    Returns
    -------
    tuple[Dict[str, Any], Optional[Dict[str, Any]]]
        (signal, cycle_doc) tuple
    """
    signal = {
        "direction": direction,
        "level": entry,
        "stop": stop,
        "target": target,
        "pattern": pattern,
        "confidence": "high",
        "status": "waiting",
    }
    
    cycle_doc = None
    if include_cycle_doc:
        cycle_doc = {
            "cycle_id": "test-cycle-123",
            "mode": "multi_expert",
            "agent_id": "test-agent",
            "asset": "NIFTY",
            "symbol": "NIFTY",
            "started_at": datetime.now(timezone.utc),
            "status": "completed",
            "experts": [
                {
                    "kind": "trend",
                    "proposals": [
                        {
                            "direction": direction,
                            "level": entry,
                            "stop": stop,
                            "target": target,
                            "confidence": 0.85
                        }
                    ],
                    "detailed_market_context": "Mock market context",
                    "candel_required_for_judge": 50
                }
            ],
            "judge": {
                "selected_proposal": signal,
                "reasoning": "Mock judge reasoning",
                "confidence": 0.85
            }
        }
    
    return signal, cycle_doc


def generate_mock_code_sniper_response(
    code: Optional[str] = None,
    confidence: float = 0.85,
    ttl_minutes: int = 60
) -> Dict[str, Any]:
    """Generate mock code sniper response.
    
    Parameters
    ----------
    code : str, optional
        Generated Python code (if None, creates a simple example)
    confidence : float
        Confidence level (0.0 to 1.0)
    ttl_minutes : int
        Time to live in minutes
    
    Returns
    -------
    Dict[str, Any]
        Mock code sniper response
    """
    if code is None:
        code = """
def check_trigger(current_candle, historical_candles):
    '''Check if trade trigger conditions are met.'''
    if current_candle['close'] > 19500:
        return {
            'action': 'buy',
            'entry_price': current_candle['close'],
            'stop_loss': current_candle['close'] - 50,
            'target': current_candle['close'] + 100,
            'reason': 'Breakout above 19500'
        }
    return {'action': 'hold', 'reason': 'No trigger yet'}
"""
    
    return {
        "code": code,
        "metadata": {
            "rationale": "Mock code sniper rationale",
            "risk_assessment": "Moderate risk"
        },
        "ttl_minutes": ttl_minutes,
        "confidence": confidence,
    }


class MockCandleService:
    """Mock historical candle service for testing."""
    
    def __init__(self, candles_5m: Optional[List[Dict[str, Any]]] = None, 
                 candles_15m: Optional[List[Dict[str, Any]]] = None):
        """Initialize mock candle service.
        
        Parameters
        ----------
        candles_5m : List[Dict[str, Any]], optional
            Pre-generated 5m candles
        candles_15m : List[Dict[str, Any]], optional
            Pre-generated 15m candles
        """
        self.candles_5m = candles_5m or []
        self.candles_15m = candles_15m or []
        self.call_count = 0
    
    async def fetch_historical_5m(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Mock fetch 5m candles."""
        self.call_count += 1
        return self.candles_5m[:limit]
    
    async def fetch_historical_15m(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 70
    ) -> List[Dict[str, Any]]:
        """Mock fetch 15m candles."""
        self.call_count += 1
        return self.candles_15m[:limit]


class MockMongoCollection:
    """Mock MongoDB collection for testing."""
    
    def __init__(self):
        """Initialize mock collection."""
        self.documents = []
        self.last_inserted_id = None
        self._id_counter = 1
    
    async def insert_one(self, document: Dict[str, Any]) -> Any:
        """Mock insert_one."""
        doc_with_id = {**document, "_id": str(self._id_counter)}
        self._id_counter += 1
        self.documents.append(doc_with_id)
        
        class MockInsertResult:
            def __init__(self, inserted_id):
                self.inserted_id = inserted_id
        
        self.last_inserted_id = doc_with_id["_id"]
        return MockInsertResult(doc_with_id["_id"])
    
    async def find_one(self, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Mock find_one."""
        for doc in self.documents:
            match = all(doc.get(k) == v for k, v in query.items())
            if match:
                return doc
        return None
    
    def find(self, query: Dict[str, Any]):
        """Mock find (returns self for chaining)."""
        self._find_query = query
        return self
    
    def sort(self, key: str, direction: int):
        """Mock sort (returns self for chaining)."""
        self._sort_key = key
        self._sort_direction = direction
        return self
    
    def limit(self, count: int):
        """Mock limit (returns self for chaining)."""
        self._limit = count
        return self
    
    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        """Mock to_list."""
        # Filter by query
        results = []
        for doc in self.documents:
            if hasattr(self, '_find_query'):
                match = all(doc.get(k) == v for k, v in self._find_query.items())
                if match:
                    results.append(doc)
            else:
                results.append(doc)
        
        # Apply limit
        if hasattr(self, '_limit'):
            results = results[:self._limit]
        
        return results
    
    async def update_one(self, query: Dict[str, Any], update: Dict[str, Any]):
        """Mock update_one."""
        for doc in self.documents:
            match = all(doc.get(k) == v for k, v in query.items())
            if match:
                if "$set" in update:
                    doc.update(update["$set"])
                
                class MockUpdateResult:
                    def __init__(self, modified):
                        self.modified_count = 1 if modified else 0
                
                return MockUpdateResult(True)
        
        class MockUpdateResult:
            def __init__(self):
                self.modified_count = 0
        
        return MockUpdateResult()
    
    async def delete_one(self, query: Dict[str, Any]):
        """Mock delete_one."""
        for i, doc in enumerate(self.documents):
            match = all(doc.get(k) == v for k, v in query.items())
            if match:
                self.documents.pop(i)
                
                class MockDeleteResult:
                    def __init__(self):
                        self.deleted_count = 1
                
                return MockDeleteResult()
        
        class MockDeleteResult:
            def __init__(self):
                self.deleted_count = 0
        
        return MockDeleteResult()
    
    async def delete_many(self, query: Dict[str, Any]):
        """Mock delete_many."""
        deleted_count = 0
        i = 0
        while i < len(self.documents):
            doc = self.documents[i]
            match = all(doc.get(k) == v for k, v in query.items())
            if match:
                self.documents.pop(i)
                deleted_count += 1
            else:
                i += 1
        
        class MockDeleteResult:
            def __init__(self, count):
                self.deleted_count = count
        
        return MockDeleteResult(deleted_count)
    
    async def create_index(self, keys: List[tuple]):
        """Mock create_index."""
        pass  # No-op for testing

