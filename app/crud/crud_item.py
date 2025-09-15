from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.schemas.item import ItemCreate, ItemUpdate


class CRUDItem:
    """CRUD helper for Item model."""

    async def get(self, db: AsyncSession, item_id: int) -> Item | None:
        return await db.get(Item, item_id)

    async def get_multi(self, db: AsyncSession, *, skip: int = 0, limit: int = 100) -> Sequence[Item]:
        stmt = select(Item).offset(skip).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()

    async def create(self, db: AsyncSession, *, obj_in: ItemCreate) -> Item:
        db_obj = Item(**obj_in.dict())
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update(self, db: AsyncSession, *, db_obj: Item, obj_in: ItemUpdate) -> Item:
        obj_data = obj_in.dict(exclude_unset=True)
        for field, value in obj_data.items():
            setattr(db_obj, field, value)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def remove(self, db: AsyncSession, *, item_id: int) -> Item | None:
        db_obj = await self.get(db, item_id)
        if db_obj is None:
            return None
        await db.delete(db_obj)
        await db.commit()
        return db_obj


crud_item = CRUDItem()
