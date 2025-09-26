from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
import secrets
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.crud.crud_data_source import crud_data_source
from app.schemas.data_source import DataSourceCreate, DataSourceOut, DataSourceUpdate
from app.streams.producer_manager import manager


router = APIRouter()


@router.get("/", response_model=list[DataSourceOut])
async def list_sources(db: AsyncSession = Depends(get_db_session)) -> list[DataSourceOut]:
    return list(await crud_data_source.list(db))


@router.post("/", response_model=DataSourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(
    *,
    db: AsyncSession = Depends(get_db_session),
    body: DataSourceCreate,
) -> DataSourceOut:
    one_time_token: str | None = None
    one_time_agent_id: str | None = None
    # Auto-generate token/agent_id for telegraf if missing
    if body.type == "telegraf":
        cfg = dict(body.config or {})
        if not cfg.get("token"):
            one_time_token = secrets.token_urlsafe(32)
            cfg["token"] = one_time_token
        if not cfg.get("agent_id"):
            one_time_agent_id = str(uuid.uuid4())
            cfg["agent_id"] = one_time_agent_id
        body = DataSourceCreate(name=body.name, type=body.type, enabled=body.enabled, config=cfg)

    obj = await crud_data_source.create(db, obj_in=body)
    # Only start producer-backed sources
    if obj.enabled and obj.type not in {"telegraf"}:
        manager.start(obj.id, obj.type, obj.config)
    # Attach one-time token fields on response only (not persisted beyond config)
    out = DataSourceOut.model_validate({
        "id": obj.id,
        "name": obj.name,
        "type": obj.type,
        "enabled": obj.enabled,
        "config": obj.config,
        "one_time_token": one_time_token,
        "one_time_agent_id": one_time_agent_id,
    })
    return out


@router.patch("/{source_id}", response_model=DataSourceOut)
async def update_source(
    *,
    db: AsyncSession = Depends(get_db_session),
    source_id: int,
    body: DataSourceUpdate,
) -> DataSourceOut:
    exists = await crud_data_source.get(db, source_id)
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")
    updated = await crud_data_source.update(db, db_obj=exists, obj_in=body)

    # Reconcile running instance (skip non-producer types like telegraf)
    await manager.stop(updated.id)
    if updated.enabled and updated.type not in {"telegraf"}:
        manager.start(updated.id, updated.type, updated.config)
    return updated


@router.delete("/{source_id}")
async def delete_source(
    *,
    db: AsyncSession = Depends(get_db_session),
    source_id: int,
) -> dict[str, str]:
    await manager.stop(source_id)
    deleted = await crud_data_source.remove(db, source_id=source_id)
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")
    return {"status": "ok"}




