"""API endpoints for prompt management."""

from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from packages.db.repositories.prompts import (
    create_prompt,
    get_prompt,
    list_prompts,
    update_prompt,
    delete_prompt,
    search_prompts,
    PromptRecord,
)
from packages.db.config_schemas import PromptConfig, GptCallConfig

router = APIRouter(prefix="/prompts", tags=["prompts"])


class PromptCreateRequest(BaseModel):
    """Request model for creating a prompt."""
    id: str
    name: str
    content: str
    description: Optional[str] = None
    gpt_config: GptCallConfig


class PromptUpdateRequest(BaseModel):
    """Request model for updating a prompt."""
    name: str
    content: str
    description: Optional[str] = None
    gpt_config: GptCallConfig


class PromptResponse(BaseModel):
    """Response model for prompt data."""
    id: str
    name: str
    content: str
    description: Optional[str] = None
    gpt_config: GptCallConfig
    created_at: datetime
    updated_at: datetime


@router.post("/", response_model=dict)
async def create_prompt_endpoint(request: PromptCreateRequest):
    """Create a new prompt."""
    try:
        # Validate and generate ID if empty
        prompt_id = request.id.strip() if request.id else f"prompt_{int(datetime.now().timestamp())}_{hash(request.name) % 10000}"
        
        if not prompt_id:
            raise HTTPException(status_code=400, detail="Prompt ID cannot be empty")
        
        prompt_config = PromptConfig(
            id=prompt_id,
            name=request.name,
            content=request.content,
            description=request.description,
            gpt_config=request.gpt_config,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        
        created_id = await create_prompt(prompt_config)
        return {"id": created_id, "message": "Prompt created successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=List[PromptResponse])
async def list_prompts_endpoint():
    """List all prompts."""
    try:
        prompts = await list_prompts()
        return [
            PromptResponse(
                id=prompt.id,
                name=prompt.name,
                content=prompt.content,
                description=prompt.description,
                gpt_config=prompt.gpt_config,
                created_at=prompt.created_at,
                updated_at=prompt.updated_at,
            )
            for prompt in prompts
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{prompt_id}", response_model=PromptResponse)
async def get_prompt_endpoint(prompt_id: str):
    """Get a specific prompt by ID."""
    try:
        prompt = await get_prompt(prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt not found")
        
        return PromptResponse(
            id=prompt.id,
            name=prompt.name,
            content=prompt.content,
            description=prompt.description,
            gpt_config=prompt.gpt_config,
            created_at=prompt.created_at,
            updated_at=prompt.updated_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{prompt_id}", response_model=dict)
async def update_prompt_endpoint(prompt_id: str, request: PromptUpdateRequest):
    """Update an existing prompt."""
    try:
        # Check if prompt exists
        existing_prompt = await get_prompt(prompt_id)
        if existing_prompt is None:
            raise HTTPException(status_code=404, detail="Prompt not found")
        
        prompt_config = PromptConfig(
            id=prompt_id,
            name=request.name,
            content=request.content,
            description=request.description,
            gpt_config=request.gpt_config,
            created_at=existing_prompt.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        
        success = await update_prompt(prompt_id, prompt_config)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to update prompt")
        
        return {"message": "Prompt updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{prompt_id}", response_model=dict)
async def delete_prompt_endpoint(prompt_id: str):
    """Delete a prompt."""
    try:
        success = await delete_prompt(prompt_id)
        if not success:
            raise HTTPException(status_code=404, detail="Prompt not found")
        
        return {"message": "Prompt deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search/{query}", response_model=List[PromptResponse])
async def search_prompts_endpoint(query: str):
    """Search prompts by name or content."""
    try:
        prompts = await search_prompts(query)
        return [
            PromptResponse(
                id=prompt.id,
                name=prompt.name,
                content=prompt.content,
                description=prompt.description,
                gpt_config=prompt.gpt_config,
                created_at=prompt.created_at,
                updated_at=prompt.updated_at,
            )
            for prompt in prompts
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
