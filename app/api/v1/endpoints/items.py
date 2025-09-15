from typing import Sequence

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.crud.crud_item import crud_item
from app.schemas.item import Item, ItemCreate, ItemUpdate

router = APIRouter()


@router.get("/", response_model=list[Item])
async def read_items(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db_session),
) -> Sequence[Item]:
    """Retrieve items."""
    return await crud_item.get_multi(db, skip=skip, limit=limit)


@router.post("/", response_model=Item, status_code=status.HTTP_201_CREATED)
async def create_item(*, db: AsyncSession = Depends(get_db_session), item_in: ItemCreate) -> Item:
    """Create new item."""
    return await crud_item.create(db, obj_in=item_in)


@router.get("/{item_id}", response_model=Item)
async def read_item(*, db: AsyncSession = Depends(get_db_session), item_id: int) -> Item:
    """Get item by ID."""
    item = await crud_item.get(db, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.patch("/{item_id}", response_model=Item)
async def update_item(
    *,
    db: AsyncSession = Depends(get_db_session),
    item_id: int,
    item_in: ItemUpdate,
) -> Item:
    """Update an item."""
    db_item = await crud_item.get(db, item_id)
    if db_item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return await crud_item.update(db, db_obj=db_item, obj_in=item_in)


@router.delete("/{item_id}", response_model=Item)
async def delete_item(*, db: AsyncSession = Depends(get_db_session), item_id: int) -> Item:
    """Delete an item."""
    deleted = await crud_item.remove(db, item_id=item_id)
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return deleted
