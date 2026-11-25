"""
Trading Filters API - Monitor and manage trading filters.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List
from pydantic import BaseModel

from packages.core.trading_filters import trading_filters

router = APIRouter(prefix="/trading-filters", tags=["trading-filters"])


class FilterConfigUpdate(BaseModel):
    """Request model for updating filter configuration."""
    min_risk_reward: float = None
    min_target_points: Dict[str, float] = None
    slippage: Dict[str, float] = None


class FilterStats(BaseModel):
    """Response model for filter statistics."""
    active_trades_count: int
    active_assets: List[str]
    filter_config: Dict[str, Any]


@router.get("/stats", response_model=FilterStats)
async def get_filter_stats():
    """Get current trading filter statistics."""
    try:
        stats = trading_filters.get_filter_stats()
        return FilterStats(**stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/active-trades")
async def get_active_trades():
    """Get all currently active trades."""
    try:
        return trading_filters.get_active_trades()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clear-trade/{asset}")
async def clear_active_trade(asset: str):
    """Clear active trade tracking for a specific asset."""
    try:
        trading_filters.clear_active_trade(asset)
        return {"message": f"Cleared active trade for {asset}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup-expired")
async def cleanup_expired_trades():
    """Manually cleanup expired trades."""
    try:
        trading_filters.cleanup_expired_trades()
        return {"message": "Expired trades cleaned up"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config")
async def update_filter_config(config: FilterConfigUpdate):
    """Update filter configuration."""
    try:
        update_data = {}
        if config.min_risk_reward is not None:
            update_data['min_risk_reward'] = config.min_risk_reward
        if config.min_target_points is not None:
            update_data['min_target_points'] = config.min_target_points
        if config.slippage is not None:
            update_data['slippage'] = config.slippage
        
        trading_filters.update_filter_config(update_data)
        return {"message": "Filter configuration updated", "config": update_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_filter_config():
    """Get current filter configuration."""
    try:
        return trading_filters.filters
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
