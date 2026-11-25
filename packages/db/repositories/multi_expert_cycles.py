"""Repository for multi-expert cycle documents."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from bson import ObjectId

from packages.db.client import get_client
from packages.db.models import MultiExpertCycleDoc, ExpertInsight, JudgeOutcome
from packages.db.persistence_gate import (
    analytics_persistence_enabled,
    log_event,
    synthetic_id,
)
from packages.core.config import MONGO_DB_NAME
from packages.core.logger import get_logger

logger = get_logger(__name__)


class MultiExpertCyclesRepository:
    """Repository for managing multi-expert cycle documents."""
    
    def __init__(self):
        self.collection_name = "multi_expert_cycles"
    
    async def _get_collection(self):
        """Get the MongoDB collection."""
        client = get_client()
        db = client[MONGO_DB_NAME]
        return db[self.collection_name]
    
    async def insert_cycle(self, cycle_doc: MultiExpertCycleDoc) -> str:
        """Insert a new cycle document and return the inserted ID."""
        # Convert to dict for MongoDB insertion / event logging
        doc_dict = cycle_doc.model_dump(exclude_none=True, exclude={"_id"})

        # Durable structured event — the full cycle (experts + judge + rationale)
        # is the system-of-record in log-only mode.
        log_event(
            "cycle", doc_dict, asset=cycle_doc.asset,
            cycle_id=cycle_doc.cycle_id, agent_id=cycle_doc.agent_id,
        )
        if not analytics_persistence_enabled():
            return synthetic_id()

        try:
            collection = await self._get_collection()
            result = await collection.insert_one(doc_dict)
            inserted_id = str(result.inserted_id)

            logger.info(f"Inserted multi-expert cycle {cycle_doc.cycle_id} with ID {inserted_id}")
            return inserted_id

        except Exception as e:
            logger.error(f"Failed to insert multi-expert cycle {cycle_doc.cycle_id}: {str(e)}")
            raise ValueError(f"Failed to insert cycle: {str(e)}") from e
    
    async def update_cycle(self, cycle_id: str, updates: Dict[str, Any]) -> bool:
        """Update an existing cycle document."""
        try:
            collection = await self._get_collection()
            
            # Convert ObjectId string back to ObjectId for query
            query = {"cycle_id": cycle_id}
            
            # Update the document
            result = await collection.update_one(query, {"$set": updates})
            
            if result.modified_count > 0:
                logger.info(f"Updated multi-expert cycle {cycle_id}")
                return True
            else:
                logger.warning(f"No cycle found with ID {cycle_id} to update")
                return False
                
        except Exception as e:
            logger.error(f"Failed to update multi-expert cycle {cycle_id}: {str(e)}")
            raise ValueError(f"Failed to update cycle: {str(e)}") from e
    
    async def get_cycle(self, cycle_id: str) -> Optional[MultiExpertCycleDoc]:
        """Get a specific cycle by ID."""
        try:
            collection = await self._get_collection()
            
            query = {"cycle_id": cycle_id}
            doc = await collection.find_one(query)
            
            if doc:
                # Convert ObjectId to string for JSON serialization
                doc["_id"] = str(doc["_id"])
                return MultiExpertCycleDoc.model_validate(doc)
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get multi-expert cycle {cycle_id}: {str(e)}")
            raise ValueError(f"Failed to get cycle: {str(e)}") from e
    
    async def list_cycles(
        self,
        agent_id: Optional[str] = None,
        asset: Optional[str] = None,
        mode: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 50,
        skip: int = 0,
        sort_by: str = "started_at",
        sort_order: int = -1
    ) -> List[MultiExpertCycleDoc]:
        """List cycles with optional filters."""
        try:
            collection = await self._get_collection()
            
            # Build query
            query = {}
            if agent_id:
                query["agent_id"] = agent_id
            if asset:
                query["asset"] = asset
            if mode:
                query["mode"] = mode
            if status:
                query["status"] = status
            if start_time or end_time:
                time_query = {}
                if start_time:
                    time_query["$gte"] = start_time
                if end_time:
                    time_query["$lte"] = end_time
                query["started_at"] = time_query
            
            # Execute query with sorting and pagination
            cursor = collection.find(query).sort(sort_by, sort_order).skip(skip).limit(limit)
            docs = await cursor.to_list(length=None)
            
            # Convert ObjectIds to strings and validate
            cycles = []
            for doc in docs:
                doc["_id"] = str(doc["_id"])
                cycles.append(MultiExpertCycleDoc.model_validate(doc))
            
            logger.info(f"Retrieved {len(cycles)} multi-expert cycles")
            return cycles
            
        except Exception as e:
            logger.error(f"Failed to list multi-expert cycles: {str(e)}")
            raise ValueError(f"Failed to list cycles: {str(e)}") from e
    
    async def get_cycle_by_mongo_id(self, mongo_id: str) -> Optional[MultiExpertCycleDoc]:
        """Get a specific cycle by MongoDB ObjectId."""
        try:
            collection = await self._get_collection()
            
            query = {"_id": ObjectId(mongo_id)}
            doc = await collection.find_one(query)
            
            if doc:
                # Convert ObjectId to string for JSON serialization
                doc["_id"] = str(doc["_id"])
                return MultiExpertCycleDoc.model_validate(doc)
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get multi-expert cycle by MongoDB ID {mongo_id}: {str(e)}")
            raise ValueError(f"Failed to get cycle by MongoDB ID: {str(e)}") from e
    
    async def count_cycles(
        self,
        agent_id: Optional[str] = None,
        asset: Optional[str] = None,
        mode: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> int:
        """Count cycles matching the given filters."""
        try:
            collection = await self._get_collection()
            
            # Build query (same as list_cycles)
            query = {}
            if agent_id:
                query["agent_id"] = agent_id
            if asset:
                query["asset"] = asset
            if mode:
                query["mode"] = mode
            if status:
                query["status"] = status
            if start_time or end_time:
                time_query = {}
                if start_time:
                    time_query["$gte"] = start_time
                if end_time:
                    time_query["$lte"] = end_time
                query["started_at"] = time_query
            
            count = await collection.count_documents(query)
            return count
            
        except Exception as e:
            logger.error(f"Failed to count multi-expert cycles: {str(e)}")
            raise ValueError(f"Failed to count cycles: {str(e)}") from e
    
    async def delete_cycle(self, cycle_id: str) -> bool:
        """Delete a cycle by ID."""
        try:
            collection = await self._get_collection()
            
            query = {"cycle_id": cycle_id}
            result = await collection.delete_one(query)
            
            if result.deleted_count > 0:
                logger.info(f"Deleted multi-expert cycle {cycle_id}")
                return True
            else:
                logger.warning(f"No cycle found with ID {cycle_id} to delete")
                return False
                
        except Exception as e:
            logger.error(f"Failed to delete multi-expert cycle {cycle_id}: {str(e)}")
            raise ValueError(f"Failed to delete cycle: {str(e)}") from e


# Global instance
multi_expert_cycles_repo = MultiExpertCyclesRepository()
