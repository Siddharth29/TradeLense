"""Repository for managing GPT prompts."""

from typing import List, Optional
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel, Field, ConfigDict

from ..client import get_client
from ..config_schemas import PromptConfig, GptCallConfig
from packages.core.config import MONGO_DB_NAME

PROMPTS_COLLECTION = "prompts"


async def get_collection() -> AsyncIOMotorCollection:
    """Get the prompts collection."""
    client = get_client()
    db = client[MONGO_DB_NAME]
    return db[PROMPTS_COLLECTION]


class PromptRecord(BaseModel):
    """Persisted prompt info with GPT configuration."""

    id: str = Field(alias="_id")
    name: str
    content: str
    description: Optional[str] = None
    gpt_config: GptCallConfig
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(populate_by_name=True, extra="forbid", strict=True)


async def create_prompt(prompt: PromptConfig) -> str:
    """Create a new prompt."""
    coll = await get_collection()
    
    # Validate and generate ID if empty
    prompt_id = prompt.id.strip() if prompt.id else f"prompt_{int(prompt.created_at.timestamp())}_{hash(prompt.name) % 10000}"
    
    if not prompt_id:
        raise ValueError("Prompt ID cannot be empty")
    
    doc = {
        "_id": prompt_id,
        "name": prompt.name,
        "content": prompt.content,
        "description": prompt.description,
        "gpt_config": prompt.gpt_config.model_dump(),
        "created_at": prompt.created_at,
        "updated_at": prompt.updated_at,
    }
    
    await coll.insert_one(doc)
    return prompt_id


async def get_prompt(prompt_id: str) -> Optional[PromptRecord]:
    """Get a prompt by ID."""
    coll = await get_collection()
    doc = await coll.find_one({"_id": prompt_id})
    
    if doc is None:
        return None
    
    return PromptRecord.model_validate(doc, strict=False)


async def list_prompts() -> List[PromptRecord]:
    """List all prompts."""
    coll = await get_collection()
    cursor = coll.find().sort("created_at", -1)
    docs = await cursor.to_list(length=None)
    
    return [PromptRecord.model_validate(doc, strict=False) for doc in docs]


async def update_prompt(prompt_id: str, prompt: PromptConfig) -> bool:
    """Update an existing prompt."""
    coll = await get_collection()
    
    doc = {
        "name": prompt.name,
        "content": prompt.content,
        "description": prompt.description,
        "gpt_config": prompt.gpt_config.model_dump(),
        "updated_at": datetime.now(timezone.utc),
    }
    
    result = await coll.update_one(
        {"_id": prompt_id},
        {"$set": doc}
    )
    
    return result.modified_count > 0


async def delete_prompt(prompt_id: str) -> bool:
    """Delete a prompt."""
    coll = await get_collection()
    result = await coll.delete_one({"_id": prompt_id})
    return result.deleted_count > 0


async def search_prompts(query: str) -> List[PromptRecord]:
    """Search prompts by name or content."""
    coll = await get_collection()
    
    # Create text search query
    search_query = {
        "$or": [
            {"name": {"$regex": query, "$options": "i"}},
            {"content": {"$regex": query, "$options": "i"}},
            {"description": {"$regex": query, "$options": "i"}},
        ]
    }
    
    cursor = coll.find(search_query).sort("created_at", -1)
    docs = await cursor.to_list(length=None)
    
    return [PromptRecord.model_validate(doc, strict=False) for doc in docs]
