from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
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
    obj = await crud_data_source.create(db, obj_in=body)
    if obj.enabled:
        manager.start(obj.id, obj.type, obj.config)
    return obj


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

    # Reconcile running instance
    await manager.stop(updated.id)
    if updated.enabled:
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



