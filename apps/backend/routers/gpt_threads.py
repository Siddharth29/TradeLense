"""
GPT Threads API - Manage GPT thread persistence and reuse.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any
from pydantic import BaseModel

from packages.db.repositories import gpt_threads

router = APIRouter(prefix="/gpt-threads", tags=["gpt-threads"])


class ThreadStats(BaseModel):
    """Response model for thread statistics."""
    total_threads: int
    active_threads: int
    expired_threads: int
    unique_asset_agent_combinations: int


@router.get("/stats", response_model=ThreadStats)
async def get_thread_stats():
    """Get GPT thread statistics."""
    try:
        stats = await gpt_threads.get_thread_stats()
        return ThreadStats(**stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup")
async def cleanup_expired_threads():
    """Manually cleanup expired GPT threads."""
    try:
        cleaned_count = await gpt_threads.cleanup_expired_threads()
        return {"message": f"Cleaned up {cleaned_count} expired threads"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/thread/{asset}/{agent_id}")
async def get_thread_info(asset: str, agent_id: str):
    """Get thread information for a specific asset/agent combination.
    
    Note: Threads are no longer reused for sniper calls. This endpoint shows
    historical thread data only.
    """
    try:
        thread_id = await gpt_threads.get_or_create_thread(asset, agent_id)
        if thread_id:
            return {
                "asset": asset, 
                "agent_id": agent_id, 
                "thread_id": thread_id, 
                "status": "active",
                "note": "Threads are no longer reused for sniper calls"
            }
        else:
            return {
                "asset": asset, 
                "agent_id": agent_id, 
                "thread_id": None, 
                "status": "not_found",
                "note": "Threads are no longer reused for sniper calls"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
