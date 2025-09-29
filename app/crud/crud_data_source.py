from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DataSource
from app.schemas.data_source import DataSourceCreate, DataSourceUpdate


class CRUDDataSource:
    async def get(self, db: AsyncSession, source_id: int) -> DataSource | None:
        return await db.get(DataSource, source_id)

    async def list(self, db: AsyncSession) -> Sequence[DataSource]:
        res = await db.execute(select(DataSource).order_by(DataSource.id.desc()))
        return res.scalars().all()

    async def create(self, db: AsyncSession, *, obj_in: DataSourceCreate) -> DataSource:
        db_obj = DataSource(**obj_in.dict())
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update(self, db: AsyncSession, *, db_obj: DataSource, obj_in: DataSourceUpdate) -> DataSource:
        obj_data = obj_in.dict(exclude_unset=True)
        for field, value in obj_data.items():
            setattr(db_obj, field, value)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def remove(self, db: AsyncSession, *, source_id: int) -> DataSource | None:
        db_obj = await self.get(db, source_id)
        if db_obj is None:
            return None
        await db.delete(db_obj)
        await db.commit()
        return db_obj


crud_data_source = CRUDDataSource()





